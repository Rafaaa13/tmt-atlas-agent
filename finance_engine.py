"""Deterministic, auditable financial calculations for TMT Atlas.

This module deliberately does not call an LLM, fetch data, or make market
predictions.  Inputs are validated before calculation and every public result
contains the formula, normalized inputs, warnings, and a status.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, localcontext
import math
from typing import Any, Iterable


_DECIMAL_ZERO = Decimal("0")
_DECIMAL_ONE = Decimal("1")


@dataclass
class CalculationResult:
    operation: str
    value: Any | None
    unit: str | None
    formula: str
    inputs: dict[str, Any]
    warnings: list[str]
    status: str = "ok"
    scenarios: list[dict[str, Any]] | None = None
    sensitivity: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        # Keep the original compact response for non-DCF operations while
        # allowing DCF to expose optional scenario/sensitivity outputs.
        if self.scenarios is None:
            result.pop("scenarios", None)
        if self.sensitivity is None:
            result.pop("sensitivity", None)
        return result


def _number(value: Any, name: str) -> Decimal:
    """Parse a finite numeric input, explicitly rejecting bool/NaN/Infinity."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{name} 不能为空")
    if isinstance(value, bool):
        raise ValueError(f"{name} 不能是布尔值")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, (int, float, str)):
        try:
            # str(float) avoids importing the binary floating-point artifact
            # into Decimal arithmetic while retaining the public API's support
            # for ordinary Python numbers and numeric strings.
            number = Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{name} 必须是数字") from exc
    else:
        raise ValueError(f"{name} 必须是数字")
    if not number.is_finite():
        raise ValueError(f"{name} 必须是有限数字，不能是 NaN 或 Inf")
    return number


def _output_number(value: Decimal, name: str = "计算结果") -> float:
    """Convert a Decimal result to a JSON-friendly finite float."""
    if not value.is_finite():
        raise ValueError(f"{name} 非有限，无法返回结果")
    output = float(value)
    if not math.isfinite(output):
        raise ValueError(f"{name} 超出可返回的有限范围")
    return output


def _input_number(value: Decimal) -> float:
    return _output_number(value, "输入")


def _blocked(
    operation: str,
    formula: str,
    inputs: dict[str, Any],
    warning: str,
    *,
    unit: str | None = None,
    warnings: list[str] | None = None,
    scenarios: list[dict[str, Any]] | None = None,
    sensitivity: dict[str, Any] | None = None,
) -> CalculationResult:
    all_warnings = list(warnings or [])
    if warning:
        all_warnings.append(warning)
    return CalculationResult(
        operation,
        None,
        unit,
        formula,
        inputs,
        all_warnings,
        "blocked",
        scenarios,
        sensitivity,
    )


def growth(current: Any, previous: Any, *, label: str = "增长率") -> CalculationResult:
    """Calculate conventional YoY: current / previous - 1.

    A negative or zero prior period is not a conventional growth-rate base.  In
    particular, this never replaces ``previous`` with ``abs(previous)``.
    """
    current_n = _number(current, "current")
    previous_n = _number(previous, "previous")
    inputs = {
        "current": _input_number(current_n),
        "previous": _input_number(previous_n),
    }
    formula = "current / previous - 1 (previous > 0)"
    if previous_n == _DECIMAL_ZERO:
        inputs["absolute_change"] = _input_number(current_n - previous_n)
        return _blocked(
            "growth",
            formula,
            inputs,
            "上期数值为 0，不能计算常规同比；请报告绝对变化 current - previous。",
            unit="%",
        )
    if previous_n < _DECIMAL_ZERO:
        inputs["absolute_change"] = _input_number(current_n - previous_n)
        return _blocked(
            "growth",
            formula,
            inputs,
            "上期数值为负，不能计算常规同比；请报告绝对变化 current - previous，不能用 abs(previous) 作为分母。",
            unit="%",
        )
    with localcontext() as context:
        context.prec = 40
        value = current_n / previous_n - _DECIMAL_ONE
    return CalculationResult("growth", _output_number(value), "%", formula, inputs, [])


