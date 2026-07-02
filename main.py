# -*- coding: utf-8 -*-
"""
A股题材雷达 · 每日自动选股推送
====================================
流程：
1. 题材雷达：扫描全市场行业+概念板块，找"低位启动、量能放大"的板块
2. 个股筛选：从入选板块的成分股里，按 位置+量价+均线+MACD+K线形态 打分
3. AI解读：调用DeepSeek把结果翻译成大白话（没配Key就跳过）
4. 推送飞书群

环境变量（在GitHub Secrets里配置）：
- FEISHU_WEBHOOK_URL  飞书机器人地址（必填）
- OPENAI_API_KEY      DeepSeek的Key（可选，配了才有AI解读）
- OPENAI_BASE_URL     默认 https://api.deepseek.com/v1
- OPENAI_MODEL        默认 deepseek-chat
"""

import os
import time
import traceback
from datetime import datetime, timedelta

import pandas as pd
import requests

# ============ 可调参数（想改策略就改这里） ============

LOOKBACK_DAYS = 130          # 板块回看多少个自然日的行情
BOARD_POS_WATCH = (0.03, 0.15)   # 距低点3%~15%：开始跟踪
BOARD_POS_FOCUS = (0.15, 0.35)   # 距低点15%~35%：重点关注
BOARD_POS_MAX = 0.35         # 距低点涨超35%的板块不要（追高风险）
BOARD_VOL_RATIO_MIN = 1.2    # 板块近5日量能至少是近60日的1.2倍
TOP_BOARDS = 5               # 最终选几个板块
STOCKS_PER_BOARD = 15        # 每个板块细看多少只成分股
STOCK_MIN_SCORE = 60         # 个股至少多少分才推荐
TOP_STOCKS_PER_BOARD = 3     # 每个板块最多推荐几只
REQUEST_SLEEP = 0.6          # 每次请求之间歇一下，避免被限流

# 垃圾板块黑名单（这些不是真题材）
BOARD_BLACKLIST = [
    "昨日", "涨停", "ST", "次新", "融资融券", "转融券", "百元", "低价",
    "破净", "高送转", "股权转让", "壳资源", "B股", "含可转债", "标普",
    "富时", "MSCI", "沪股通", "深股通", "机构重仓", "基金重仓", "QFII",
    "预盈预增", "预亏预减", "举牌", "微盘股", "AB股", "AH股", "同花顺",
]


# ============ 工具函数 ============

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def ema(series, n):
    return series.ewm(span=n, adjust=False).mean()


def calc_macd(close):
    """返回 DIF、DEA 两条线"""
    dif = ema(close, 12) - ema(close, 26)
    dea = ema(dif, 9)
    return dif, dea


# ============ 第一步：题材雷达 ============

def _pick_col(df, keyword):
    """在表格里找包含关键字的列名（防接口改列名）"""
    for c in df.columns:
        if keyword in str(c):
            return c
    return None


def _fetch_board_list(fn, btype, errors, retries=3):
    """拉板块列表，失败自动重试，错误记下来给报告用"""
    for attempt in range(1, retries + 1):
        try:
            df = fn()
            if df is None or len(df) == 0:
                raise ValueError("返回了空表")
            name_col = _pick_col(df, "名称")
            chg_col = _pick_col(df, "涨跌幅")
            if name_col is None:
                raise ValueError(f"找不到名称列，实际列名: {list(df.columns)[:8]}")
            out = []
            for _, r in df.iterrows():
                chg = 0.0
                if chg_col:
                    try:
                        chg = float(r.get(chg_col, 0) or 0)
                    except Exception:
                        chg = 0.0
                out.append({"name": str(r[name_col]), "type": btype, "chg": chg})
            return out
        except Exception as e:
            log(f"{btype}板块获取失败(第{attempt}次): {e}")
            if attempt == retries:
                errors.append(f"{btype}板块列表: {str(e)[:120]}")
            else:
                time.sleep(3 * attempt)  # 歇一会再试
    return []


