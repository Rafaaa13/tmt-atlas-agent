# TMT Atlas Agent

> Official historical facts · Controlled Agent plans · Deterministic finance · Optional evidence-grounded LLM

TMT Atlas Agent is a local-first TMT research workbench. It connects source-backed historical company facts, evidence review, deterministic financial calculations, controlled Agent plans, authorized feed collection, company snapshots, sector comparability, event triage, and memo drafts.

It is not an auto-trading system, investment adviser, real-time market terminal, or autonomous researcher that silently approves facts.

## What changed from the demo version

The original UI used clearly labelled illustrative demo values. This upgrade switches the active product path to `official_historical` mode using `data/official_facts.json`, a curated source-backed corpus for nine companies: NVIDIA, SMIC, Microsoft, Alibaba, Baidu, Amazon, Alphabet, Meta, and Tencent.

Every displayed metric is one of:

- Official direct disclosure.
- Auditable derivation from official facts with a visible formula.
- Missing / undisclosed, with an explicit reason.

The product does not fill gaps with estimates, search snippets, currency conversion, or model-generated numbers.

## Run locally

Requirements: Python 3.10+. The deterministic core uses the Python standard library; no Docker, Node, database server, or LLM API key is required.

```bash
cd tmt-atlas-agent
python3 server.py
```

Open <http://127.0.0.1:8765>.

Run the full local test suite:

```bash
python3 -m unittest discover -s tests -v
```

Current upgrade test baseline: **151 tests passing**.

## Product surfaces

The sidebar contains:

- `研究总览` — official historical data mode, company count, official fact count, and workflow entry points.
- `公司快照` — direct/derived/missing metrics, reporting period, currency/unit, source URL, and locator.
- `赛道扫描` — eligible ranks, non-comparable rows, missing rows, and no automatic currency conversion.
- `事件雷达` — event facts, possible transmission paths, inferences, and open questions.
- `研究备忘录` — deterministic official-fact memo draft with source links.
- `证据审核` — local PDF/CSV/JSON/Markdown/TXT ingestion, source locators, pending facts, audit and review.
- `金融计算` — growth, margin, CAGR, FCF, enterprise value, multiples, and simplified DCF.
- `授权收集` — low-level feed configuration and review-first metadata collection.
- `Agent 自动化` — approveable plans, runs, tasks, retrieval review gates, and draft artifacts.

## Official historical facts

The primary corpus is:

```text
data/official_facts.json
```

Each fact includes company, metric, value, unit, currency, period, accounting basis, scope, source URL, source title, publication date, and a locator. Non-GAAP/non-IFRS measures retain their explicit status. Official pages being publicly accessible does not grant unlimited redistribution rights; the repository stores structured facts and source links, not complete restricted reports.

Some metrics remain missing because a defensible primary-source fact was not available in the curated corpus. This is intentional. A missing value is not zero.

## Controlled Agent workflow

Agent plans are persisted locally by `agent_orchestrator.py`:

```text
draft → approve → scheduled/manual run → draft artifact or review gate
```

Supported plan kinds:

- `collection_cycle` — only explicitly scoped and authorized feeds; run is now scoped with no global-feed leakage.
- `company_snapshot_refresh` — deterministic official snapshot artifact.
- `sector_scan` — deterministic comparability artifact.
- `event_triage` — deterministic event draft.
- `memo_draft` — deterministic memo draft.

The scheduler runs only while the local server is open. A feed item remains metadata-only until review. Accepting a retrieval item does not approve a financial fact. Agent draft artifacts are now retrievable and reviewable without changing the fact store.

Relevant API routes include:

```text
GET  /api/agent/plans
POST /api/agent/plans
POST /api/agent/plans/{id}/approve
POST /api/agent/plans/{id}/run
GET  /api/agent/runs
GET  /api/agent/runs/{id}/tasks
GET  /api/agent/artifacts
POST /api/agent/artifacts/{id}/review
GET  /api/agent/review-gates
GET  /api/agent/retrieval-items
```

## Optional LLM enhancement

The controlled Agent does not require an LLM. The optional `llm_research.py` module adds evidence-grounded language drafts only:

- Explicit settings and per-request consent are required.
- API key is read only from `TMT_LLM_API_KEY`.
- The user supplies at most 12 selected evidence items.
- Every claim must cite a supplied evidence ID.
- New numeric tokens, dates, or unknown citations are rejected.
- The LLM result is a draft and never writes or approves facts.
- An unavailable or disabled LLM leaves the deterministic local artifact intact.

The legacy `/api/llm/draft` endpoint remains for compatibility. New structured research integrations can use `/api/llm/enhance`.

## Source and data boundaries

- Official facts are historical and source-backed, not live quotes.
- RSS/Atom/JSON collection stores metadata and links; it does not copy full articles or automatically convert headlines into financial facts.
- Users must have permission to access and process local materials.
- Do not commit credentials, private internship files, personal information, commercial database exports, or local runtime state.
- This tool is not financial advice.

## Repository layout

```text
server.py                 # local API, official snapshot/sector handlers, Agent APIs
official_data.py          # source-backed facts → direct/derived/missing view models
agent_orchestrator.py     # persisted plans, runs, tasks, review gates, draft artifacts
connectors.py             # scoped authorized RSS/Atom/JSON Feed collector
llm_research.py           # opt-in citation-checked LLM draft enhancement
workspace_db.py           # local SQLite sources/evidence/facts/audit
finance_engine.py         # deterministic finance calculations
data/official_facts.json  # official historical corpus
data/demo_seed.json       # company metadata and legacy workflow fixtures
frontend/                 # workbench and Agent UI
tests/                    # 151 local tests
```

## Interview framing

> I upgraded a TMT research workbench from a demo-data dashboard into an official historical-facts workflow. The product now distinguishes direct disclosures, auditable derived ratios, and unavailable fields; it does not hide missing data behind estimates. A controlled Agent plan can schedule authorized metadata collection or produce snapshot, sector, event, and memo drafts, but every state-changing step has a review gate. An optional LLM can improve language from selected evidence only; it cannot write or approve financial facts.

See `docs/UPGRADE-OVERVIEW.md`, `docs/INTERVIEW-GUIDE.md`, `docs/RESUME-VERSIONS.md`, and `docs/DATA-VERIFICATION-LOG.md` for the full product and interview story.

## License

Code is MIT-licensed. Company filings, IR pages, feeds, and third-party materials remain subject to their own terms.
