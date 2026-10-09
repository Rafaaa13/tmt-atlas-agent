"""Deterministic mapping from curated official facts to Atlas UI view models.

This module is intentionally not a scraper and never invents a value.  It accepts
facts that already carry a primary-source locator, derives only formulas whose
inputs are present and comparable, and represents every other metric explicitly
as missing.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable, Mapping, Optional


RATIO_KEYS = frozenset({"revenue_growth", "gross_margin", "operating_margin", "rd_ratio"})
AMOUNT_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": ("revenue",),
    "operating_income": ("operating_income",),
    "gross_profit": ("gross_profit",),
    "research_and_development": (
        "research_and_development",
        "research_and_development_expense",
        "rd_expense",
        "r_and_d",
    ),
    "operating_cash_flow": ("operating_cash_flow",),
    "capital_expenditures": ("capital_expenditures", "capex"),
    "free_cash_flow": ("free_cash_flow", "fcf"),
}
CANONICAL_ALIASES: dict[str, str] = {
    "capex": "capital_expenditures",
    "fcf": "free_cash_flow",
}
LABELS = {
    "revenue": "总收入",
    "revenue_growth": "收入增速",
    "gross_profit": "毛利",
    "gross_margin": "毛利率",
    "operating_income": "营业利润",
    "operating_margin": "营业利润率",
    "research_and_development": "研发费用",
    "rd_ratio": "研发费用率",
    "operating_cash_flow": "经营现金流",
    "capital_expenditures": "资本开支",
    "capex": "资本开支",
    "free_cash_flow": "自由现金流",
    "fcf": "自由现金流",
    "net_income": "净利润",
    "net_income_attributable": "归属于股东的净利润",
    "net_income_attributable_to_ordinary_shareholders": "归属于普通股股东的净利润",
    "diluted_eps": "摊薄每股收益",
    "diluted_eps_ads": "摊薄每 ADS 收益",
}


def _finite(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _copy_source(fact: Mapping[str, Any]) -> dict[str, Any]:
    source = fact.get("source") if isinstance(fact.get("source"), Mapping) else {}
    result = {
        "url": fact.get("source_url") or source.get("url") or source.get("source_url") or "",
        "title": fact.get("source_title") or source.get("title") or source.get("source_title") or "",
        "published_at": fact.get("published_at") or source.get("published_at"),
        "locator": fact.get("locator") or source.get("locator") or "",
    }
    return result


def _period_label(fact: Mapping[str, Any]) -> Optional[str]:
    explicit = fact.get("period") or fact.get("period_label")
    if explicit:
        return str(explicit)
    end = str(fact.get("period_end") or "")
    if not end:
        return None
    year = end[:4]
    if not year.isdigit():
        return end
    # A March year-end is used by Alibaba in the curated corpus; preserve the
    # fiscal-year convention rather than relabeling it as a calendar year.
    return f"FY{year}"


def _canonical_key(key: Any) -> str:
    raw = str(key or "")
    return CANONICAL_ALIASES.get(raw, raw)


def _dimension_key(fact: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        fact.get("currency"),
        fact.get("period_start"),
        fact.get("period_end"),
        fact.get("period_type"),
        fact.get("accounting_basis"),
        fact.get("scope"),
    )


def _same_dimensions(*facts: Mapping[str, Any]) -> bool:
    if not facts or any(not f for f in facts):
        return False
    dimensions = [_dimension_key(f) for f in facts]
    if any(not item[0] or not item[1] or not item[2] for item in dimensions):
        return False
    return len(set(dimensions)) == 1


def _format_number(value: Any, digits: int = 1) -> str:
    if value is None or not _finite(value):
        return "—"
    return f"{float(value):,.{digits}f}"


def _display_value(value: Any, unit: Any, currency: Any) -> str:
    if value is None:
        return "未披露"
    unit_text = str(unit or "")
    if unit_text == "%":
        return f"{float(value) * 100:.1f}%"
    amount = _format_number(value)
    currency_text = str(currency or "")
    return " ".join(part for part in (amount, unit_text) if part).strip() or amount


def _status_quality(status: str) -> str:
    return {
        "direct": "官方直接披露",
        "derived": "官方事实派生（可审计）",
        "missing": "缺失/未披露",
    }.get(status, "需审核")


class OfficialSnapshotBuilder:
    """Build official-only metric objects from a fact list and seed metadata."""

    def __init__(self, facts: Iterable[Mapping[str, Any]] | None = None) -> None:
        self.facts: list[dict[str, Any]] = [dict(item) for item in (facts or []) if isinstance(item, Mapping)]
        self.by_company: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for fact in self.facts:
            company_id = str(fact.get("company_id") or "")
            if company_id:
                self.by_company[company_id].append(fact)

    def facts_for(self, company_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self.by_company.get(company_id, [])]

    def fact_count(self, company_id: str) -> int:
        return len(self.by_company.get(company_id, []))

    def _find(self, company_id: str, keys: Iterable[str]) -> Optional[dict[str, Any]]:
        wanted = set(keys)
        candidates = [
            fact for fact in self.by_company.get(company_id, [])
            if str(fact.get("metric_key") or "") in wanted and _finite(fact.get("value"))
        ]
        # Stable order is important for deterministic snapshots if a source has
        # more than one disclosure for the same metric.
        candidates.sort(key=lambda fact: (str(fact.get("period_end") or ""), str(fact.get("source_url") or "")), reverse=True)
        return dict(candidates[0]) if candidates else None

    def _base(
        self,
        *,
        key: str,
        label: str,
        value: Any,
        unit: Any,
        currency: Any,
        period: Any,
        status: str,
        source: Optional[Mapping[str, Any]] = None,
        fact: Optional[Mapping[str, Any]] = None,
        formula: Optional[str] = None,
        inputs: Optional[list[dict[str, Any]]] = None,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        source_dict = dict(source or {})
        result: dict[str, Any] = {
            "key": key,
            "label": label,
            "value": value,
            "unit": unit,
            "currency": currency,
            "period": period,
            "period_start": fact.get("period_start") if fact else None,
            "period_end": fact.get("period_end") if fact else None,
            "period_type": fact.get("period_type") if fact else None,
            "accounting_basis": fact.get("accounting_basis") if fact else None,
            "scope": fact.get("scope") if fact else None,
            "status": status,
            "fact_status": fact.get("status") if fact else None,
            "quality": _status_quality(status),
            "display_value": _display_value(value, unit, currency),
            "display_delta": "—",
            "trend": None,
            "source": source_dict,
            "source_locator": source_dict.get("locator") or None,
            "source_title": source_dict.get("title") or "未配置来源",
            "source_url": source_dict.get("url") or "",
            "formula": formula,
            "calculation": {"formula": formula, "inputs": inputs or [], "auditable": bool(formula)} if formula else None,
            "missing_reason": reason,
            "basis": fact.get("accounting_basis") if fact else None,
        }
        if status == "derived":
            result["fact_status"] = "derived_from_official_facts"
        return result

    def _direct(self, key: str, label: str, fact: Mapping[str, Any]) -> dict[str, Any]:
        source = _copy_source(fact)
        unit = fact.get("unit") or ("%" if key in RATIO_KEYS else "")
        currency = fact.get("currency")
        return self._base(
            key=key,
            label=label,
            value=float(fact["value"]),
            unit=unit,
            currency=currency,
            period=_period_label(fact),
            status="direct",
            source=source,
            fact=fact,
        )

    def _missing(self, key: str, label: str, reason: str = "official_fact_unavailable") -> dict[str, Any]:
        return self._base(
            key=key,
            label=label,
            value=None,
            unit="%" if key in RATIO_KEYS else "",
            currency=None,
            period=None,
            status="missing",
            reason=reason,
        )

    def _derived_ratio(
        self,
        *,
        key: str,
        label: str,
        numerator_key: str,
        denominator_key: str,
        numerator: Optional[Mapping[str, Any]],
        denominator: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if not numerator or not denominator:
            return self._missing(key, label, f"requires_{numerator_key}_and_{denominator_key}")
        if not _same_dimensions(numerator, denominator):
            return self._missing(key, label, "inputs_not_comparable")
        denominator_value = float(denominator["value"])
        if denominator_value == 0:
            return self._missing(key, label, "denominator_zero")
        value = float(numerator["value"]) / denominator_value
        source_num = _copy_source(numerator)
        source_den = _copy_source(denominator)
        locator_parts = [part for part in (source_num.get("locator"), source_den.get("locator")) if part]
        source = {
            "url": source_num.get("url") if source_num.get("url") == source_den.get("url") else "",
            "title": source_num.get("title") if source_num.get("title") == source_den.get("title") else "Official facts (two locators)",
            "published_at": source_num.get("published_at") if source_num.get("published_at") == source_den.get("published_at") else None,
            "locator": "；".join(locator_parts),
        }
        inputs = [
            {"key": numerator_key, "value": numerator["value"], "unit": numerator.get("unit"), "currency": numerator.get("currency")},
            {"key": denominator_key, "value": denominator["value"], "unit": denominator.get("unit"), "currency": denominator.get("currency")},
        ]
        return self._base(
            key=key,
            label=label,
            value=value,
            unit="%",
            currency=numerator.get("currency"),
            period=_period_label(numerator),
            status="derived",
            source=source,
            fact=numerator,
            formula=f"{numerator_key} / {denominator_key}",
            inputs=inputs,
        )

    def _growth(self, company_id: str, label: str) -> dict[str, Any]:
        revenue = self._find(company_id, ("revenue",))
        if not revenue or not _finite(revenue.get("prior_value")):
            return self._missing("revenue_growth", label, "requires_revenue_prior_value")
        prior = float(revenue["prior_value"])
        current = float(revenue["value"])
        if prior == 0:
            return self._missing("revenue_growth", label, "prior_value_zero")
        # A prior value is only auditable when it stays in the same reporting
        # currency.  A prior_currency field, when present, is checked; no
        # currency conversion is ever attempted.
        prior_currency = revenue.get("prior_currency") or revenue.get("currency")
        if prior_currency != revenue.get("currency"):
            return self._missing("revenue_growth", label, "prior_currency_mismatch")
        source = _copy_source(revenue)
        value = (current - prior) / prior
        inputs = [
            {"key": "revenue", "value": current, "unit": revenue.get("unit"), "currency": revenue.get("currency")},
            {"key": "revenue_prior", "value": prior, "unit": revenue.get("unit"), "currency": prior_currency},
        ]
        return self._base(
            key="revenue_growth",
            label=label,
            value=value,
            unit="%",
            currency=revenue.get("currency"),
            period=_period_label(revenue),
            status="derived",
            source=source,
            fact=revenue,
            formula="(revenue - revenue_prior) / revenue_prior",
            inputs=inputs,
        )

    def _fcf(self, company_id: str, label: str) -> dict[str, Any]:
        direct = self._find(company_id, AMOUNT_ALIASES["free_cash_flow"])
        if direct:
            return self._direct("fcf", label, direct)
        ocf = self._find(company_id, AMOUNT_ALIASES["operating_cash_flow"])
        capex = self._find(company_id, AMOUNT_ALIASES["capital_expenditures"])
        explicit = bool(
            (ocf and (ocf.get("derived") or ocf.get("derivation_allowed") or ocf.get("allow_derived")))
            or (capex and (capex.get("derived") or capex.get("derivation_allowed") or capex.get("allow_derived")))
        )
        if not (ocf and capex and explicit):
            return self._missing("fcf", label, "requires_direct_free_cash_flow_or_explicit_ocf_capex_derivation")
        if not _same_dimensions(ocf, capex):
            return self._missing("fcf", label, "inputs_not_comparable")
        source_ocf = _copy_source(ocf)
        source_capex = _copy_source(capex)
        source = {
            "url": source_ocf.get("url") if source_ocf.get("url") == source_capex.get("url") else "",
            "title": "Official facts (OCF minus CapEx)",
            "published_at": source_ocf.get("published_at") if source_ocf.get("published_at") == source_capex.get("published_at") else None,
            "locator": "；".join(p for p in (source_ocf.get("locator"), source_capex.get("locator")) if p),
        }
        return self._base(
            key="fcf",
            label=label,
            value=float(ocf["value"]) - float(capex["value"]),
            unit=ocf.get("unit") or capex.get("unit") or "",
            currency=ocf.get("currency"),
            period=_period_label(ocf),
            status="derived",
            source=source,
            fact=ocf,
            formula="operating_cash_flow - capital_expenditures",
            inputs=[
                {"key": "operating_cash_flow", "value": ocf["value"], "unit": ocf.get("unit"), "currency": ocf.get("currency")},
                {"key": "capital_expenditures", "value": capex["value"], "unit": capex.get("unit"), "currency": capex.get("currency")},
            ],
        )

    def metric(self, company_id: str, key: str, label: Optional[str] = None) -> dict[str, Any]:
        requested = str(key)
        display_label = label or LABELS.get(requested) or requested
        canonical = _canonical_key(requested)
        if requested == "revenue_growth":
            return self._growth(company_id, display_label)
        if requested == "operating_margin":
            return self._derived_ratio(
                key=requested,
                label=display_label,
                numerator_key="operating_income",
                denominator_key="revenue",
                numerator=self._find(company_id, AMOUNT_ALIASES["operating_income"]),
                denominator=self._find(company_id, AMOUNT_ALIASES["revenue"]),
            )
        if requested == "gross_margin":
            return self._derived_ratio(
                key=requested,
                label=display_label,
                numerator_key="gross_profit",
                denominator_key="revenue",
                numerator=self._find(company_id, AMOUNT_ALIASES["gross_profit"]),
                denominator=self._find(company_id, AMOUNT_ALIASES["revenue"]),
            )
        if requested == "rd_ratio":
            return self._derived_ratio(
                key=requested,
                label=display_label,
                numerator_key="research_and_development",
                denominator_key="revenue",
                numerator=self._find(company_id, AMOUNT_ALIASES["research_and_development"]),
                denominator=self._find(company_id, AMOUNT_ALIASES["revenue"]),
            )
        if requested in {"fcf", "free_cash_flow"}:
            metric = self._fcf(company_id, display_label)
            metric["key"] = requested
            return metric

        candidates = AMOUNT_ALIASES.get(canonical, (canonical,))
        fact = self._find(company_id, candidates)
        if fact:
            return self._direct(requested, display_label, fact)
        return self._missing(requested, display_label)

    def metric_keys(self, company: Mapping[str, Any]) -> list[tuple[str, str]]:
        company_id = str(company.get("id") or "")
        seen: set[str] = set()
        result: list[tuple[str, str]] = []
        for item in company.get("metrics", []) if isinstance(company.get("metrics"), list) else []:
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("key") or "")
            if key and key not in seen:
                result.append((key, str(item.get("label") or LABELS.get(key) or key)))
                seen.add(key)
        # FCF is a first-class UI metric when the official corpus has it, even
        # if the legacy seed did not include the field.
        if self._find(company_id, AMOUNT_ALIASES["free_cash_flow"]) and "fcf" not in seen:
            result.append(("fcf", "自由现金流"))
            seen.add("fcf")
        known_raw = [
            "revenue", "operating_income", "gross_profit", "research_and_development",
            "operating_cash_flow", "capital_expenditures", "net_income", "net_income_attributable",
            "net_income_attributable_to_ordinary_shareholders", "diluted_eps", "diluted_eps_ads",
        ]
        for raw_key in known_raw:
            fact = self._find(company_id, AMOUNT_ALIASES.get(raw_key, (raw_key,)))
            if fact and raw_key not in seen:
                result.append((raw_key, LABELS.get(raw_key, str(fact.get("label") or raw_key))))
                seen.add(raw_key)
        return result

    def metrics_for(self, company: Mapping[str, Any]) -> list[dict[str, Any]]:
        company_id = str(company.get("id") or "")
        return [self.metric(company_id, key, label) for key, label in self.metric_keys(company)]

    def snapshot(self, company: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "metrics": self.metrics_for(company),
            "official_fact_count": self.fact_count(str(company.get("id") or "")),
            "official_facts": self.facts_for(str(company.get("id") or "")),
        }


__all__ = ["OfficialSnapshotBuilder"]