def get_all_boards(ak, errors):
    """拿到全市场 行业板块+概念板块 的当日快照"""
    boards = _fetch_board_list(ak.stock_board_industry_name_em, "industry", errors)
    log(f"行业板块 {len(boards)} 个")
    time.sleep(REQUEST_SLEEP)
    concepts = _fetch_board_list(ak.stock_board_concept_name_em, "concept", errors)
    log(f"概念板块 {len(concepts)} 个")
    boards += concepts

    # 过滤黑名单
    boards = [b for b in boards
              if not any(k in b["name"] for k in BOARD_BLACKLIST)]
    return boards


def get_board_hist(ak, name, btype, start, end):
    """拿板块的日K历史，行业和概念的接口参数不太一样，都试一遍"""
    if btype == "industry":
        attempts = [
            dict(symbol=name, start_date=start, end_date=end, period="日k", adjust=""),
            dict(symbol=name, start_date=start, end_date=end, adjust=""),
        ]
        fn = ak.stock_board_industry_hist_em
    else:
        attempts = [
            dict(symbol=name, period="daily", start_date=start, end_date=end, adjust=""),
            dict(symbol=name, start_date=start, end_date=end, adjust=""),
        ]
        fn = ak.stock_board_concept_hist_em
    for kw in attempts:
        try:
            df = fn(**kw)
            if df is not None and len(df) > 20:
                return df
        except Exception:
            continue
    return None


def analyze_board(df):
    """算板块的：距低点位置、近5日涨幅、量能倍数"""
    close = pd.to_numeric(df["收盘"], errors="coerce")
    vol = pd.to_numeric(df["成交量"], errors="coerce")
    close = close.dropna()
    vol = vol.dropna()
    if len(close) < 30:
        return None
    low = close.tail(120).min()
    cur = close.iloc[-1]
    pos = cur / low - 1                       # 距低点涨了多少
    chg5 = cur / close.iloc[-6] - 1 if len(close) > 6 else 0
    v5 = vol.tail(5).mean()
    v60 = vol.tail(60).mean()
    vol_ratio = v5 / v60 if v60 > 0 else 0
    return {"pos": pos, "chg5": chg5, "vol_ratio": vol_ratio}


def scan_boards(ak):
    """题材雷达主流程：粗筛 -> 拉历史 -> 精筛 -> 打分排序
    返回 (命中板块列表, 诊断统计)"""
    errors = []
    boards = get_all_boards(ak, errors)
    stats = {"total": len(boards), "checked": 0, "fetch_fail": 0,
             "pos_low": 0, "pos_high": 0, "vol_low": 0, "hit": 0,
             "errors": errors}
    if not boards:
        return [], stats

    # 粗筛规则：
    # - 行业板块只有80多个，全部细查（低位启动的板块当天不一定涨，不能靠当日涨幅筛）
    # - 概念板块几百个，取当日涨幅前80的细查（概念太多只能取活跃的）
    industries = [b for b in boards if b["type"] == "industry"]
    concepts = [b for b in boards if b["type"] == "concept"]
    concepts.sort(key=lambda b: b["chg"], reverse=True)
    candidates = industries + concepts[:80]
    stats["checked"] = len(candidates)
    log(f"细查 {len(industries)} 个行业板块（全量）+ {min(len(concepts), 80)} 个活跃概念板块")

    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=LOOKBACK_DAYS + 80)).strftime("%Y%m%d")

    results = []
    for b in candidates:
        time.sleep(REQUEST_SLEEP)
        df = get_board_hist(ak, b["name"], b["type"], start, end)
        if df is None:
            stats["fetch_fail"] += 1
            continue
        m = analyze_board(df)
        if m is None:
            stats["fetch_fail"] += 1
            continue
        pos, vr = m["pos"], m["vol_ratio"]

        if pos < BOARD_POS_WATCH[0]:
            stats["pos_low"] += 1
            continue
        if pos > BOARD_POS_MAX:
            stats["pos_high"] += 1
            continue
        if vr < BOARD_VOL_RATIO_MIN:
            stats["vol_low"] += 1
            continue

        if BOARD_POS_FOCUS[0] <= pos <= BOARD_POS_FOCUS[1]:
            tier = "🔥重点关注"
            tier_score = 30
        else:
            tier = "👀开始跟踪"
            tier_score = 20

        score = tier_score + min(vr, 3) * 15 + min(m["chg5"], 0.10) * 200 + b["chg"]
        results.append({**b, **m, "tier": tier, "score": score})

    stats["hit"] = len(results)
    results.sort(key=lambda x: x["score"], reverse=True)
    log(f"雷达命中 {len(results)} 个板块，取前 {TOP_BOARDS} 个")
    return results[:TOP_BOARDS], stats


