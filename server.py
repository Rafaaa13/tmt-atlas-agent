#!/usr/bin/env python3
"""TMT Atlas local-first research workbench.

Zero-dependency local server for the portfolio MVP. The demo intentionally works
without an LLM, database server, Docker, or network access.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import math
import os
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import finance_engine
from agent_orchestrator import AgentOrchestrator, AgentOrchestratorError
from connectors import Collector
from llm_research import enhance as enhance_llm_research
from official_data import OfficialSnapshotBuilder
from workspace_db import MAX_SOURCE_BYTES, WorkspaceDB

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
FRONTEND_DIR = ROOT / "frontend"
SEED_PATH = DATA_DIR / "demo_seed.json"
SOURCES_PATH = DATA_DIR / "source_registry.json"
SETTINGS_PATH = DATA_DIR / "local_settings.json"
LOCAL_SOURCES_PATH = DATA_DIR / "local_sources.json"
VERIFIED_FACTS_PATH = DATA_DIR / "verified_facts.json"  # Legacy import corpus only.
OFFICIAL_FACTS_PATH = DATA_DIR / "official_facts.json"
MAX_JSON_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BYTES = ((MAX_SOURCE_BYTES + 2) // 3) * 4 + 16 * 1024
VERIFIED_FACT_STATUSES = frozenset({"verified", "verified_non_gaap", "manual_primary_source"})
OFFICIAL_FACT_STATUS_ALIASES = frozenset({
    "official", "source-backed", "source_backed", "official_historical",
})
RATIO_METRIC_KEYS = frozenset({"revenue_growth", "gross_margin", "operating_margin", "rd_ratio"})
RATIO_INPUT_KEYS = {
    "gross_margin": (("gross_profit",), ("revenue",)),
    "operating_margin": (("operating_income", "operating_profit"), ("revenue",)),
    "rd_ratio": (("research_and_development", "research_and_development_expense", "rd_expense", "r_and_d"), ("revenue",)),
}

# These are application-owned state directories.  Request payloads never choose
# a filesystem destination; callers may only override them through deployment
# environment variables.
STATE_DIR = Path(os.environ.get("TMT_STATE_DIR", str(DATA_DIR / "workspace_state"))).expanduser()
COLLECTOR_STATE_DIR = Path(
    os.environ.get("TMT_COLLECTOR_STATE_DIR", str(DATA_DIR / "collector_state"))
).expanduser()
AGENT_STATE_DIR = Path(
    os.environ.get("TMT_AGENT_STATE_DIR", str(STATE_DIR / "agent_state"))
).expanduser()
WORKSPACE_DB = WorkspaceDB(STATE_DIR)
COLLECTOR = Collector(COLLECTOR_STATE_DIR)


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def decode_upload_base64(value: Any) -> bytes:
    """Decode an upload using strict standard Base64 validation."""
    if not isinstance(value, str):
        raise ValueError("invalid_upload")
    try:
        encoded = value.encode("ascii")
        if len(encoded) > ((MAX_SOURCE_BYTES + 2) // 3) * 4:
            raise ValueError("invalid_upload")
        return base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError, TypeError):
        raise ValueError("invalid_upload")


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load_official_facts() -> list[dict[str, Any]]:
    """Load the source-backed historical corpus, never the demo metrics.

    The persisted corpus is normally a mapping with a ``facts`` list, but the
    loader also accepts a bare list for compatibility with older local exports.
    Statuses are retained verbatim when they are part of the official contract,
    including ``verified_non_gaap``; they are never silently downgraded to a
    demo or fabricated status.
    """
    raw = load_json(OFFICIAL_FACTS_PATH, None)
    if raw is None:
        raw = load_json(VERIFIED_FACTS_PATH, [])
    if isinstance(raw, dict):
        candidate = raw.get("facts", raw.get("data", []))
        if isinstance(candidate, dict):
            flattened: list[dict[str, Any]] = []
            for company_id, values in candidate.items():
                values = values if isinstance(values, list) else [values]
                for value in values:
                    if isinstance(value, dict):
                        item = dict(value)
                        item.setdefault("company_id", company_id)
                        flattened.append(item)
            raw = flattened
        else:
            raw = candidate
    if not isinstance(raw, list):
        return []

    accepted_statuses = VERIFIED_FACT_STATUSES | OFFICIAL_FACT_STATUS_ALIASES
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        status = item.get("status", "manual_primary_source")
        if not isinstance(status, str) or status not in accepted_statuses:
            continue
        fact = dict(item)
        # Preserve verified_non_gaap so the UI and agent artifacts can tell a
        # company-defined liquidity measure from a GAAP/IFRS direct fact.
        if status in OFFICIAL_FACT_STATUS_ALIASES:
            fact["status"] = "verified"
        else:
            fact["status"] = status
        source: dict[str, Any] = dict(item.get("source") or {}) if isinstance(item.get("source"), dict) else {}
        for flat, short in (("source_url", "url"), ("source_title", "title")):
            value = item.get(flat) or source.get(flat) or source.get(short)
            if value:
                fact[flat] = value
                source.update({flat: value, short: value})
        for key in ("published_at", "locator"):
            value = item.get(key) or source.get(key)
            if value:
                fact[key] = value
                source[key] = value
        fact["source"] = source
        result.append(fact)
    return result


def load_verified_facts() -> list[dict[str, Any]]:
    """Backward-compatible endpoint shape, now using the official corpus."""
    return load_official_facts()


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def number(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def display_metric(metric: dict[str, Any]) -> str:
    value = metric.get("value")
    unit = metric.get("unit", "")
    if unit == "%":
        return pct(value)
    if unit == "USD bn":
        return f"${number(value)}bn"
    return f"{number(value)} {unit}".strip()


def display_delta(value: Any, unit: str = "%") -> str:
    if value is None:
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if unit == "%":
        return f"{n * 100:+.1f}pp"
    return f"{n:+.1f}"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class AtlasStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.seed = load_json(SEED_PATH, {"dataset": {}, "companies": [], "events": [], "research_prompts": []})
        self.source_registry = load_json(SOURCES_PATH, {"sources": []})
        self.official_payload = load_json(OFFICIAL_FACTS_PATH, {})
        self.official_facts = load_official_facts()
        self.official_builder = OfficialSnapshotBuilder(self.official_facts)
        self.settings = load_json(SETTINGS_PATH, {
            "provider": "none",
            "base_url": "",
            "model": "",
            "allow_external_llm": False,
            "allow_scheduled_collection": False,
            "last_updated": None,
        })
        self.local_sources = load_json(LOCAL_SOURCES_PATH, [])

    @property
    def dataset(self) -> dict[str, Any]:
        official_meta: dict[str, Any] = {}
        if isinstance(self.official_payload, dict) and isinstance(self.official_payload.get("dataset"), dict):
            official_meta = dict(self.official_payload["dataset"])
        base = official_meta
        # Do not expose demo-seed metadata as the active dataset contract.
        for key in ("currency_convention", "period_convention"):
            base.pop(key, None)
        base.update({
            "name": base.get("name") if base.get("name") and "demo" not in str(base.get("name")).lower() else "TMT Atlas Official Historical Facts",
            "version": base.get("version") if base.get("version") and "demo" not in str(base.get("version")).lower() else "1.0.0",
            "status": "official_historical",
            "data_mode": "official_historical",
            "notice": "页面只展示官方历史事实及可审计派生值；缺失字段显示为未披露，不使用演示指标，不做币种转换。定期收集仅保存授权来源元数据，官方事实仍需证据审核。",
            "source_backed": True,
            "official_fact_count": len(self.official_facts),
        })
        return base

    @property
    def companies(self) -> list[dict[str, Any]]:
        return self.seed.get("companies", [])

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.seed.get("events", [])

    @property
    def all_sources(self) -> list[dict[str, Any]]:
        return self.source_registry.get("sources", []) + self.local_sources

    def company(self, company_id: str) -> dict[str, Any] | None:
        return next((c for c in self.companies if c.get("id") == company_id), None)

    def source(self, source_id: str) -> dict[str, Any] | None:
        return next((s for s in self.all_sources if s.get("id") == source_id), None)

    def event(self, event_id: str) -> dict[str, Any] | None:
        return next((e for e in self.events if e.get("id") == event_id), None)

    def sectors(self) -> list[str]:
        return sorted({c.get("sector", "") for c in self.companies if c.get("sector")})

    def _latest_official_fact(self, company_id: str, metric_keys: tuple[str, ...]) -> dict[str, Any] | None:
        wanted = set(metric_keys)
        candidates = [
            fact for fact in self.official_builder.facts_for(company_id)
            if str(fact.get("metric_key") or "") in wanted
            and not isinstance(fact.get("value"), bool)
            and _finite_number(fact.get("value"))
        ]
        candidates.sort(
            key=lambda fact: (str(fact.get("period_end") or ""), str(fact.get("source_url") or "")),
            reverse=True,
        )
        return candidates[0] if candidates else None

    @staticmethod
    def _ratio_dimensions_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
        # Ratios are only auditable when the two amounts share every reporting
        # dimension, including unit.  A same-currency USD million/USD billion
        # pair must not be treated as comparable by accident.
        fields = (
            "currency", "period_start", "period_end", "period_type",
            "accounting_basis", "scope", "unit",
        )
        values = []
        for fact in (left, right):
            row = tuple(fact.get(field) for field in fields)
            if any(value in (None, "") for value in row):
                return False
            values.append(row)
        return values[0] == values[1]

    def _direct_ratio(self, key: str, label: str, fact: dict[str, Any]) -> dict[str, Any]:
        metric = self.official_builder._direct(key, label, fact)
        metric["unit"] = "%"
        metric["display_value"] = pct(metric.get("value"))
        return metric

    def metric(self, company: dict[str, Any], key: str, label: str | None = None) -> dict[str, Any] | None:
        company_id = str(company.get("id") or "")
        requested = str(key)
        metric_label = label or ""
        # An explicitly disclosed ratio wins over a derivation.  This matters
        # for issuers such as NVIDIA and SMIC, where gross margin is reported
        # directly but gross profit is not present in the curated corpus.
        if requested in RATIO_METRIC_KEYS:
            direct = self._latest_official_fact(company_id, (requested,))
            if direct is not None:
                return self._direct_ratio(requested, metric_label or str(direct.get("label") or requested), direct)

        metric = self.official_builder.metric(company_id, requested, metric_label or None)
        if requested in RATIO_INPUT_KEYS:
            numerator_keys, denominator_keys = RATIO_INPUT_KEYS[requested]
            numerator = self._latest_official_fact(company_id, numerator_keys)
            denominator = self._latest_official_fact(company_id, denominator_keys)
            if numerator is not None and denominator is not None:
                if not self._ratio_dimensions_match(numerator, denominator):
                    return self.official_builder._missing(
                        requested,
                        str(metric.get("label") or metric_label or requested),
                        "inputs_not_comparable",
                    )
                # Use the actual disclosed metric key so aliases such as
                # Tencent's operating_profit can still produce an auditable
                # operating margin.
                return self.official_builder._derived_ratio(
                    key=requested,
                    label=str(metric.get("label") or metric_label or requested),
                    numerator_key=str(numerator.get("metric_key") or numerator_keys[0]),
                    denominator_key=str(denominator.get("metric_key") or denominator_keys[0]),
                    numerator=numerator,
                    denominator=denominator,
                )
        return metric

    def with_provenance(self, metric: dict[str, Any]) -> dict[str, Any]:
        """Attach UI provenance without replacing official values with seed data."""
        source = metric.get("source") if isinstance(metric.get("source"), dict) else {}
        result = dict(metric)
        source_id = metric.get("source_id")
        if not source_id and source.get("url"):
            source_id = "official-" + hashlib.sha256(str(source["url"]).encode("utf-8")).hexdigest()[:16]
        result.update({
            "source_id": source_id,
            "trace": {
                "status": metric.get("status", "missing"),
                "source_id": source_id,
                "evidence_locator": metric.get("source_locator"),
                "source_url": metric.get("source_url", ""),
            },
        })
        return result

    def snapshot(self, company_id: str) -> dict[str, Any] | None:
        company = self.company(company_id)
        if not company:
            return None
        built = self.official_builder.snapshot(company)
        metric_specs = list(self.official_builder.metric_keys(company))
        known_metric_identities = {
            "fcf" if key == "free_cash_flow" else "capex" if key == "capital_expenditures" else key
            for key, _label in metric_specs
        }
        # Surface every additional official fact (including segment facts) while
        # keeping seed aliases such as capex/free_cash_flow single-valued.
        for fact in self.official_builder.facts_for(company_id):
            fact_key = str(fact.get("metric_key") or "").strip()
            if not fact_key:
                continue
            identity = "fcf" if fact_key == "free_cash_flow" else "capex" if fact_key == "capital_expenditures" else fact_key
            if identity in known_metric_identities:
                continue
            metric_specs.append((fact_key, str(fact.get("label") or fact_key)))
            known_metric_identities.add(identity)
        raw_metrics: list[dict[str, Any]] = []
        for key, label in metric_specs:
            metric = self.metric(company, key, label)
            if metric is None:
                continue
            metric = dict(metric)
            metric["label"] = label or metric.get("label") or key
            raw_metrics.append(metric)
        # ``capex`` and ``capital_expenditures`` are aliases in the seed and
        # must render once, not as two apparent official metrics.
        metrics: list[dict[str, Any]] = []
        seen_metric_keys: set[str] = set()
        for metric in raw_metrics:
            identity = "capex" if metric.get("key") == "capital_expenditures" else str(metric.get("key"))
            if identity in seen_metric_keys:
                continue
            seen_metric_keys.add(identity)
            metrics.append(self.with_provenance(metric))
        events = [self.event_view(e) for e in self.events if e.get("company_id") == company_id]
        return {
            "company": {
                k: company.get(k)
                for k in ["id", "name", "name_zh", "ticker", "country", "listing", "sector", "description"]
            },
            "metrics": metrics,
            "events": sorted(events, key=lambda x: x.get("date", ""), reverse=True),
            "data_notice": self.dataset.get("notice"),
            "data_mode": "official_historical",
            "as_of": self.dataset.get("as_of"),
            "official_fact_count": built["official_fact_count"],
            "source_count": len({fact.get("source_url") for fact in built["official_facts"] if fact.get("source_url")}),
        }

    def event_view(self, event: dict[str, Any]) -> dict[str, Any]:
        company = self.company(event.get("company_id", "")) or {}
        source = self.source(event.get("source_id", "")) or {}
        return {
            **event,
            "company_name": company.get("name", "未知公司"),
            "company_name_zh": company.get("name_zh", ""),
            "source_title": source.get("title", "未配置来源"),
            "source_url": source.get("url", ""),
            "status_label": "演示事件" if event.get("demo_only") else "官方来源入口",
        }

    def sector_scan(self, sector: str) -> dict[str, Any]:
        selected = [c for c in self.companies if c.get("sector") == sector]
        keys = ["revenue", "revenue_growth", "gross_margin", "operating_margin", "rd_ratio", "capex"]
        rows: list[dict[str, Any]] = []
        for company in selected:
            row: dict[str, Any] = {
                "company_id": company["id"],
                "company": company["name"],
                "company_zh": company["name_zh"],
                "country": company["country"],
                "ticker": company["ticker"],
                "sector": company["sector"],
            }
            for key in keys:
                row[key] = self.with_provenance(self.metric(company, key))
            growth = row["revenue_growth"]
            row["comparison_basis"] = {
                "currency": growth.get("currency"),
                "period": growth.get("period"),
                "period_start": growth.get("period_start"),
                "period_end": growth.get("period_end"),
                "period_type": growth.get("period_type"),
                "accounting_basis": growth.get("accounting_basis"),
                "scope": growth.get("scope"),
                "unit": growth.get("unit"),
            }
            row["comparability"] = "missing" if growth.get("status") == "missing" else "pending"
            row["comparability_reason"] = growth.get("missing_reason") if growth.get("status") == "missing" else None
            rows.append(row)

        candidates = [row for row in rows if row["revenue_growth"].get("status") in {"direct", "derived"} and row["revenue_growth"].get("value") is not None]
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in candidates:
            metric = row["revenue_growth"]
            signature = (
                metric.get("currency"), metric.get("period"),
                metric.get("period_start"), metric.get("period_end"),
                metric.get("period_type"), metric.get("accounting_basis"),
                metric.get("scope"), metric.get("unit"),
            )
            grouped.setdefault(signature, []).append(row)

        eligible_rows: list[dict[str, Any]] = []
        not_comparable_rows: list[dict[str, Any]] = []
        rank_groups: list[dict[str, Any]] = []
        for signature, group in grouped.items():
            currency, period, period_start, period_end, period_type, basis, scope, unit = signature
            basis_label = {
                "currency": currency,
                "period": period,
                "period_start": period_start,
                "period_end": period_end,
                "period_type": period_type,
                "accounting_basis": basis,
                "scope": scope,
                "unit": unit,
            }
            if len(group) < 2:
                for row in group:
                    row["comparability"] = "not_comparable"
                    row["comparability_reason"] = "fewer_than_two_matching_facts"
                    row["rank_group"] = basis_label
                    not_comparable_rows.append(row)
                continue
            ordered = sorted(group, key=lambda row: float(row["revenue_growth"]["value"]), reverse=True)
            group_id = "|".join(str(value or "") for value in signature)
            for index, row in enumerate(ordered, 1):
                row["growth_rank"] = index
                row["rank_group"] = {"id": group_id, **basis_label}
                row["comparability"] = "eligible"
                row["comparability_reason"] = "matching_currency_period_basis_scope"
                eligible_rows.append(row)
            rank_groups.append({"id": group_id, "size": len(group), **basis_label, "company_ids": [row["company_id"] for row in ordered]})

        missing_rows = [row for row in rows if row["revenue_growth"].get("status") == "missing"]
        result_rows = eligible_rows + not_comparable_rows + missing_rows
        return {
            "sector": sector,
            "rows": result_rows,
            "eligible_rows": eligible_rows,
            "not_comparable_rows": not_comparable_rows,
            "missing_rows": missing_rows,
            "rank_groups": rank_groups,
            "columns": keys,
            "method": "只在币种、报告期、期间类型、会计口径、合并范围和单位完全一致且至少有两条收入增速事实时排名；缺失或不可比数据不参与排名，不做币种转换。",
            "as_of": self.dataset.get("as_of"),
            "data_mode": "official_historical",
            "official_fact_count": sum(self.official_builder.fact_count(str(company.get("id"))) for company in selected),
        }

    def event_analysis(self, event_id: str) -> dict[str, Any] | None:
        event = self.event(event_id)
        if not event:
            return None
        company = self.company(event.get("company_id", "")) or {}
        snapshot = self.snapshot(company.get("id", "")) or {}
        metrics = {m.get("key"): m for m in snapshot.get("metrics", [])}
        direct = [
            f"事件主体：{company.get('name_zh')}（{company.get('name')}）",
            f"事件日期：{event.get('date')}",
            f"事件类型：{event.get('type')}",
            f"来源状态：{'演示事件，需替换为官方段落证据' if event.get('demo_only') else '已关联官方来源入口'}",
        ]
        transmission = []
        if metrics.get("revenue_growth", {}).get("value") is not None:
            transmission.append(f"收入增速当前为 {metrics['revenue_growth']['display_value']}；需要进一步确认事件是否改变收入确认、需求或客户预算。")
        if metrics.get("capex", {}).get("value") is not None:
            transmission.append(f"资本开支当前为 {metrics['capex']['display_value']}；需要验证事件是否影响产能、算力投入或现金流压力。")
        if metrics.get("operating_margin"):
            transmission.append(f"营业利润率当前为 {metrics['operating_margin']['display_value']}；不能仅凭事件标题推断利润率方向。")
        return {
            "event": self.event_view(event),
            "direct_facts": direct,
            "transmission_paths": transmission,
            "inferences": [
                "事件影响需要通过业务、指标和时间窗口传导；当前输出是研究框架，不是投资建议。",
                "如果来源只提供相对增长率或管理层表述，不能反推绝对金额。",
            ],
            "questions": [
                "是否有官方文件披露了受影响业务的绝对规模？",
                "事件发生前后的报告期是否可以直接比较？",
                "是否存在反向证据、管理层澄清或不同口径的披露？",
                "哪些结论需要用户先确认后才能进入研究备忘录？",
            ],
            "evidence_coverage": 0 if event.get("demo_only") else None,
            "evidence_coverage_status": "未建立页码/段落证据覆盖率；演示事件需核验" if event.get("demo_only") else "仅有关联来源入口，尚未完成段落级覆盖率核验",        }

    def search(self, query: str) -> dict[str, Any]:
        q = query.strip().lower()
        if not q:
            return {"companies": [], "events": [], "sources": []}
        companies = []
        for c in self.companies:
            haystack = " ".join(str(c.get(k, "")) for k in ["name", "name_zh", "ticker", "sector", "description"]).lower()
            if q in haystack:
                companies.append({"id": c["id"], "name": c["name"], "name_zh": c["name_zh"], "sector": c["sector"], "type": "company"})
        events = []
        for e in self.events:
            company = self.company(e.get("company_id", "")) or {}
            haystack = " ".join(str(e.get(k, "")) for k in ["title", "summary", "type", "date"]).lower()
            haystack += " " + " ".join(str(company.get(k, "")) for k in ["name", "name_zh", "ticker", "sector"]).lower()
            if q in haystack:
                events.append({**self.event_view(e), "type": "event"})
        sources = []
        for s in self.all_sources:
            source_company = self.company(s.get("company_id", "")) or {}
            haystack = " ".join(str(s.get(k, "")) for k in ["title", "company", "sector", "type", "notes"]).lower()
            haystack += " " + " ".join(str(source_company.get(k, "")) for k in ["name", "name_zh", "ticker", "sector"]).lower()
            if q in haystack:
                sources.append({**s, "type": "source"})
        return {"companies": companies[:12], "events": events[:12], "sources": sources[:12]}

    def add_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title", "")).strip()
        url = str(payload.get("url", "")).strip()
        if not title or not url:
            raise ValueError("title 和 url 为必填项")
        source_id = "local-" + hashlib.sha1((title + url).encode("utf-8")).hexdigest()[:10]
        item = {
            "id": source_id,
            "company_id": payload.get("company_id", "local"),
            "company": payload.get("company", "本地来源"),
            "sector": payload.get("sector", "未分类"),
            "type": payload.get("type", "用户授权来源"),
            "title": title,
            "url": url,
            "access": "user_added",
            "notes": payload.get("notes", "由用户在本地工作区添加；正式使用前请确认权限与来源。"),
            "added_at": utc_now(),
        }
        with self.lock:
            self.local_sources = [s for s in self.local_sources if s.get("id") != source_id]
            self.local_sources.append(item)
            save_json(LOCAL_SOURCES_PATH, self.local_sources)
        return item

    def memo(self, company_id: str | None, sector: str | None, event_id: str | None, notes: str = "") -> dict[str, Any]:
        snapshot = self.snapshot(company_id) if company_id else None
        scan = self.sector_scan(sector) if sector else None
        analysis = self.event_analysis(event_id) if event_id else None
        title_parts = []
        if snapshot:
            title_parts.append(snapshot["company"]["name_zh"] + "公司快照")
        if scan:
            title_parts.append(scan["sector"] + "赛道扫描")
        if analysis:
            title_parts.append("事件影响分析")
        title = "｜".join(title_parts) or "TMT Atlas 研究备忘录"
        lines = [
            f"# {title}",
            "",
            f"> 数据截至：{self.dataset.get('as_of', '未标注')} · 状态：官方历史事实工作区，导出前需复核引用",
            "",
            "## 研究结论",
            notes.strip() or "当前备忘录由官方历史事实和可审计派生值生成。请在正式使用前回到来源定位核验关键结论。",
            "",
            "## 关键事实与计算",
        ]
        evidence_count = 0
        if snapshot:
            lines.append(f"### {snapshot['company']['name_zh']}（{snapshot['company']['name']}）")
            for metric in snapshot["metrics"][:7]:
                lines.append(f"- {metric['label']}：{metric['display_value']}（{metric['period']}，{metric['quality']}；来源：{metric['source_title']}）")
                evidence_count += 1
        if scan:
            lines.append(f"### {scan['sector']}比较")
            for row in scan["rows"]:
                growth = row.get("revenue_growth", {}).get("display_value", "未披露")
                margin = row.get("operating_margin", {}).get("display_value", "未披露")
                lines.append(f"- {row['company_zh']}：收入增速 {growth}；营业利润率 {margin}；仅在口径一致时比较，缺失或不可比项不参与排名。")
                evidence_count += 1
        if analysis:
            lines.extend(["### 事件分析", f"- {analysis['event']['title']}", "- 直接事实：" + "；".join(analysis["direct_facts"]), "- 待验证问题：" + "；".join(analysis["questions"])])
            evidence_count += 1
        lines.extend([
            "",
            "## 研究边界",
            "本材料用于展示证据组织、口径管理和研究工作流，不构成投资建议；任何正式结论都应回到官方文件原文核验。",
            "",
            "## 来源",
        ])
        source_ids: list[str] = []
        if snapshot:
            source_ids.extend(snapshot["company"].get("source_ids", []))
        if analysis:
            source_ids.append(analysis["event"].get("source_id", ""))
        seen: set[str] = set()
        for source_id in source_ids:
            if source_id in seen:
                continue
            seen.add(source_id)
            source = self.source(source_id)
            if source:
                url = str(source.get("url") or "")
                if url:
                    lines.append(f"- [{source['title']}]({url})")
        # Snapshot facts carry their own primary-source URLs and locators; do
        # not rely on the legacy seed registry for official memo citations.
        if snapshot:
            for metric in snapshot.get("metrics", []):
                url = str(metric.get("source_url") or "")
                if not url or url in seen:
                    continue
                seen.add(url)
                title_text = metric.get("source_title") or "Official source"
                lines.append(f"- [{title_text}]({url})")
        return {
            "title": title,
            "markdown": "\n".join(lines) + "\n",
            "html": render_markdownish_html(title, lines),
            "evidence_count": evidence_count,
            "coverage": None,
            "coverage_status": "备忘录当前保留来源入口和官方事实状态；段落级引用覆盖率需在事实审核后计算。",
            "generated_at": utc_now(),
        }


def render_markdownish_html(title: str, lines: list[str]) -> str:
    body: list[str] = []
    for line in lines:
        escaped = html.escape(line)
        if line.startswith("# "):
            body.append(f"<h1>{escaped[2:]}</h1>")
        elif line.startswith("## "):
            body.append(f"<h2>{escaped[3:]}</h2>")
        elif line.startswith("### "):
            body.append(f"<h3>{escaped[4:]}</h3>")
        elif line.startswith("- "):
            body.append(f"<li>{escaped[2:]}</li>")
        elif line.startswith("> "):
            body.append(f"<blockquote>{escaped[2:]}</blockquote>")
        elif line.strip():
            body.append(f"<p>{escaped}</p>")
    return "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>{}</title><style>body{{font-family:system-ui;max-width:860px;margin:40px auto;padding:0 24px;line-height:1.7;color:#17202a}}h1{{color:#0f3d3e}}blockquote{{background:#eef7f5;padding:12px 16px;border-left:4px solid #35a69a}}li{{margin:6px 0}}</style><body>{}</body></html>".format(html.escape(title), "".join(body))


STORE = AtlasStore()


def _draft_artifact(kind: str, result: Any, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Wrap deterministic research output as a reviewable draft artifact.

    Optional LLM enhancement is strictly additive and opt-in: the caller must
    set ``llm_consent`` and provide selected evidence context. It never changes
    the deterministic result or approves facts.
    """
    artifact: dict[str, Any] = {
        "artifact_type": kind,
        "status": "draft",
        "data_mode": "official_historical",
        "facts_approved": False,
        "review_required": True,
        "generated_at": utc_now(),
        "result": result,
    }
    request = payload or {}
    if request.get("llm_consent") is True and request.get("llm_context"):
        llm = enhance_llm_research(
            kind,
            request.get("llm_context"),
            STORE.settings,
            consent=True,
        )
        artifact["llm_enhancement"] = llm
        artifact["llm_is_additive"] = True
    return artifact