def margin(
    numerator: Any,
    denominator: Any,
    *,
    numerator_label: str = "利润",
    denominator_label: str = "收入",
) -> CalculationResult:
    numerator_n = _number(numerator, "numerator")
    denominator_n = _number(denominator, "denominator")
    inputs = {
        "numerator": _input_number(numerator_n),
        "denominator": _input_number(denominator_n),
    }
    formula = f"{numerator_label} / {denominator_label} (denominator > 0)"
    if denominator_n <= _DECIMAL_ZERO:
        return _blocked(
            "margin",
            formula,
            inputs,
            "利润率分母必须大于 0；0 或负数收入不能按通常利润率解释。",
            unit="%",
        )
    with localcontext() as context:
        context.prec = 40
        value = numerator_n / denominator_n
    return CalculationResult("margin", _output_number(value), "%", formula, inputs, [])


def cagr(start: Any, end: Any, periods: Any) -> CalculationResult:
    start_n = _number(start, "start")
    end_n = _number(end, "end")
    periods_n = _number(periods, "periods")
    inputs = {
        "start": _input_number(start_n),
        "end": _input_number(end_n),
        "periods": _input_number(periods_n),
    }
    formula = "(end / start) ** (1 / periods) - 1"
    if start_n <= _DECIMAL_ZERO:
        return _blocked(
            "cagr",
            formula,
            inputs,
            "CAGR 要求起点 start 大于 0。",
            unit="%",
        )
    if end_n < _DECIMAL_ZERO:
        return _blocked(
            "cagr",
            formula,
            inputs,
            "CAGR 不接受负的终点 end；请改用绝对变化或分阶段分析。",
            unit="%",
        )
    if periods_n <= _DECIMAL_ZERO:
        return _blocked(
            "cagr",
            formula,
            inputs,
            "期间数 periods 必须大于 0。",
            unit="%",
        )
    if periods_n > Decimal("100"):
        return _blocked(
            "cagr",
            formula,
            inputs,
            "期间数 periods 不能超过 100。",
            unit="%",
        )
    with localcontext() as context:
        context.prec = 40
        # Decimal supports non-integer exponents and keeps the calculation out
        # of binary floating point until the response is serialized.
        value = (end_n / start_n) ** (_DECIMAL_ONE / periods_n) - _DECIMAL_ONE
    return CalculationResult("cagr", _output_number(value), "%", formula, inputs, [])


_UNIT_FACTORS: dict[str, dict[str, Decimal]] = {
    "USD": {
        "USD": Decimal("1"),
        "USD thousand": Decimal("1000"),
        "USD million": Decimal("1000000"),
        "USD billion": Decimal("1000000000"),
        "USD k": Decimal("1000"),
        "USD m": Decimal("1000000"),
        "USD bn": Decimal("1000000000"),
    },
    "CNY": {
        "CNY": Decimal("1"),
        "CNY thousand": Decimal("1000"),
        "CNY million": Decimal("1000000"),
        "CNY billion": Decimal("1000000000"),
        "CNY k": Decimal("1000"),
        "CNY m": Decimal("1000000"),
        "CNY bn": Decimal("1000000000"),
    },
}


def convert_unit(value: Any, from_unit: str, to_unit: str) -> CalculationResult:
    value_n = _number(value, "value")
    from_unit = str(from_unit).strip()
    to_unit = str(to_unit).strip()
    from_family = next((family for family, units in _UNIT_FACTORS.items() if from_unit in units), None)
    to_family = next((family for family, units in _UNIT_FACTORS.items() if to_unit in units), None)
    formula = f"value × factor({from_unit}) / factor({to_unit})"
    inputs = {
        "value": _input_number(value_n),
        "from_unit": from_unit,
        "to_unit": to_unit,
    }
    if not from_family or not to_family:
        return _blocked(
            "unit_conversion",
            formula,
            inputs,
            "只允许在受支持的同币种单位族内换算。",
            unit=to_unit or None,
        )
    if from_family != to_family:
        return _blocked(
            "unit_conversion",
            formula,
            inputs,
            "币种不同，不能只靠单位换算；需要明确汇率来源和日期。",
            unit=to_unit,
        )
    with localcontext() as context:
        context.prec = 40
        converted = value_n * _UNIT_FACTORS[from_family][from_unit] / _UNIT_FACTORS[to_family][to_unit]
    return CalculationResult("unit_conversion", _output_number(converted), to_unit, formula, inputs, [])