# ============ 第二步：个股筛选 ============

def get_constituents(ak, name, btype):
    try:
        if btype == "industry":
            df = ak.stock_board_industry_cons_em(symbol=name)
        else:
            df = ak.stock_board_concept_cons_em(symbol=name)
        return df
    except Exception as e:
        log(f"成分股获取失败 {name}: {e}")
        return None


def detect_candle_patterns(df):
    """检测最近3天有没有 孕线/阳吞没/锤子线 这几个看涨形态"""
    o = pd.to_numeric(df["开盘"], errors="coerce").values
    c = pd.to_numeric(df["收盘"], errors="coerce").values
    h = pd.to_numeric(df["最高"], errors="coerce").values
    l = pd.to_numeric(df["最低"], errors="coerce").values
    found = []
    n = len(c)
    for i in range(max(1, n - 3), n):
        body_prev = abs(c[i-1] - o[i-1])
        body_cur = abs(c[i] - o[i])
        # 孕线：前一根大K线，当前小K线完全被包在前一根实体里
        if body_prev > 0 and body_cur < body_prev * 0.6:
            hi_prev, lo_prev = max(o[i-1], c[i-1]), min(o[i-1], c[i-1])
            if max(o[i], c[i]) <= hi_prev and min(o[i], c[i]) >= lo_prev:
                if c[i-1] < o[i-1]:  # 前阴后孕，可能见底
                    found.append("孕线(潜在反转)")
        # 阳吞没：当前大阳线完全吃掉前一根阴线
        if c[i] > o[i] and c[i-1] < o[i-1]:
            if o[i] <= c[i-1] and c[i] >= o[i-1]:
                found.append("阳线吞没(看涨)")
        # 锤子线：下影线很长，实体很小
        rng = h[i] - l[i]
        if rng > 0:
            lower_shadow = min(o[i], c[i]) - l[i]
            if lower_shadow > rng * 0.6 and body_cur < rng * 0.3:
                found.append("锤子线(下方有支撑)")
    return list(dict.fromkeys(found))  # 去重保序


def score_stock(df):
    """个股打分：趋势 + MACD + 位置 + 量价 + K线形态，满分100"""
    close = pd.to_numeric(df["收盘"], errors="coerce")
    vol = pd.to_numeric(df["成交量"], errors="coerce")
    if len(close) < 40:
        return None

    cur = close.iloc[-1]
    ma5 = close.rolling(5).mean().iloc[-1]
    ma10 = close.rolling(10).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    dif, dea = calc_macd(close)

    score = 0
    reasons = []

    # 1) 趋势（30分）
    if cur > ma20:
        score += 15
        reasons.append("站上20日均线")
    if ma5 > ma10 > ma20:
        score += 15
        reasons.append("均线多头排列")

    # 2) MACD（20分）
    if dif.iloc[-1] > dea.iloc[-1]:
        score += 10
        # 最近3天内金叉
        crossed = any(dif.iloc[-i] > dea.iloc[-i] and dif.iloc[-i-1] <= dea.iloc[-i-1]
                      for i in range(1, 4) if len(dif) > i + 1)
        if crossed:
            score += 10
            reasons.append("MACD刚金叉")
        else:
            reasons.append("MACD在零轴上方运行" if dif.iloc[-1] > 0 else "MACD走强")

    # 3) 位置（20分）—— 低位加分，高位扣分
    low60 = close.tail(60).min()
    pos = cur / low60 - 1
    if 0.05 <= pos <= 0.25:
        score += 20
        reasons.append(f"距近期低点仅涨{pos:.0%}，位置不高")
    elif 0.25 < pos <= 0.40:
        score += 10
    elif pos > 0.60:
        score -= 15
        reasons.append(f"⚠️已从低点涨{pos:.0%}，追高有风险")

    # 4) 量价配合（20分）
    v5 = vol.tail(5).mean()
    v30 = vol.tail(30).mean()
    vr = v5 / v30 if v30 > 0 else 0
    chg5 = cur / close.iloc[-6] - 1 if len(close) > 6 else 0
    if 1.2 <= vr <= 3.5 and chg5 > 0:
        score += 20
        reasons.append(f"温和放量上涨(量能{vr:.1f}倍)")
    elif vr > 3.5 and pos > 0.4:
        score -= 10
        reasons.append("⚠️高位天量，警惕出货")

    # 5) K线形态（10分）
    patterns = detect_candle_patterns(df)
    if patterns:
        score += 10
        reasons.append("K线信号: " + "、".join(patterns))

    score = max(0, min(100, score))
    return {"score": score, "reasons": reasons, "pos": pos, "price": cur}


