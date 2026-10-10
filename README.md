# TMT Atlas Agent

**A local-first research workbench for TMT analysts. It takes one research task from a news item to a sourced memo, and ties every number to an official disclosure.**

English | [简体中文](README.zh-CN.md)

TMT Atlas Agent is not an auto-trading system, an investment adviser, a real-time market terminal, or an autonomous researcher that approves facts on its own. The analyst writes the conclusion.

## Contents

- [The research chain](#the-research-chain)
- [Try the whole chain in ten minutes](#try-the-whole-chain-in-ten-minutes)
- [Feature guide](#feature-guide)
- [Data model](#data-model)
- [Run locally](#run-locally)
- [API overview](#api-overview)
- [Repository layout](#repository-layout)
- [Current limitations](#current-limitations)

## The research chain

Every main page of the app is one step of the same chain. The table below is the path of one study, in the order I walk through it in the demo. The sidebar groups the same pages differently, with collection under the local tools.

| Step | Page in the app | What you do | What the product guarantees |
|---|---|---|---|
| 1. Collect news | 授权收集 (Authorised collection) | Enter a feed you are allowed to read, tick the authorisation box, run once | Only that feed is contacted. Only title, date and link are stored. Every item starts as `pending` and never becomes a financial fact by itself. |
| 2. Scan a sector | 赛道扫描 (Sector scan) | Pick a sector | Companies share a ranking only when currency, period, accounting standard, consolidation level and unit all match. The rest read "not comparable". No FX conversion. |
| 3. Read an event | 事件雷达 (Event radar) | Open an event | Direct facts, transmission paths, inferences and open questions are kept apart. Inferences are marked "must be confirmed by a person". |
| 4. Check the company | 公司快照 (Company snapshot) | Pick a company | Each metric is `direct`, `derived` (formula and inputs shown) or `missing` (reason shown). |
| 5. Draft the memo | 研究备忘录 (Research memo) | Pick a snapshot, a sector and an event, add notes | Facts carry period and source. Gaps read "not disclosed". Export as Markdown or HTML. |
| 6. Make the judgment | "Research conclusion" in the memo | Write it yourself | The product gives no rating and no buy/sell call. |

Three more pages sit beside the chain and are covered in the [feature guide](#feature-guide):

- 证据审核 (Evidence review): turn your own PDF, CSV, JSON, Markdown or text files into located evidence and review proposed facts.
- 金融计算 (Finance calculator): growth, margins, CAGR, unit conversion, valuation multiples, free cash flow and a simplified DCF, all with visible inputs and formulas.
- Agent 自动化 (Agent automation): approve a plan once and let it refresh a snapshot, run a sector scan, triage an event, draft a memo or collect authorised news. Results are always drafts.

## Try the whole chain in ten minutes

Start the server (see [Run locally](#run-locally)), open <http://127.0.0.1:8765>, then follow the six steps. The numbers below come from the bundled official-facts corpus, so you can check them against your own screen.

1. **Collect news.** Open 授权收集. Enter `https://nvidianews.nvidia.com/releases.xml`, tick both checkboxes (enable, and confirm you may use the source), click 保存授权配置, then 现在运行一次. The inbox fills with `pending` items from NVIDIA's newsroom. The count depends on the feed on the day you run it.
2. **Scan a sector.** Open 赛道扫描 and choose 平台/电商/广告. Meta (22.2%), Alphabet (15.1%) and Amazon (12.4%) are ranked 1 to 3, because all three report in USD for calendar 2025 under US GAAP on a consolidated basis. Tencent (13.9%) reports in RMB under IFRS, so it stays in the table as 不可比较 without a rank.
3. **Read an event.** Open 事件雷达 and select 出口管制与供应链风险 (NVIDIA). The transmission path quotes revenue growth 114.2%, capex USD 3,236 million and operating margin 62.4% from the snapshot, and warns that a headline alone cannot tell which way the margin moves.
4. **Check the company.** Open 公司快照 and choose NVIDIA. Revenue is USD 130,497 million (FY2025, US GAAP, `direct`). Revenue growth 114.2% is `derived` and shows its formula, (130,497 − 60,922) ÷ 60,922. The R&D ratio is `missing`, with the reason printed on the card.
5. **Draft the memo.** Open 研究备忘录, choose NVIDIA, 平台/电商/广告 and the export-control event in the three drop-downs, and click 生成本地备忘录. Type your own notes into 研究员备注 first if you want them to appear under 研究结论.
6. **Make the judgment.** Read the memo, decide what the export controls mean for NVIDIA's growth, and write that in the conclusion. Nothing in the product does this for you.

To see the Agent layer, open Agent 自动化, create a 公司快照刷新 plan for NVIDIA, approve it, and click 立即运行. The result appears under 研究草稿产物 as a draft marked "facts approved: no".

## Feature guide

### 1. Authorised news collection

- Accepts RSS, Atom or JSON Feed URLs. Only HTTPS and public hosts are allowed. Private or loopback addresses, URLs with credentials and redirect-style query parameters are rejected.
- Off by default. Nothing is contacted until you enable collection and tick the authorisation box. The minimum interval is 15 minutes, and the timer only runs while the local server is open.
- Stores the title, date, link and source name of each item. It never stores the article text.
- Each item lands in a review inbox as `pending`. 进入研究线索 marks it `accepted`, 忽略 marks it `rejected`. Both leave `facts_approved` at `false`: a news item is a lead for the analyst and never a fact.

### 2. Sector scan

The scan covers three sectors: AI infrastructure and semiconductors (NVIDIA, SMIC), cloud and SaaS (Microsoft, Alibaba, Baidu), and platform, e-commerce and advertising (Amazon, Alphabet, Meta, Tencent). The table shows revenue, revenue growth, gross margin, operating margin, R&D ratio, capex and a comparability label for each company.

The ranking rule is strict on purpose. Companies are grouped by currency, reporting period, period type, base accounting standard, consolidation level and unit. A group with two or more companies is ranked by revenue growth. A company that is alone in its group is shown as 不可比较. A company with no growth figure is shown as missing. "Not disclosed" is never treated as zero, and no currency is converted.

Today only the platform sector forms a rank group (the three US companies). The other two sectors list companies with different fiscal year-ends or accounting bases, so the scan shows no ranking there. That is the intended behaviour, not a gap in the data.

### 3. Event radar

Each event page splits a single event into four blocks:

- **Direct facts:** subject, date, event type and source status, only what the source confirms.
- **Possible transmission paths:** the path quotes live numbers from the company snapshot (revenue growth, capex, operating margin) and states what still has to be confirmed.
- **Analytical inferences:** kept in their own block and marked "must be confirmed by a person".
- **Open questions:** for example whether the revenue of the affected business is disclosed.

The ten events that ship with the app are examples. They carry a 演示事件 badge and show 0% evidence coverage so nobody mistakes them for sourced news.

### 4. Company snapshot

A snapshot shows 10 to 12 metric cards per company, depending on what the company discloses. The core ones are total revenue, revenue growth, gross margin, operating margin, R&D ratio, capex, core-segment share and operating income; operating cash flow, net income and segment revenue follow where available. Every card carries one of three states:

| State | Badge | Meaning |
|---|---|---|
| `direct` | 官方直接披露 | Read straight from an official disclosure, with period, currency, accounting standard, source title and the table or row it came from |
| `derived` | 官方事实派生 | Calculated from official facts; the formula and its inputs are printed on the card |
| `missing` | 未披露 | No same-basis official fact is available; the card states why and nothing is estimated |

The 加入研究备忘录 button sends the snapshot to the memo builder.

### 5. Research memo

The memo builder takes an optional snapshot, an optional sector and an optional event, plus your notes. It writes a draft with these sections, in this order:

1. 研究结论 (research conclusion): your notes. If you wrote none, one reminder line appears and the system draws no conclusion.
2. 关键事实与计算 (key facts and calculations): one line per metric with period, state and source title; missing values read 未披露 with the reason. The sector comparison and the event analysis, if you chose them, follow as sub-sections here. The event analysis lists the direct facts and the open questions.
3. 研究边界 (research boundaries) and 来源 (sources) with links.

Export the result as Markdown or HTML. The memo is produced by templates and works without any language model.

### 6. Evidence review

For material you bring yourself, the evidence page keeps the chain from file to fact:

- Ingest a local PDF, CSV, JSON, Markdown or text file. Each snippet keeps its location: page for PDF, line for text, row for CSV. PDF parsing needs the optional `pdfplumber` or `pypdfium2` packages, and scanned PDFs can use optional local OCR.
- Search the snippets, then propose a fact from one. A proposed fact starts as `pending`.
- Accept or reject a fact with a written reason. Two facts that disagree on the same metric are flagged 需核验 instead of one silently replacing the other.
- Sources, evidence, facts and every review decision are stored in a local SQLite file, which gives an audit trail.

### 7. Finance calculator

All arithmetic runs in plain Python with visible inputs, formulas and warnings. It refuses to guess: a zero denominator, a missing value or mismatched units blocks the result and says why, and unusual values such as negative revenue raise a warning.

Operations: growth, margin, CAGR, unit conversion, comparability check, enterprise value, EV/revenue, P/E, free cash flow, and a simplified DCF with sensitivity tables and scenarios.

### 8. Agent automation

An Agent plan is a research task you approve once. Five kinds exist:

| Kind | What a run produces |
|---|---|
| `company_snapshot_refresh` | A draft snapshot for one company, built from the official facts |
| `sector_scan` | A draft scan with the comparability result for one sector |
| `event_triage` | A draft event breakdown |
| `memo_draft` | A draft memo from a company, sector or event |
| `collection_cycle` | New items from feeds you listed and authorised, held for review |

A plan moves through `draft → approved → running on a schedule (or run by hand) → draft artifact or review gate`. The interval is at least 15 minutes, a plan can be paused and resumed, and the scheduler only runs while the local server is open. Each artifact waits as 待确认 until you accept or reject it with a written reason, and accepting a draft never approves a financial fact. A collection plan only reaches feeds named in its own scope.

### 9. Optional language model

The language model is off by default and nothing in the chain above needs it. When you switch it on, it can turn evidence you selected into a draft paragraph:

- It needs the setting switched on and your consent on each request. The API key is read only from the `TMT_LLM_API_KEY` environment variable and is never written to disk.
- It receives at most 12 evidence items that you ticked, and nothing else.
- Every claim in the answer must cite an ID of an evidence item that was sent. A new number, a new date or an unknown citation gets the whole answer rejected.
- The result is a draft. It cannot write or approve a fact, and if the model is unavailable the rule-based output stays as it was.
- Endpoints on private networks are refused. A local Ollama server needs an explicit `allow_local_llm` setting.

## Data model

The bundled corpus is `data/official_facts.json`: 60 facts for nine companies (NVIDIA, SMIC, Microsoft, Alibaba, Baidu, Amazon, Alphabet, Meta, Tencent), taken from earnings releases, 10-K and 6-K filings, investor relations pages and issuer announcements. Each fact records the company, metric, value, prior-period value where used, unit, currency, period, accounting standard, scope, source URL, source title, publication date and a locator for the row or table it came from.

Three rules shape everything the app displays:

- A value is `direct`, `derived` or `missing`. There is no fourth "estimated" state.
- A derived value is only computed from facts that share currency, period, accounting standard, scope and unit.
- Public pages are linked, not copied. The repository stores structured facts and source links, not whole restricted reports.

The numbers on screen come from `official_facts.json`. `data/demo_seed.json` only holds company metadata, the ten example events and the sample prompts.

## Run locally

Requirements: Python 3.10 or newer. The core uses only the standard library. It needs no Docker, no Node and no database server, and it needs no API key.

```bash
git clone https://github.com/Rafaaa13/tmt-atlas-agent.git
cd tmt-atlas-agent
python3 server.py
```

Open <http://127.0.0.1:8765>. Run the test suite with:

```bash
python3 -m unittest discover -s tests -v
```

The current baseline is **152 tests passing**.

| Variable | Purpose | Default |
|---|---|---|
| `TMT_HOST` | Address the server binds to | `127.0.0.1` |
| `TMT_PORT` | Port | `8765` |
| `TMT_LLM_API_KEY` | Key for the optional language model | empty |
| `TMT_LLM_BASE_URL`, `TMT_LLM_MODEL` | Endpoint and model for the optional language model | empty |

**Security note:** the server has no login. It binds to `127.0.0.1` so only your own machine can reach it. Do not set `TMT_HOST` to a public address, and do not put the server behind a public port, without adding authentication first.

## API overview

All routes are local JSON endpoints served by `server.py`.

| Area | Routes |
|---|---|
| Research views | `GET /api/companies/{id}/snapshot`, `GET /api/sectors/{sector}`, `GET /api/events`, `GET /api/events/{id}`, `POST /api/memos` |
| Evidence and facts | `POST /api/ingest`, `GET /api/evidence`, `GET /api/facts`, `POST /api/facts`, `POST /api/facts/review`, `GET /api/audit` |
| Calculation | `POST /api/calculate` |
| Collection | `POST /api/collection/config`, `POST /api/collection/run`, `GET /api/collection/inbox`, `POST /api/collection/review` |
| Agent | `GET/POST /api/agent/plans`, `POST /api/agent/plans/{id}/approve`, `.../pause`, `.../resume`, `.../run`, `GET /api/agent/runs`, `GET /api/agent/artifacts`, `POST /api/agent/artifacts/{id}/review`, `GET /api/agent/review-gates`, `GET /api/agent/retrieval-items` |
| Language model | `POST /api/settings/llm`, `POST /api/llm/enhance`, `POST /api/llm/draft` (older route) |

## Repository layout

```text
server.py                 # local server, research views, API routes
official_data.py          # official facts -> direct / derived / missing view models
finance_engine.py         # growth, margins, CAGR, valuation, DCF
evidence_engine.py        # local file ingestion and located evidence
workspace_db.py           # SQLite store for sources, evidence, facts, audit
connectors.py             # authorised feed collector with review inbox
agent_orchestrator.py     # plans, runs, tasks, review gates, draft artifacts
llm_research.py           # optional, citation-checked language-model drafts
data/official_facts.json  # the official-facts corpus (60 facts, 9 companies)
data/demo_seed.json       # company metadata, example events, sample prompts
frontend/                 # the workbench pages
tests/                    # 152 tests
docs/                     # architecture, product, evaluation and interview notes
```

## Current limitations

- The data is historical and already disclosed. It is not a live quote feed and is not refreshed automatically.
- The ten events in the event radar are examples. Items accepted from the collection inbox are marked `accepted`, but nothing yet carries them into the event radar. Wiring the two together is the next step for this chain.
- Collection and the Agent scheduler run only while the local server is open.
- The sector scan is strict by design, so most sectors show few or no ranked companies. Cross-currency comparison would need a dated, sourced exchange rate, which the product does not use.
- The product gives no ratings, price targets or trading signals.
- Whole filings are not bundled. Public pages are linked, and you are responsible for having the right to read and process any file or feed you add.

## More documents

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/PRODUCT-OVERVIEW.md`](docs/PRODUCT-OVERVIEW.md), [`docs/EVALUATION.md`](docs/EVALUATION.md), [`docs/DATA-SOURCES.md`](docs/DATA-SOURCES.md) and [`docs/DATA-VERIFICATION-LOG.md`](docs/DATA-VERIFICATION-LOG.md) go deeper on design, data and checks.

## License

The code is MIT-licensed. Company filings, investor relations pages, feeds and other third-party materials keep their own terms.