_REQUIRED_COMPARABILITY_DIMENSIONS = (
    "metric_key",
    "unit",
    "currency",
    "period_type",
    "period_start",
    "period_end",
    "accounting_basis",
    "scope",
)


def _missing_dimension(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip().lower()
    return text in {"", "none", "null"}


def comparability(
    facts: Iterable[dict[str, Any]] | None,
    *,
    allow_currency_independent: bool = False,
) -> dict[str, Any]:
    """Check whether facts are comparable without silently filling gaps.

    Currency independence is opt-in only.  It is useful for dimensionless
    ratios, but it never makes missing currency metadata acceptable and never
    waives unit, period, scope, or accounting-basis checks.
    """
    if not isinstance(allow_currency_independent, bool):
        raise ValueError("allow_currency_independent 必须是布尔值")
    facts_list = list(facts or [])
    if len(facts_list) < 2:
        return {
            "comparable": False,
            "reasons": ["至少需要两个事实才能判断可比性。"],
            "checked": [],
        }
    if any(not isinstance(fact, dict) for fact in facts_list):
        return {
            "comparable": False,
            "reasons": ["每个事实必须是对象，不能用非结构化值判断可比性。"],
            "checked": [],
        }

    reasons: list[str] = []
    checked: list[str] = []
    for dimension in _REQUIRED_COMPARABILITY_DIMENSIONS:
        missing_indexes = [index for index, fact in enumerate(facts_list) if _missing_dimension(fact.get(dimension))]
        if missing_indexes:
            reasons.append(
                f"缺少必需维度 {dimension}（事实 {', '.join(str(i + 1) for i in missing_indexes)} 缺失；"
                "即使所有事实都为空也不能视为可比）。"
            )
            continue
        values = {str(fact.get(dimension)).strip() for fact in facts_list}
        if dimension == "currency" and allow_currency_independent:
            checked.append(dimension)
            continue
        if len(values) > 1:
            reasons.append(f"{dimension} 不一致：{', '.join(sorted(values))}")
        else:
            checked.append(dimension)
    return {"comparable": not reasons, "reasons": reasons, "checked": checked}


def valuation_multiple(
    numerator: Any,
    denominator: Any,
    *,
    numerator_label: str = "市值",
    denominator_label: str = "收入",
) -> CalculationResult:
    numerator_n = _number(numerator, "numerator")
    denominator_n = _number(denominator, "denominator")
    inputs = {
        "numerator": _input_number(numerator_n),
        "denominator": _input_number(denominator_n),
        "numerator_label": numerator_label,
        "denominator_label": denominator_label,
    }
    formula = f"{numerator_label} / {denominator_label} (denominator > 0)"
    if denominator_n <= _DECIMAL_ZERO:
        return _blocked(
            "valuation_multiple",
            formula,
            inputs,
            "估值倍数的分母必须大于 0；亏损或负值分母应换用其他分析框架。",
            unit="x",
        )
    with localcontext() as context:
        context.prec = 40
        value = numerator_n / denominator_n
    return CalculationResult(
        "valuation_multiple",
        _output_number(value),
        "x",
        formula,
        inputs,
        ["估值倍数是机械计算，不含股价时点、净债务、稀释股本等完整估值假设；不构成投资建议。"],
    )


def ev_revenue(
    enterprise_value_input: Any,
    revenue: Any,
    *,
    unit: str | None = None,
) -> CalculationResult:
    result = valuation_multiple(
        enterprise_value_input,
        revenue,
        numerator_label="企业价值（EV）",
        denominator_label="收入（Revenue）",
    )
    result.operation = "ev_revenue"
    result.unit = "x"
    result.inputs["unit"] = unit
    result.warnings.insert(0, "EV 与收入被假设为同一币种、单位和可比期间；请核对输入。")
    return result


def pe(
    market_cap: Any,
    net_income: Any,
    *,
    unit: str | None = None,
) -> CalculationResult:
    result = valuation_multiple(
        market_cap,
        net_income,
        numerator_label="市值（Market Cap）",
        denominator_label="净利润（Net Income）",
    )
    result.operation = "pe"
    result.inputs["unit"] = unit
    result.warnings.insert(0, "P/E 被假设使用同一币种、单位和可比期间；亏损公司不适用。")
    return result


def _nonnegative_component(value: Any, name: str) -> Decimal:
    number = _number(value, name)
    if number < _DECIMAL_ZERO:
        raise ValueError(f"{name} 不能为负数")
    return number


def enterprise_value(
    market_cap: Any,
    debt: Any = 0,
    preferred_equity: Any = 0,
    minority_interest: Any = 0,
    cash: Any = 0,
    *,
    unit: str | None = None,
) -> CalculationResult:
    market_cap_n = _nonnegative_component(market_cap, "market_cap")
    debt_n = _nonnegative_component(debt, "debt")
    preferred_n = _nonnegative_component(preferred_equity, "preferred_equity")
    minority_n = _nonnegative_component(minority_interest, "minority_interest")
    cash_n = _nonnegative_component(cash, "cash")
    with localcontext() as context:
        context.prec = 40
        value = market_cap_n + debt_n + preferred_n + minority_n - cash_n
    inputs = {
        "market_cap": _input_number(market_cap_n),
        "debt": _input_number(debt_n),
        "preferred_equity": _input_number(preferred_n),
        "minority_interest": _input_number(minority_n),
        "cash": _input_number(cash_n),
        "unit": unit,
    }
    return CalculationResult(
        "enterprise_value",
        _output_number(value),
        unit or "input units",
        "market_cap + debt + preferred_equity + minority_interest - cash",
        inputs,
        [
            "所有输入被假设为同一币种和单位；请在使用前核对，尤其是现金、债务、优先股和少数股东权益。",
            "企业价值是机械计算，不构成投资建议，也不包含市场预测。",
        ],
    )


def fcf(cfo: Any, capex: Any, *, unit: str | None = None) -> CalculationResult:
    cfo_n = _number(cfo, "cfo")
    capex_n = _number(capex, "capex")
    inputs = {"cfo": _input_number(cfo_n), "capex": _input_number(capex_n), "unit": unit}
    formula = "CFO - CapEx (CapEx entered as a positive cash outflow)"
    if capex_n < _DECIMAL_ZERO:
        return _blocked(
            "fcf",
            formula,
            inputs,
            "capex 必须按正的现金流出输入；不要用负号后再隐式取绝对值。",
            unit=unit,
        )
    value = cfo_n - capex_n
    return CalculationResult(
        "fcf",
        _output_number(value),
        unit,
        formula,
        inputs,
        ["自由现金流采用 CFO - 正值 CapEx 约定；这是机械计算，不构成投资建议。"],
    )


def _parse_growth_rates(forecast_growth: Any, years: int, name: str = "forecast_growth") -> list[Decimal]:
    if isinstance(forecast_growth, (str, bytes)):
        raise ValueError(f"{name} 必须是长度等于 years 的增长率列表")
    try:
        rates_raw = list(forecast_growth)
    except TypeError as exc:
        raise ValueError(f"{name} 必须是长度等于 years 的增长率列表") from exc
    if len(rates_raw) != years:
        raise ValueError(f"{name} 必须明确提供 years={years} 个增长率")
    return [_number(rate, f"{name}[{index}]") for index, rate in enumerate(rates_raw)]


def _dcf_core(
    base_fcf: Decimal,
    growth_rates: list[Decimal],
    wacc: Decimal,
    terminal_growth: Decimal,
    years: int,
    net_debt: Decimal,
    shares: Decimal,
) -> dict[str, Decimal]:
    if wacc <= terminal_growth:
        raise ValueError("WACC 必须大于 terminal_growth，终值分母不能为 0 或负数")
    if _DECIMAL_ONE + wacc <= _DECIMAL_ZERO:
        raise ValueError("WACC 必须大于 -100%，以保证折现因子为正")
    if shares <= _DECIMAL_ZERO:
        raise ValueError("shares 必须大于 0")
    with localcontext() as context:
        context.prec = 50
        forecast_fcfs: list[Decimal] = []
        current_fcf = base_fcf
        pv_forecast = _DECIMAL_ZERO
        discount_base = _DECIMAL_ONE + wacc
        for year, growth_rate in enumerate(growth_rates, start=1):
            current_fcf *= _DECIMAL_ONE + growth_rate
            forecast_fcfs.append(current_fcf)
            pv_forecast += current_fcf / (discount_base ** year)
        terminal_fcf = current_fcf * (_DECIMAL_ONE + terminal_growth)
        terminal_value = terminal_fcf / (wacc - terminal_growth)
        pv_terminal = terminal_value / (discount_base ** years)
        enterprise_value_n = pv_forecast + pv_terminal
        equity_value = enterprise_value_n - net_debt
        value_per_share = equity_value / shares
        terminal_contribution = (
            pv_terminal / enterprise_value_n if enterprise_value_n != _DECIMAL_ZERO else _DECIMAL_ZERO
        )
    return {
        "pv_forecast": pv_forecast,
        "terminal_fcf": terminal_fcf,
        "terminal_value": terminal_value,
        "pv_terminal": pv_terminal,
        "enterprise_value": enterprise_value_n,
        "equity_value": equity_value,
        "value_per_share": value_per_share,
        "terminal_contribution": terminal_contribution,
        "forecast_fcf_last": current_fcf,
        "forecast_fcf_sum": sum(forecast_fcfs, _DECIMAL_ZERO),
    }


def _core_inputs(core: dict[str, Decimal]) -> dict[str, float]:
    return {key: _output_number(value, key) for key, value in core.items()}


def _sensitivity_values(spec: Any, name: str) -> list[Decimal]:
    if isinstance(spec, bool):
        raise ValueError(f"{name} 范围必须是数值列表或 min/max/step 对象")
    if isinstance(spec, dict):
        if "values" in spec:
            spec = spec["values"]
        elif "min" in spec and "max" in spec:
            minimum = _number(spec["min"], f"{name}.min")
            maximum = _number(spec["max"], f"{name}.max")
            if maximum < minimum:
                raise ValueError(f"{name}.max 不能小于 min")
            if minimum == maximum:
                return [minimum]
            step = _number(spec.get("step", (maximum - minimum) / Decimal("2")), f"{name}.step")
            if step <= _DECIMAL_ZERO:
                raise ValueError(f"{name}.step 必须大于 0")
            values: list[Decimal] = []
            current = minimum
            while current <= maximum:
                values.append(current)
                current += step
                if len(values) > 101:
                    raise ValueError(f"{name} 范围点数不能超过 101")
            if values[-1] != maximum:
                values.append(maximum)
            return values
        else:
            raise ValueError(f"{name} 必须包含 values 或 min/max")
    if isinstance(spec, (str, bytes)):
        raise ValueError(f"{name} 必须是数值列表")
    try:
        raw_values = list(spec)
    except TypeError:
        raw_values = [spec]
    if not raw_values:
        raise ValueError(f"{name} 不能为空")
    return [_number(value, f"{name}[{index}]") for index, value in enumerate(raw_values)]


def _make_sensitivity(
    sensitivity_spec: Any,
    *,
    base_wacc: Decimal,
    base_terminal_growth: Decimal,
    base_fcf: Decimal,
    growth_rates: list[Decimal],
    years: int,
    net_debt: Decimal,
    shares: Decimal,
) -> tuple[dict[str, Any] | None, list[str]]:
    if sensitivity_spec is None or sensitivity_spec is False:
        return None, []
    if sensitivity_spec is True:
        sensitivity_spec = {}
    if not isinstance(sensitivity_spec, dict):
        raise ValueError("sensitivity 必须是对象")
    wacc_spec = sensitivity_spec.get("wacc", sensitivity_spec.get("wacc_range"))
    growth_spec = sensitivity_spec.get(
        "terminal_growth",
        sensitivity_spec.get("g", sensitivity_spec.get("terminal_growth_range", sensitivity_spec.get("g_range"))),
    )
    if wacc_spec is None:
        wacc_spec = {"min": base_wacc - Decimal("0.02"), "max": base_wacc + Decimal("0.02"), "step": Decimal("0.02")}
    if growth_spec is None:
        growth_spec = {
            "min": base_terminal_growth - Decimal("0.01"),
            "max": base_terminal_growth + Decimal("0.01"),
            "step": Decimal("0.01"),
        }
    wacc_values = _sensitivity_values(wacc_spec, "sensitivity.wacc")
    growth_values = _sensitivity_values(growth_spec, "sensitivity.terminal_growth")
    matrix: list[list[float | None]] = []
    warnings: list[str] = []
    invalid_pair = False
    for candidate_wacc in wacc_values:
        row: list[float | None] = []
        for candidate_growth in growth_values:
            if candidate_wacc <= candidate_growth:
                row.append(None)
                invalid_pair = True
                continue
            try:
                result = _dcf_core(
                    base_fcf,
                    growth_rates,
                    candidate_wacc,
                    candidate_growth,
                    years,
                    net_debt,
                    shares,
                )
                row.append(_output_number(result["value_per_share"], "sensitivity value"))
            except ValueError:
                row.append(None)
                invalid_pair = True
        matrix.append(row)
    if invalid_pair:
        warnings.append("敏感性矩阵中 WACC <= terminal_growth 的组合已标记为 null。")
    output = {
        "wacc": [_output_number(value, "sensitivity.wacc") for value in wacc_values],
        "terminal_growth": [_output_number(value, "sensitivity.terminal_growth") for value in growth_values],
        "values": matrix,
        "unit": "currency/share",
        "formula": "对每个 WACC / terminal_growth 组合重新折现并计算每股价值",
    }
    return output, warnings


def _make_scenarios(
    scenarios_input: Any,
    *,
    base_fcf: Decimal,
    base_growth_rates: list[Decimal],
    base_wacc: Decimal,
    base_terminal_growth: Decimal,
    years: int,
    net_debt: Decimal,
    shares: Decimal,
) -> list[dict[str, Any]] | None:
    if scenarios_input is None:
        return None
    if isinstance(scenarios_input, (str, bytes)):
        raise ValueError("scenarios 必须是对象列表")
    try:
        scenario_items = list(scenarios_input)
    except TypeError as exc:
        raise ValueError("scenarios 必须是对象列表") from exc
    outputs: list[dict[str, Any]] = []
    for index, scenario in enumerate(scenario_items):
        if not isinstance(scenario, dict):
            outputs.append({"name": str(index + 1), "status": "blocked", "value": None, "warnings": ["情景必须是对象。"]})
            continue
        name = str(scenario.get("name", f"scenario_{index + 1}"))
        try:
            scenario_rates = _parse_growth_rates(
                scenario.get("forecast_growth", scenario.get("growth_rates", base_growth_rates)),
                years,
                f"scenarios[{index}].forecast_growth",
            )
            scenario_wacc = _number(scenario.get("wacc", base_wacc), f"scenarios[{index}].wacc")
            scenario_growth = _number(
                scenario.get("terminal_growth", scenario.get("g", base_terminal_growth)),
                f"scenarios[{index}].terminal_growth",
            )
            core = _dcf_core(
                base_fcf,
                scenario_rates,
                scenario_wacc,
                scenario_growth,
                years,
                net_debt,
                shares,
            )
            output = {
                "name": name,
                "status": "ok",
                "value": _output_number(core["value_per_share"], f"scenarios[{index}] value"),
                "unit": "currency/share",
                "wacc": _output_number(scenario_wacc),
                "terminal_growth": _output_number(scenario_growth),
                "enterprise_value": _output_number(core["enterprise_value"]),
                "equity_value": _output_number(core["equity_value"]),
                "terminal_contribution": _output_number(core["terminal_contribution"]),
                "warnings": [],
            }
            if core["terminal_contribution"] >= Decimal("0.5"):
                output["warnings"].append("终值现值占企业价值较高，结果对终值假设敏感。")
            outputs.append(output)
        except (ValueError, ArithmeticError) as exc:
            outputs.append({"name": name, "status": "blocked", "value": None, "warnings": [str(exc)]})
    return outputs


def dcf(
    base_fcf: Any,
    forecast_growth: Any,
    wacc: Any,
    terminal_growth: Any,
    years: Any,
    net_debt: Any,
    shares: Any,
    *,
    unit: str | None = None,
    scenarios: Any = None,
    sensitivity: Any = None,
) -> CalculationResult:
    """Simple explicit-growth DCF returning implied value per share.

    ``forecast_growth`` must contain one user-supplied growth rate for every
    forecast year.  No growth rate, market forecast, or recommendation is
    inferred by this function.
    """
    base_fcf_n = _number(base_fcf, "base_fcf")
    wacc_n = _number(wacc, "wacc")
    terminal_growth_n = _number(terminal_growth, "terminal_growth")
    years_n = _number(years, "years")
    net_debt_n = _number(net_debt, "net_debt")
    shares_n = _number(shares, "shares")
    formula = (
        "FCF_t = base_fcf × Π(1 + forecast_growth_i); "
        "PV = Σ FCF_t/(1+WACC)^t + [FCF_years×(1+g)/(WACC-g)]/(1+WACC)^years; "
        "value_per_share = (PV - net_debt) / shares"
    )
    basic_inputs: dict[str, Any] = {
        "base_fcf": _input_number(base_fcf_n),
        "wacc": _input_number(wacc_n),
        "terminal_growth": _input_number(terminal_growth_n),
        "years": _input_number(years_n),
        "net_debt": _input_number(net_debt_n),
        "shares": _input_number(shares_n),
        "unit": unit,
    }
    output_unit = f"{unit}/share" if unit else "currency/share"
    if base_fcf_n <= _DECIMAL_ZERO:
        return _blocked("dcf", formula, basic_inputs, "base_fcf 必须大于 0。", unit=output_unit)
    if shares_n <= _DECIMAL_ZERO:
        return _blocked("dcf", formula, basic_inputs, "shares 必须大于 0。", unit=output_unit)
    if years_n != years_n.to_integral_value() or not (Decimal("1") <= years_n <= Decimal("10")):
        return _blocked("dcf", formula, basic_inputs, "years 必须是 1 至 10 的整数。", unit=output_unit)
    years_i = int(years_n)
    try:
        growth_rates = _parse_growth_rates(forecast_growth, years_i)
    except ValueError as exc:
        return _blocked("dcf", formula, basic_inputs, str(exc), unit=output_unit)
    basic_inputs["forecast_growth"] = [_input_number(rate) for rate in growth_rates]
    if wacc_n <= terminal_growth_n:
        return _blocked("dcf", formula, basic_inputs, "WACC 必须大于 terminal_growth。", unit=output_unit)
    if _DECIMAL_ONE + wacc_n <= _DECIMAL_ZERO:
        return _blocked("dcf", formula, basic_inputs, "WACC 必须大于 -100%，以保证折现因子为正。", unit=output_unit)

    try:
        core = _dcf_core(
            base_fcf_n,
            growth_rates,
            wacc_n,
            terminal_growth_n,
            years_i,
            net_debt_n,
            shares_n,
        )
    except (ValueError, ArithmeticError) as exc:
        return _blocked("dcf", formula, basic_inputs, str(exc), unit=output_unit)

    basic_inputs.update(_core_inputs(core))
    warnings = [
        "DCF 仅按用户提供的现金流、增长率和折现率机械计算，不构成投资建议，也不包含市场预测。",
        "base_fcf、net_debt 和 shares 被假设为与 unit 一致；请核对币种、单位和口径。",
    ]
    if core["enterprise_value"] <= _DECIMAL_ZERO:
        warnings.append("折现后企业价值非正；请检查现金流和估值假设。")
    if core["terminal_contribution"] >= Decimal("0.5"):
        warnings.append(
            f"终值现值占企业价值 {_output_number(core['terminal_contribution'] * Decimal('100')):.1f}%；"
            "结果对 WACC 与 terminal_growth 敏感。"
        )
    elif core["enterprise_value"] == _DECIMAL_ZERO:
        warnings.append("企业价值为 0，无法形成有意义的终值贡献比例。")

    try:
        sensitivity_output, sensitivity_warnings = _make_sensitivity(
            sensitivity,
            base_wacc=wacc_n,
            base_terminal_growth=terminal_growth_n,
            base_fcf=base_fcf_n,
            growth_rates=growth_rates,
            years=years_i,
            net_debt=net_debt_n,
            shares=shares_n,
        )
    except (ValueError, ArithmeticError) as exc:
        return _blocked(
            "dcf",
            formula,
            basic_inputs,
            f"敏感性设置无效：{exc}",
            unit=output_unit,
        )
    warnings.extend(sensitivity_warnings)
    scenario_output = _make_scenarios(
        scenarios,
        base_fcf=base_fcf_n,
        base_growth_rates=growth_rates,
        base_wacc=wacc_n,
        base_terminal_growth=terminal_growth_n,
        years=years_i,
        net_debt=net_debt_n,
        shares=shares_n,
    )
    if scenario_output is not None:
        warnings.append("情景结果仅反映用户提供的假设，不是市场预测。")
    return CalculationResult(
        "dcf",
        _output_number(core["value_per_share"]),
        output_unit,
        formula,
        basic_inputs,
        warnings,
        "ok",
        scenario_output,
        sensitivity_output,
    )


def calculate(payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch the stable calculation API and return a structured dictionary."""
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象")
    operation = str(payload.get("operation", "")).strip().lower()
    if operation == "growth":
        return growth(payload.get("current"), payload.get("previous"), label=str(payload.get("label", "增长率"))).to_dict()
    if operation == "margin":
        return margin(
            payload.get("numerator"),
            payload.get("denominator"),
            numerator_label=str(payload.get("numerator_label", "利润")),
            denominator_label=str(payload.get("denominator_label", "收入")),
        ).to_dict()
    if operation == "cagr":
        return cagr(payload.get("start"), payload.get("end"), payload.get("periods")).to_dict()
    if operation == "unit_conversion":
        return convert_unit(payload.get("value"), str(payload.get("from_unit", "")), str(payload.get("to_unit", ""))).to_dict()
    if operation == "valuation_multiple":
        multiple_type = str(payload.get("multiple_type", payload.get("type", ""))).strip().lower()
        if multiple_type in {"ev_revenue", "ev/revenue", "ev-revenue"}:
            return ev_revenue(
                payload.get("enterprise_value", payload.get("numerator")),
                payload.get("revenue", payload.get("denominator")),
                unit=payload.get("unit"),
            ).to_dict()
        if multiple_type in {"pe", "p/e"}:
            return pe(
                payload.get("market_cap", payload.get("numerator")),
                payload.get("net_income", payload.get("denominator")),
                unit=payload.get("unit"),
            ).to_dict()
        return valuation_multiple(
            payload.get("numerator"),
            payload.get("denominator"),
            numerator_label=str(payload.get("numerator_label", "市值")),
            denominator_label=str(payload.get("denominator_label", "收入")),
        ).to_dict()
    if operation in {"ev_revenue", "ev/revenue", "ev-revenue"}:
        return ev_revenue(
            payload.get("enterprise_value", payload.get("numerator")),
            payload.get("revenue", payload.get("denominator")),
            unit=payload.get("unit"),
        ).to_dict()
    if operation in {"pe", "p/e"}:
        return pe(
            payload.get("market_cap", payload.get("numerator")),
            payload.get("net_income", payload.get("denominator")),
            unit=payload.get("unit"),
        ).to_dict()
    if operation == "enterprise_value":
        return enterprise_value(
            payload.get("market_cap"),
            payload.get("debt", payload.get("total_debt", 0)),
            payload.get("preferred_equity", payload.get("preferred", 0)),
            payload.get("minority_interest", payload.get("minority", 0)),
            payload.get("cash", payload.get("cash_and_equivalents", 0)),
            unit=payload.get("unit"),
        ).to_dict()
    if operation == "fcf":
        return fcf(payload.get("cfo"), payload.get("capex"), unit=payload.get("unit")).to_dict()
    if operation == "dcf":
        return dcf(
            payload.get("base_fcf"),
            payload.get("forecast_growth", payload.get("growth_rates")),
            payload.get("wacc"),
            payload.get("terminal_growth", payload.get("g")),
            payload.get("years"),
            payload.get("net_debt"),
            payload.get("shares"),
            unit=payload.get("unit"),
            scenarios=payload.get("scenarios"),
            sensitivity=payload.get("sensitivity"),
        ).to_dict()
    if operation == "comparability":
        comparison = comparability(
            payload.get("facts", []),
            allow_currency_independent=payload.get("allow_currency_independent", False),
        )
        return {
            "operation": "comparability",
            "value": comparison["comparable"],
            "unit": None,
            "formula": "all required dimensions present and equal (currency may differ only when explicitly allowed)",
            "inputs": {
                "facts": payload.get("facts", []),
                "allow_currency_independent": payload.get("allow_currency_independent", False),
            },
            "warnings": comparison["reasons"],
            "status": "ok" if comparison["comparable"] else "blocked",
            **comparison,
        }
    raise ValueError(
        "不支持的计算类型；可选：growth、margin、cagr、unit_conversion、valuation_multiple、"
        "comparability、enterprise_value、fcf、dcf、ev_revenue、pe"
    )