def screen_board_stocks(ak, board):
    """筛一个板块里的个股"""
    cons = get_constituents(ak, board["name"], board["type"])
    if cons is None or len(cons) == 0:
        return []

    # 排雷：ST、退市、新股、低价股不要
    def bad(row):
        name = str(row.get("名称", ""))
        try:
            price = float(row.get("最新价", 0) or 0)
        except Exception:
            price = 0
        return ("ST" in name or "退" in name or name.startswith(("N", "C"))
                or price < 2)

    cons = cons[~cons.apply(bad, axis=1)].copy()
    # 按成交额排序，优先看资金活跃的
    if "成交额" in cons.columns:
        cons["成交额"] = pd.to_numeric(cons["成交额"], errors="coerce")
        cons = cons.sort_values("成交额", ascending=False)
    cons = cons.head(STOCKS_PER_BOARD)

    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=150)).strftime("%Y%m%d")

    picks = []
    for _, row in cons.iterrows():
        code = str(row["代码"]).zfill(6)
        name = str(row["名称"])
        time.sleep(REQUEST_SLEEP)
        try:
            df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                    start_date=start, end_date=end, adjust="qfq")
            if df is None or len(df) < 40:
                continue
            r = score_stock(df)
            if r and r["score"] >= STOCK_MIN_SCORE:
                picks.append({"code": code, "name": name, **r})
        except Exception:
            continue

    picks.sort(key=lambda x: x["score"], reverse=True)
    return picks[:TOP_STOCKS_PER_BOARD]


# ============ 第三步：抓当天财经快讯 + AI大白话解读（可选） ============

def fetch_hot_news(ak, max_items=40):
    """抓当天的财经快讯标题，喂给AI做题材关联分析。
    两个来源都试：财联社电报、东方财富全球快讯，哪个能用用哪个。"""
    news = []
    # 来源1：财联社电报
    try:
        df = ak.stock_info_global_cls(symbol="全部")
        col = "标题" if "标题" in df.columns else df.columns[0]
        for _, r in df.head(max_items).iterrows():
            t = str(r.get(col, "")).strip()
            content = str(r.get("内容", "")).strip()[:80]
            if t or content:
                news.append(t if t and t != "nan" else content)
    except Exception as e:
        log(f"财联社快讯获取失败: {e}")
    # 来源2：东方财富全球财经快讯（补充）
    if len(news) < 15:
        try:
            df = ak.stock_info_global_em()
            col = "标题" if "标题" in df.columns else df.columns[0]
            for _, r in df.head(max_items).iterrows():
                t = str(r.get(col, "")).strip()
                if t and t != "nan":
                    news.append(t)
        except Exception as e:
            log(f"东财快讯获取失败: {e}")
    # 去重、截断
    news = list(dict.fromkeys(news))[:max_items]
    log(f"抓到 {len(news)} 条财经快讯")
    return news


