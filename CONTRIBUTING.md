# Contributing to TMT Atlas

感谢你对 TMT Atlas 的关注。这个项目优先解决证据可追溯、金融口径安全和本地优先体验，而不是单纯增加 Agent 数量。

## 开始前

请先阅读 `README.md`、`docs/ARCHITECTURE.md`、`docs/EVALUATION.md` 和 `docs/DATA-SOURCES.md`。涉及真实公司资料时，请确认来源公开、使用权限清晰，并且不要提交内部材料、API key 或完整受限制报告。

## 本地运行

```bash
python3 server.py
python3 -m unittest discover -s tests -v
```

## 提交原则

新的金融指标必须说明定义、单位、期间、会计口径、来源和缺失值行为。新的模型能力必须有无 LLM 降级路径、证据范围说明、失败状态和至少一个反例测试。新的外部数据连接必须说明权限、费用、速率、缓存和失败处理。

请保持事实、计算、推断和用户观点分层；不要将演示数据写成实时市场数据，不要将研究辅助工具描述成投资建议系统。

## Pull Request 建议

PR 描述请包括背景、用户问题、方案取舍、测试命令、数据来源/许可说明、已知限制和后续工作。如果改变了主要用户流程，请附截图或短录屏。
