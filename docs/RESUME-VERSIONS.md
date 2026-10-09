# TMT Atlas｜简历版本

## 中文版：AI 产品岗位一行版

TMT Atlas｜本地优先 TMT 金融研究工作台：独立实现本地 PDF/CSV/JSON/TXT 证据导入、页码/行号证据检索、SQLite pending 事实审核与审计、公司快照、赛道扫描、事件分析和 Markdown/HTML 备忘录导出；接入 OpenAI-compatible API 后，可在用户显式同意下仅对勾选证据生成草稿，并通过授权 RSS/Atom/JSON Feed 审阅箱收集资讯。

## 中文版：AI 产品岗位两行版

独立设计并实现 TMT Atlas 本地优先金融研究工作台，覆盖公司快照、赛道扫描、事件影响分析、PDF/CSV/JSON/TXT 证据导入、SQLite 事实审核与 Markdown/HTML 备忘录导出；将收入增速、利润率、企业价值、自由现金流和简化 DCF 计算放在可测试的本地规则引擎，LLM 仅通过显式同意的选定证据草稿入口增强表达，资讯收集采用授权 Feed + review-first 审阅箱。  
技术：Python 标准库本地服务、SQLite/FTS5（可回退 LIKE）、HTML/CSS/JavaScript、OpenAI-compatible API 适配和结构化 JSON 数据模型。

## 中文版：金融研究/投研岗位一行版

独立搭建 TMT 研究工作台，围绕 NVIDIA、Microsoft、阿里巴巴、百度等中美公司维护官方 IR/SEC/HKEX/SSE 来源入口，并补充 Baidu、Alibaba、Meta 的小型官方业绩核验语料；实现本地证据定位、pending 事实审核、收入/利润/现金流/估值机械计算、可比矩阵、事件时间线与带来源研究备忘录，重点处理财年、单位、会计准则、非 GAAP 和缺失值不可比问题。

## 中文版：金融研究/投研岗位两行版

针对 TMT 研究中的材料分散、指标口径不一致与结论难复核问题，独立设计并实现本地优先研究工作台，覆盖 AI 基础设施/半导体、云/SaaS、平台/电商/广告三类赛道及 9 家中美公司。  
建立来源注册表、指标状态、证据覆盖、事件直接事实/推断分层与本地备忘录导出流程；明确演示数据、官方来源和待核验问题边界，不将模型输出或缺失值冒充金融事实。

## 英文版：AI Product Resume Bullet

Built TMT Atlas, a local-first TMT research workbench with local PDF/CSV/JSON/TXT evidence ingestion, page/line/row provenance, SQLite fact review and audit, deterministic financial calculations, sector comparisons, event analysis, and memo export; added an explicit-consent OpenAI-compatible draft adapter for user-selected evidence and an authorized RSS/Atom/JSON metadata collector with a review inbox.

## 英文版：Finance Resume Bullet

Independently designed and implemented a TMT research workbench covering 9 US/China demo companies across AI infrastructure/semiconductors, cloud/SaaS, and platform/e-commerce/advertising; maintained official IR/SEC/HKEX/SSE source entry points, added a small curated official-facts corpus, and built evidence review, deterministic finance calculations, event timelines, comparability warnings, and provenance-aware memo exports.

## 当前最诚信的项目状态表述

可以写：

- 已完成可本地启动的 MVP；
- 已实现无 LLM 的核心研究工作流；
- 已实现本地证据导入、pending 事实审核和确定性金融计算；
- 已建立来源注册表、证据状态和演示数据边界；
- 已实现可选 OpenAI-compatible LLM 入口和显式同意机制；
- 已实现授权 Feed 元数据收集和 review-first 审阅箱；
- 已建立核心行为测试和后续评测方案。

目前不要写：

- 提升研究效率 30%/50%。
- 真实用户数、留存率或日活。
- 实时覆盖全市场资讯。
- 金融分析准确率达到某个百分比。
- 自动生成可靠投资建议。
- 已通过生产级高并发或企业安全认证。
- 已获得 GitHub 高赞或大量开源用户。

## 面试中根据真实测试结果补充的位置

完成用户试用后，可以把下面的占位替换成事实：

> 邀请 [人数] 名用户完成 [任务]，首次成功时间中位数为 [时间]，任务完成率为 [比例]；根据 [具体反馈]，我将 [功能] 从 [原方案] 改为 [新方案]。

完成真实材料评测后，可以补充：

> 在 [样例数量] 个固定用例上，核心计算通过率为 [比例]，关键结论引用覆盖率为 [比例]，无证据拒答率为 [比例]；主要失败案例是 [案例]，因此增加了 [改动]。