def ai_summary(report_text, news_list):
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    base = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.getenv("OPENAI_MODEL", "deepseek-chat")

    news_block = "\n".join(f"- {n}" for n in news_list) if news_list else "（今日未抓到快讯）"
    prompt = (
        "你是一位说人话的股票分析助手，读者完全不懂技术术语。\n"
        "下面给你两份材料：【今日财经快讯】和【题材雷达筛选结果】。\n\n"
        "请输出（总共不超过450字）：\n"
        "1. 消息面关联：逐个看雷达选出的板块，在快讯里找有没有相关的国内外热点"
        "消息，明确标注是【利好】还是【利空】，并用一句话解释逻辑。\n"
        "2. 如果某个板块在快讯里找不到相关消息，直接写'暂无消息面驱动，属于资金"
        "行为'，严禁编造不存在的新闻。\n"
        "3. 最后给一句整体建议（观察为主还是可小仓位试探），必须提示风险。\n"
        "语气平实，不吹票。直接输出内容，禁止'好的''老板''收到'之类的寒暄开场白。\n\n"
        f"【今日财经快讯】\n{news_block}\n\n"
        f"【题材雷达筛选结果】\n{report_text}"
    )
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": model, "max_tokens": 1200,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=120,
        )
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"AI解读失败(不影响推送): {e}")
        return None


# ============ 第四步：推送飞书 ============

def push_feishu(text):
    url = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not url:
        log("未配置 FEISHU_WEBHOOK_URL，直接打印结果：\n" + text)
        return
    try:
        resp = requests.post(url, json={"msg_type": "text",
                                        "content": {"text": text}}, timeout=30)
        log(f"飞书推送结果: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log(f"飞书推送失败: {e}")


# ============ 主流程 ============

def build_report(boards_result, stats):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📡 A股题材雷达分析日报 {today}", ""]

    if not boards_result:
        lines.append("今日雷达没有扫到符合'低位启动+放量'条件的板块。")
        lines.append("空仓休息也是一种操作，等信号出现再动手。")
    else:
        for i, (board, stocks) in enumerate(boards_result, 1):
            lines.append(f"【{i}】{board['name']}  {board['tier']}")
            lines.append(
                f"    距低点+{board['pos']:.0%} | 近5日{board['chg5']:+.1%} | "
                f"量能{board['vol_ratio']:.1f}倍 | 今日{board['chg']:+.1f}%")
            if stocks:
                for s in stocks:
                    lines.append(f"    ▸ {s['name']}({s['code']}) 评分{s['score']} "
                                 f"现价{s['price']:.2f}")
                    lines.append(f"      {'；'.join(s['reasons'][:3])}")
            else:
                lines.append("    （板块入选，但成分股暂无高分标的，先观察）")
            lines.append("")

    # 诊断信息：让每次运行都能自我解释，出问题好排查
    lines.append(
        f"📋 扫描明细：细查{stats.get('checked', 0)}个板块 | "
        f"命中{stats.get('hit', 0)} | 还在低位没启动{stats.get('pos_low', 0)} | "
        f"涨幅已超35%剔除{stats.get('pos_high', 0)} | "
        f"量能不足{stats.get('vol_low', 0)} | 取数失败{stats.get('fetch_fail', 0)}")
    if stats.get("checked", 0) == 0:
        lines.append("🚨 板块名单一个都没拿到，今天的'没扫到'是故障导致的，不是市场真没信号！")
        for e in stats.get("errors", [])[:3]:
            lines.append(f"    错误详情: {e}")
    elif stats.get("fetch_fail", 0) > stats.get("checked", 1) * 0.3:
        lines.append("⚠️ 今日取数失败偏多，结果可能不完整，建议明天对照观察。")
    lines.append("")

    return "\n".join(lines)


def main():
    log("=== 题材雷达启动 ===")
    try:
        import akshare as ak
    except ImportError:
        push_feishu("题材雷达运行失败：akshare 没装上，请检查 requirements.txt")
        return

    try:
        top_boards, stats = scan_boards(ak)
        boards_result = []
        for b in top_boards:
            log(f"筛选板块成分股: {b['name']}")
            stocks = screen_board_stocks(ak, b)
            boards_result.append((b, stocks))

        report = build_report(boards_result, stats)

        news = fetch_hot_news(ak)
        ai_text = ai_summary(report, news)
        if ai_text:
            report += "\n🗞️ 消息面AI解读\n" + ai_text + "\n"

        report += ("\n——\n仅供参考，不构成投资建议。"
                   "股市有风险，下单前自己再看一眼。")
        push_feishu(report)
        log("=== 完成 ===")
    except Exception:
        err = traceback.format_exc()
        log(err)
        push_feishu(f"题材雷达分析今日运行出错，请检查：\n{err[-500:]}")


if __name__ == "__main__":
    main()