def _agent_company_snapshot(_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    company_id = str(payload.get("company_id") or "").strip()
    snapshot = STORE.snapshot(company_id) if company_id else None
    if snapshot is None:
        return _draft_artifact("company_snapshot_refresh", {"error": "company_not_found"})
    return _draft_artifact("company_snapshot_refresh", snapshot, payload=payload)


def _agent_sector_scan(_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    sector = str(payload.get("sector") or "").strip()
    scan = STORE.sector_scan(sector) if sector else {
        "sector": "",
        "rows": [],
        "eligible_rows": [],
        "not_comparable_rows": [],
        "missing_rows": [],
        "rank_groups": [],
        "data_mode": "official_historical",
    }
    return _draft_artifact("sector_scan", scan, payload=payload)


def _agent_event_triage(_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    event_id = str(payload.get("event_id") or "").strip()
    analysis = STORE.event_analysis(event_id) if event_id else None
    return _draft_artifact("event_triage", analysis or {"error": "event_not_found"}, payload=payload)


def _agent_memo_draft(_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    notes = payload.get("notes", "")
    memo = STORE.memo(
        str(payload.get("company_id") or "").strip() or None,
        str(payload.get("sector") or "").strip() or None,
        str(payload.get("event_id") or "").strip() or None,
        str(notes) if notes is not None else "",
    )
    return _draft_artifact("memo_draft", memo, payload=payload)


AGENT_RESEARCH_HANDLERS = {
    "company_snapshot_refresh": _agent_company_snapshot,
    "sector_scan": _agent_sector_scan,
    "event_triage": _agent_event_triage,
    "memo_draft": _agent_memo_draft,
}
AGENT_ORCHESTRATOR = AgentOrchestrator(
    AGENT_STATE_DIR,
    collector=COLLECTOR,
    research_handlers=AGENT_RESEARCH_HANDLERS,
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the terminal readable while preserving request visibility.
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {self.address_string()} {format % args}")

    def send_json(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_JSON_RESPONSE_BYTES:
            raw = json.dumps({"error": "response_too_large"}, ensure_ascii=False).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise ValueError("invalid_request")
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request_too_large")
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload_must_be_object")
        return payload

    def route(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        return parsed.path.rstrip("/") or "/", parse_qs(parsed.query)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path, query = self.route()
        try:
            if path == "/api/health":
                self.send_json({"ok": True, "service": "tmt-atlas", "mode": "local-first", "time": utc_now()})
            elif path == "/api/config":
                self.send_json({
                    "product": "TMT Atlas",
                    "dataset": STORE.dataset,
                    "sectors": STORE.sectors(),
                    "companies": [{k: c.get(k) for k in ["id", "name", "name_zh", "ticker", "country", "listing", "sector"]} for c in STORE.companies],
                    "prompts": STORE.seed.get("research_prompts", []),
                    "llm": {k: v for k, v in STORE.settings.items() if k != "api_key"},
                })
            elif path == "/api/search":
                self.send_json(STORE.search((query.get("q") or [""])[0]))
            elif path == "/api/companies":
                self.send_json({"companies": [{k: c.get(k) for k in ["id", "name", "name_zh", "ticker", "country", "listing", "sector", "description"]} for c in STORE.companies]})
            elif path.startswith("/api/companies/") and path.endswith("/snapshot"):
                company_id = path.split("/")[3]
                result = STORE.snapshot(company_id)
                self.send_json(result or {"error": "company_not_found"}, 200 if result else 404)
            elif path == "/api/sectors":
                self.send_json({"sectors": STORE.sectors()})
            elif path.startswith("/api/sectors/"):
                sector = unquote(path.split("/", 3)[3])
                result = STORE.sector_scan(sector)
                self.send_json(result)
            elif path == "/api/events":
                events = [STORE.event_view(e) for e in STORE.events]
                self.send_json({"events": sorted(events, key=lambda x: x.get("date", ""), reverse=True)})
            elif path.startswith("/api/events/"):
                event_id = path.split("/")[3]
                result = STORE.event_analysis(event_id)
                self.send_json(result or {"error": "event_not_found"}, 200 if result else 404)
            elif path == "/api/sources":
                self.send_json({"sources": STORE.all_sources})
            elif path == "/api/workspace":
                self.send_json({
                    "sources": WORKSPACE_DB.sources(),
                    "evidence": WORKSPACE_DB.evidence(),
                    "facts": WORKSPACE_DB.facts(),
                    "audit": WORKSPACE_DB.audit(),
                    "stats": WORKSPACE_DB.stats(),
                })
            elif path == "/api/evidence":
                self.send_json({
                    "evidence": WORKSPACE_DB.evidence(
                        (query.get("q") or [""])[0],
                        (query.get("source_id") or [""])[0],
                    )
                })
            elif path == "/api/facts":
                self.send_json({"facts": WORKSPACE_DB.facts((query.get("company_id") or [""])[0])})
            elif path == "/api/audit":
                self.send_json({"audit": WORKSPACE_DB.audit()})
            elif path == "/api/stats":
                self.send_json(WORKSPACE_DB.stats())
            elif path == "/api/verified-facts":
                self.send_json({"facts": load_verified_facts()})
            elif path == "/api/collection/status":
                self.send_json(COLLECTOR.config())
            elif path == "/api/collection/inbox":
                status = (query.get("status") or [None])[0]
                self.send_json({"items": COLLECTOR.inbox(status)})
            elif path == "/api/agent/plans":
                self.send_json({"plans": AGENT_ORCHESTRATOR.plans()})
            elif path.startswith("/api/agent/plans/"):
                parts = path.split("/")
                if len(parts) != 5 or not parts[4]:
                    self.send_json({"error": "not_found"}, 404)
                else:
                    self.send_json({"plan": AGENT_ORCHESTRATOR.get_plan(unquote(parts[4]))})
            elif path == "/api/agent/runs":
                self.send_json({"runs": AGENT_ORCHESTRATOR.runs()})
            elif path.startswith("/api/agent/runs/"):
                parts = path.split("/")
                if len(parts) == 6 and parts[5] == "tasks" and parts[4]:
                    self.send_json({"tasks": AGENT_ORCHESTRATOR.tasks(unquote(parts[4]))})
                elif len(parts) == 5 and parts[4]:
                    self.send_json({"run": AGENT_ORCHESTRATOR.get_run(unquote(parts[4]))})
                else:
                    self.send_json({"error": "not_found"}, 404)
            elif path == "/api/agent/artifacts":
                run_id = (query.get("run_id") or [None])[0]
                self.send_json({"artifacts": AGENT_ORCHESTRATOR.artifacts(run_id)})
            elif path.startswith("/api/agent/artifacts/"):
                artifact_id = path.split("/")[-1]
                self.send_json({"artifact": AGENT_ORCHESTRATOR.get_artifact(unquote(artifact_id))})
            elif path == "/api/agent/review-gates":
                status = (query.get("status") or [None])[0]
                self.send_json({"review_gates": AGENT_ORCHESTRATOR.review_gates(status)})
            elif path == "/api/agent/retrieval-items":
                status = (query.get("status") or [None])[0]
                self.send_json({"retrieval_items": AGENT_ORCHESTRATOR.retrieval_items(status)})
            elif path.startswith("/api/memos/"):
                self.send_json({"error": "memos are generated with POST /api/memos"}, 405)
            else:
                self.serve_static(path)
        except AgentOrchestratorError as exc:
            self.send_json({"error": exc.code}, 400)
        except (ValueError, KeyError):
            self.send_json({"error": "validation_error"}, 400)
        except Exception:
            self.send_json({"error": "server_error"}, 500)

    def do_POST(self) -> None:
        path, _query = self.route()
        try:
            payload = self.read_json()
            if path == "/api/sources":
                item = STORE.add_source(payload)
                self.send_json({"source": item}, 201)
            elif path == "/api/ingest":
                self.handle_ingest(payload)
            elif path == "/api/facts":
                self.send_json({"fact": WORKSPACE_DB.add_fact(payload)}, 201)
            elif path == "/api/facts/review":
                self.send_json({
                    "fact": WORKSPACE_DB.review_fact(
                        payload.get("id"), payload.get("status"), payload.get("reason")
                    )
                })
            elif path == "/api/calculate":
                self.send_json(finance_engine.calculate(payload))
            elif path == "/api/memos":
                result = STORE.memo(payload.get("company_id"), payload.get("sector"), payload.get("event_id"), payload.get("notes", ""))
                self.send_json(result)
            elif path == "/api/settings/llm":
                # Never persist an API key. The runtime key comes from TMT_LLM_API_KEY.
                allowed_keys = {"provider", "base_url", "model", "allow_external_llm", "allow_scheduled_collection"}
                new_settings = {k: payload.get(k) for k in allowed_keys if k in payload}
                new_settings["last_updated"] = utc_now()
                with STORE.lock:
                    STORE.settings.update(new_settings)
                    save_json(SETTINGS_PATH, STORE.settings)
                self.send_json({"llm": {k: v for k, v in STORE.settings.items() if k != "api_key"}, "key_present": bool(os.environ.get("TMT_LLM_API_KEY"))})
            elif path == "/api/llm/enhance":
                self.handle_llm_enhance(payload)
            elif path == "/api/llm/draft":
                self.handle_llm_draft(payload)
            elif path == "/api/collection/config":
                self.send_json(COLLECTOR.configure(payload))
            elif path == "/api/collection/run":
                self.send_json({"items": COLLECTOR.run_once()})
            elif path == "/api/collection/review":
                self.send_json({"item": COLLECTOR.review(payload.get("id"), payload.get("status"))})
            elif path == "/api/collection/plan":
                self.handle_collection_plan(payload)
            elif path == "/api/agent/plans":
                self.send_json({"plan": AGENT_ORCHESTRATOR.create_plan(payload)}, 201)
            elif path.startswith("/api/agent/plans/"):
                parts = path.split("/")
                if len(parts) != 6 or not parts[4] or parts[5] not in {"approve", "pause", "resume", "run"}:
                    self.send_json({"error": "not_found"}, 404)
                    return
                plan_id = unquote(parts[4])
                action = parts[5]
                if action == "approve":
                    self.send_json({"plan": AGENT_ORCHESTRATOR.approve_plan(plan_id)})
                elif action == "pause":
                    self.send_json({"plan": AGENT_ORCHESTRATOR.pause_plan(plan_id)})
                elif action == "resume":
                    self.send_json({"plan": AGENT_ORCHESTRATOR.resume_plan(plan_id)})
                else:
                    self.send_json({
                        "run": AGENT_ORCHESTRATOR.run_plan(
                            plan_id,
                            str(payload.get("trigger", "manual")),
                            scheduled_for=payload.get("scheduled_for"),
                        )
                    })
            elif path.startswith("/api/agent/artifacts/") and path.endswith("/review"):
                parts = path.split("/")
                if len(parts) != 6 or not parts[4]:
                    self.send_json({"error": "not_found"}, 404)
                    return
                self.send_json({"artifact": AGENT_ORCHESTRATOR.review_artifact(
                    unquote(parts[4]), payload.get("decision", payload.get("status")), payload.get("reason")
                )})
            elif path.startswith("/api/agent/review-gates/") and path.endswith("/decision"):
                parts = path.split("/")
                if len(parts) != 6 or not parts[4]:
                    self.send_json({"error": "not_found"}, 404)
                    return
                decision = payload.get("decision", payload.get("status"))
                self.send_json({
                    "review_gate": AGENT_ORCHESTRATOR.decide_gate(
                        unquote(parts[4]), decision, payload.get("reason")
                    )
                })
            elif path.startswith("/api/agent/retrieval-items/") and path.endswith("/review"):
                parts = path.split("/")
                if len(parts) != 6 or not parts[4]:
                    self.send_json({"error": "not_found"}, 404)
                    return
                status = payload.get("status", payload.get("decision"))
                self.send_json({
                    "retrieval_item": AGENT_ORCHESTRATOR.review_retrieval(
                        unquote(parts[4]), status
                    )
                })
            else:
                self.send_json({"error": "not_found"}, 404)
        except json.JSONDecodeError:
            self.send_json({"error": "invalid_json"}, 400)
        except AgentOrchestratorError as exc:
            self.send_json({"error": exc.code}, 400)
        except ValueError:
            self.send_json({"error": "validation_error"}, 400)
        except KeyError:
            self.send_json({"error": "not_found"}, 404)
        except Exception:
            self.send_json({"error": "server_error"}, 500)

    def handle_ingest(self, payload: dict[str, Any]) -> None:
        filename = payload.get("filename")
        content = decode_upload_base64(payload.get("content_base64"))
        result = WORKSPACE_DB.import_bytes(
            filename,
            content,
            ocr=payload.get("ocr", False),
            company_id=payload.get("company_id", ""),
        )
        self.send_json({
            "source": result["source"],
            "evidence": result["evidence"],
            "pending": result["pending_fact_count"],
        }, 201)

    def handle_collection_plan(self, payload: dict[str, Any]) -> None:
        if not STORE.settings.get("allow_scheduled_collection"):
            self.send_json({"error": "collection_disabled"}, 403)
            return
        selected_ids = payload.get("source_ids") or []
        sources = [s for s in STORE.all_sources if s.get("id") in selected_ids]
        plan = {
            "created_at": utc_now(),
            "frequency": payload.get("frequency", "weekly"),
            "lookback_days": int(payload.get("lookback_days", 7)),
            "sources": [{"id": s.get("id"), "title": s.get("title"), "url": s.get("url")} for s in sources],
            "steps": [
                "读取已授权来源的公开元数据/公告入口",
                "按标题、日期和 URL 去重",
                "进入待审核资讯箱，不自动写入确认事实",
                "用户确认后再进行事实抽取和事件分类",
            ],
            "status": "preview_only",
            "note": "当前版本只生成安全的审阅计划，不在没有明确连接器配置时自动抓取全网。",
        }
        self.send_json({"plan": plan})

    def handle_llm_enhance(self, payload: dict[str, Any]) -> None:
        kind = str(payload.get("kind") or "memo_draft")
        context = payload.get("context")
        consent = payload.get("consent") is True
        result = enhance_llm_research(kind, context, STORE.settings, consent=consent)
        self.send_json(result)

    def handle_llm_draft(self, payload: dict[str, Any]) -> None:
        if not STORE.settings.get("allow_external_llm"):
            self.send_json({"error": "llm_disabled"}, 403)
            return
        api_key = os.environ.get("TMT_LLM_API_KEY")
        base_url = (STORE.settings.get("base_url") or os.environ.get("TMT_LLM_BASE_URL") or "").rstrip("/")
        model = STORE.settings.get("model") or os.environ.get("TMT_LLM_MODEL") or ""
        if not api_key or not base_url or not model:
            self.send_json({"error": "llm_not_configured"}, 400)
            return
        evidence = payload.get("evidence", [])
        if not isinstance(evidence, list) or len(evidence) > 12:
            self.send_json({"error": "evidence_scope_invalid"}, 400)
            return
        prompt = str(payload.get("instruction", "请根据证据生成一段研究备忘录草稿，并区分事实、计算和推断。"))
        context = "\n".join(f"[{i+1}] {str(item)[:1500]}" for i, item in enumerate(evidence))
        request_body = json.dumps({
            "model": model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": "你是 TMT 研究助理。只能基于用户选定证据回答；缺少证据时明确说不足以判断；不得把推断写成事实，不提供投资建议。"},
                {"role": "user", "content": prompt + "\n\n选定证据：\n" + context},
            ],
        }).encode("utf-8")
        request = urllib.request.Request(base_url + "/chat/completions", data=request_body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.loads(response.read().decode("utf-8"))
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            self.send_json({"content": content, "model": model, "evidence_count": len(evidence), "generated": True})
        except urllib.error.HTTPError:
            self.send_json({"error": "llm_http_error"}, 502)
        except (urllib.error.URLError, TimeoutError, OSError):
            self.send_json({"error": "llm_network_error"}, 502)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path == "/" else path.lstrip("/")
        candidate = (FRONTEND_DIR / relative).resolve()
        if FRONTEND_DIR.resolve() not in candidate.parents and candidate != FRONTEND_DIR.resolve():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        raw = candidate.read_bytes()
        content_type = "text/html; charset=utf-8"
        if candidate.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif candidate.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif candidate.suffix == ".json":
            content_type = "application/json; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _agent_scheduler_loop(stop_event: threading.Event, interval_seconds: float = 20.0) -> None:
    """Run the persisted agent scheduler only while the server is serving."""
    while not stop_event.wait(interval_seconds):
        try:
            AGENT_ORCHESTRATOR.tick()
        except Exception:
            # Scheduler failures are persisted by the orchestrator where
            # applicable; never terminate the HTTP server or expose details.
            continue


def main() -> None:
    host = os.environ.get("TMT_HOST", "127.0.0.1")
    port = int(os.environ.get("TMT_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    stop_event = threading.Event()
    scheduler_thread = threading.Thread(
        target=_agent_scheduler_loop,
        args=(stop_event, 20.0),
        name="tmt-agent-scheduler",
        daemon=True,
    )
    scheduler_thread.start()
    print(f"TMT Atlas running at http://{host}:{port}")
    print("Local-first mode: no LLM, no external fetch, and no API key required.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping TMT Atlas")
    finally:
        stop_event.set()
        scheduler_thread.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    main()
