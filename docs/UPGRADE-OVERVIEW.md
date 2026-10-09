# TMT Atlas Agent｜升级版说明

## 这次升级新增了什么

`tmt-atlas-agent` 是 TMT Atlas 的升级版独立仓库目录。它在原有本地研究工作台基础上，加入了官方历史事实、受控 Agent 编排、可选 LLM 研究草稿和更严格的数据状态边界。

升级后的主链路是：

```text
官方来源事实
  → 公司快照
  → 可比性检查
  → 赛道扫描
  → 资讯收集
  → 事件候选
  → 人工审核门
  → 研究备忘录草稿
```

## 官方事实模式

当前页面默认进入 `official_historical` 模式，不再把 `demo_seed.json` 中的数字当作主数据。

官方事实 corpus 位于：

```text
data/official_facts.json
```

覆盖 9 家公司：

- NVIDIA
- SMIC
- Microsoft
- Alibaba
- Baidu
- Amazon
- Alphabet
- Meta
- Tencent

事实来自官方业绩公告、SEC 10-K/6-K、公司 IR 或发行人正式公告，字段包含：

- 期间
- 单位
- 币种
- 会计口径
- 合并范围
- 来源 URL
- 来源标题
- 发布日期
- 报告定位说明
- 直接披露/非 GAAP/派生状态

产品中明确区分三种状态：

- `官方直接披露`
- `官方事实派生`
- `未披露`

无法核验的字段不会用估算数字补齐。例如 Azure、云业务占比、研发费用率或跨币种可比增长率，在没有符合条件的官方事实时会显示为“未披露”。

## 公司快照

公司快照不再直接读取演示指标。它现在展示：

- 官方历史事实数量
- 指标状态
- 报告期
- 币种和单位
- 会计口径
- 来源 URL
- 来源定位
- 计算公式
- 派生值所使用的输入事实
- 缺失原因

示例：营业利润率只有在营业利润和收入具有相同币种、期间、会计口径、合并范围和单位时才会派生。

## 赛道扫描

赛道扫描现在拆成：

- 可比较且可以排名的公司
- 由于报告期、币种、口径或范围不同而不可比的公司
- 缺少官方数据的公司

系统不会：

- 把缺失值当作 0
- 自动做人民币/美元转换
- 把不同财年混成同一期间
- 把单家公司伪装成排名组
- 用模型生成的数字参加排名

## Agent 自动化

新增 Agent 编排器位于：

```text
agent_orchestrator.py
```

支持五类计划：

- `collection_cycle`：授权 RSS/Atom/JSON Feed 元数据收集
- `company_snapshot_refresh`：生成官方事实公司快照草稿
- `sector_scan`：生成可比性检查和赛道扫描草稿
- `event_triage`：生成事件分诊草稿
- `memo_draft`：生成研究备忘录草稿

Agent 的状态链是：

```text
draft → approved → active → run → draft artifact / waiting_review
```

Agent 不会自动批准金融事实。定期收集只保存标题、日期、链接等资讯元数据；任何事实都必须回到来源、证据和审核台。

当前调度器只有在本地服务运行期间工作，应用关闭时不会在后台继续执行。

## LLM 可选增强

升级版增加了更严格的 `llm_research.py` 接口：

- 需要开启 LLM 设置
- 需要用户明确同意
- 只发送用户选择的证据片段
- 最多 12 条证据，合计上下文有限制
- 模型返回必须是结构化 JSON
- 每条 claim 必须引用已发送的 evidence ID
- 新数字、日期或未知引用会被拒绝
- LLM 只生成未验证草稿，不写入事实表
- API key 只读取 `TMT_LLM_API_KEY`
- 默认拒绝不安全端点和私网地址
- 本地 Ollama 需要明确 `allow_local_llm=true`

LLM 是表达和研究效率增强，不是金融数字计算器，也不是自动事实审核器。

## 运行

```bash
cd tmt-atlas-agent
python3 server.py
```

打开：

```text
http://127.0.0.1:8765
```

运行完整测试：

```bash
python3 -m unittest discover -s tests -v
```

当前升级版最后一次本地测试结果：

```text
Ran 144 tests
OK
```

## GitHub 仓库

升级版目标仓库：

```text
https://github.com/Rafaaa13/tmt-atlas-agent.git
```

当前本地目录与旧版 `TMT-金融Agent` 分离，不会覆盖旧仓库。

如果你的 GitHub 仓库还没有创建：

1. 打开 <https://github.com/new>。
2. Repository name 填 `tmt-atlas-agent`。
3. Owner 选择 `Rafaaa13`。
4. 选择 Public。
5. 不要自动添加 README、`.gitignore` 或 License。
6. 在本目录执行 Git 初始化和推送命令。

## 重要数据许可说明

官方网页可以公开访问，不代表可以无限制复制、镜像或再分发。仓库保存来源链接、必要的事实字段和定位说明，不打包完整受限报告。真实材料、公司内部文件、商业数据库内容和 API key 不应放进公开仓库。

## 面试时的准确表述

> 我把 TMT Atlas 从演示数据工作台升级成了官方历史事实驱动的研究工作台。系统对 9 家 TMT 公司维护带来源和定位说明的历史事实，并把直接披露、公式派生和未披露字段分开。上层通过受控 Agent 计划执行授权资讯收集、公司快照刷新、赛道扫描、事件分诊和备忘录草稿；Agent 负责编排，不绕过事实审核。LLM 只是用户明确同意后的证据草稿增强，不能写入事实或生成无法追溯的金融数字。
