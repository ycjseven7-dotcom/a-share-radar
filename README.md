# 📡 A股题材雷达

每晚21:00（北京时间，工作日）自动扫描全市场，找出"低位启动、量能放大"的题材板块，
从中筛出技术面高分的候选股，推送到你的飞书群。

## 选股逻辑（大白话版）

1. **题材雷达**：扫描全部行业+概念板块
   - 距离近期低点涨了 3%~15% 且放量 → 标记"👀开始跟踪"
   - 涨了 15%~35% → 标记"🔥重点关注"
   - 涨超 35% 的不要（追高风险大）
   - 自动剔除"昨日涨停""ST板块"这类垃圾概念

2. **个股打分（0-100分）**，60分以上才推荐：
   - 趋势：站上20日线 / 均线多头排列
   - MACD：金叉或多头运行
   - 位置：离低点近加分，涨太多扣分
   - 量价：温和放量上涨加分，高位天量扣分
   - K线：孕线、阳吞没、锤子线等看涨形态加分
   - 自动排除：ST股、退市股、上市新股、2元以下低价股

3. **AI解读**：DeepSeek用大白话总结当天最值得关注的题材和风险

## 部署步骤（5分钟）

1. GitHub右上角 ➕ → New repository → 名字随便起（如 a-share-radar）→ 选 Public → Create
2. 进入新仓库 → 点 "uploading an existing file" → 把本文件夹里所有东西拖进去 → Commit changes
3. Settings → Secrets and variables → Actions → New repository secret，逐条添加：

| Name | 内容 | 必填吗 |
|------|------|-------|
| FEISHU_WEBHOOK_URL | 飞书机器人Webhook地址 | ✅ 必填 |
| OPENAI_API_KEY | DeepSeek的Key | 可选（配了才有AI解读） |
| OPENAI_BASE_URL | https://api.deepseek.com/v1 | 可选 |
| OPENAI_MODEL | deepseek-chat | 可选 |

4. Actions 标签 → 启用 workflows → 左边选"题材雷达每日推送" → Enable workflow → Run workflow 测试

## 想调整策略？

打开 `main.py`，最上面"可调参数"区域，每个参数都有中文注释，
比如想改成只要"重点关注"级别的板块、或者提高个股分数线，改数字就行。

## 免责声明

仅供参考，不构成投资建议。股市有风险，下单前自己再看一眼。
