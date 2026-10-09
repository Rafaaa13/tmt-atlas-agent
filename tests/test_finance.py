import math
import unittest
from decimal import Decimal

import finance_engine as finance


class TestFinanceEngine(unittest.TestCase):
    def test_growth_uses_conventional_yoy(self):
        result = finance.growth("120", "100")
        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.value, 0.2)
        self.assertEqual(result.formula, "current / previous - 1 (previous > 0)")

    def test_growth_zero_prior_is_blocked_with_absolute_change(self):
        result = finance.growth(10, 0)
        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.value)
        self.assertEqual(result.inputs["absolute_change"], 10.0)
        self.assertIn("绝对变化", result.warnings[0])

    def test_growth_negative_prior_does_not_use_abs_denominator(self):
        result = finance.growth(10, -5)
        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.value)
        self.assertEqual(result.inputs["absolute_change"], 15.0)
        self.assertIn("不能用 abs(previous)", result.warnings[0])

    def test_finite_numeric_validation_rejects_bool_nan_inf(self):
        for value in (True, False, float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    finance.growth(value, 1)

    def test_numeric_string_and_decimal_are_accepted(self):
        result = finance.margin(Decimal("2.5"), "10")
        self.assertAlmostEqual(result.value, 0.25)

    def test_margin_requires_positive_denominator(self):
        for denominator in (0, -1):
            with self.subTest(denominator=denominator):
                result = finance.margin(10, denominator)
                self.assertEqual(result.status, "blocked")
                self.assertIsNone(result.value)
                self.assertIn("大于 0", result.warnings[0])

    def test_cagr_validates_positive_start_and_period_range(self):
        result = finance.cagr(100, 121, 2)
        self.assertAlmostEqual(result.value, 0.1, places=10)
        for start, end, periods in ((0, 100, 2), (-1, 100, 2), (100, -1, 2), (100, 121, 0), (100, 121, 101)):
            with self.subTest(start=start, end=end, periods=periods):
                self.assertEqual(finance.cagr(start, end, periods).status, "blocked")

    def test_unit_conversion_is_same_currency_only(self):
        result = finance.convert_unit(2, "USD million", "USD thousand")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value, 2000.0)
        blocked = finance.convert_unit(2, "USD million", "CNY million")
        self.assertEqual(blocked.status, "blocked")

    def test_valuation_multiple_requires_positive_denominator(self):
        result = finance.valuation_multiple(100, 20)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value, 5.0)
        for denominator in (0, -1):
            with self.subTest(denominator=denominator):
                self.assertEqual(finance.valuation_multiple(100, denominator).status, "blocked")

    def test_ev_revenue_and_pe_are_supported(self):
        ev_revenue = finance.ev_revenue(500, 100, unit="USD million")
        self.assertEqual(ev_revenue.operation, "ev_revenue")
        self.assertEqual(ev_revenue.value, 5.0)
        pe = finance.pe(600, 30, unit="USD million")
        self.assertEqual(pe.operation, "pe")
        self.assertEqual(pe.value, 20.0)
        self.assertEqual(finance.calculate({"operation": "valuation_multiple", "multiple_type": "pe", "numerator": 600, "denominator": 30})["operation"], "pe")

    def test_enterprise_value_components_follow_formula(self):
        result = finance.enterprise_value(1000, debt=200, preferred_equity=50, minority_interest=25, cash=125, unit="USD million")
        self.assertEqual(result.value, 1150.0)
        self.assertEqual(result.unit, "USD million")
        self.assertIn("market_cap + debt", result.formula)
        self.assertTrue(any("同一币种和单位" in warning for warning in result.warnings))

    def test_enterprise_value_rejects_negative_components_and_nonfinite(self):
        with self.assertRaises(ValueError):
            finance.enterprise_value(100, debt=-1)
        with self.assertRaises(ValueError):
            finance.enterprise_value(float("inf"))

    def test_fcf_uses_positive_capex_outflow_convention(self):
        result = finance.fcf(100, 30, unit="USD million")
        self.assertEqual(result.operation, "fcf")
        self.assertEqual(result.value, 70.0)
        blocked = finance.fcf(100, -30)
        self.assertEqual(blocked.status, "blocked")
        self.assertIn("正的现金流出", blocked.warnings[0])

    def _dcf_payload(self):
        return dict(
            base_fcf=100,
            forecast_growth=[0.10, 0.08, 0.06],
            wacc=0.10,
            terminal_growth=0.03,
            years=3,
            net_debt=50,
            shares=10,
            unit="USD million",
        )

    def test_dcf_requires_explicit_growth_and_returns_per_share_value(self):
        result = finance.dcf(**self._dcf_payload())
        self.assertEqual(result.status, "ok")
        self.assertIsInstance(result.value, float)
        self.assertEqual(result.unit, "USD million/share")
        self.assertEqual(result.inputs["forecast_growth"], [0.1, 0.08, 0.06])
        self.assertIn("WACC", result.formula)
        self.assertTrue(any("机械计算" in warning for warning in result.warnings))

    def test_dcf_blocks_invalid_assumptions(self):
        base = self._dcf_payload()
        cases = [
            (dict(base, base_fcf=0), "base_fcf"),
            (dict(base, years=0), "years"),
            (dict(base, years=11, forecast_growth=[0.1] * 11), "years"),
            (dict(base, wacc=0.03), "WACC"),
            (dict(base, shares=0), "shares"),
            (dict(base, forecast_growth=[0.1]), "forecast_growth"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):
                result = finance.dcf(**payload)
                self.assertEqual(result.status, "blocked")
                self.assertIsNone(result.value)
                self.assertTrue(any(expected in warning for warning in result.warnings))

    def test_dcf_warns_when_terminal_value_dominates(self):
        payload = self._dcf_payload()
        payload.update(wacc=0.06, terminal_growth=0.05)
        result = finance.dcf(**payload)
        self.assertEqual(result.status, "ok")
        self.assertTrue(any("终值" in warning and "敏感" in warning for warning in result.warnings))

    def test_dcf_supports_scenarios(self):
        payload = self._dcf_payload()
        payload["scenarios"] = [
            {"name": "base", "wacc": 0.10, "terminal_growth": 0.03},
            {"name": "higher-growth", "forecast_growth": [0.12, 0.10, 0.08], "wacc": 0.10, "terminal_growth": 0.03},
        ]
        result = finance.dcf(**payload)
        self.assertEqual(len(result.scenarios), 2)
        self.assertEqual(result.scenarios[0]["status"], "ok")
        self.assertGreater(result.scenarios[1]["value"], result.scenarios[0]["value"])

    def test_dcf_supports_wacc_growth_sensitivity_and_invalid_pairs(self):
        payload = self._dcf_payload()
        payload["sensitivity"] = {
            "wacc": [0.08, 0.10],
            "terminal_growth": [0.02, 0.03],
        }
        result = finance.dcf(**payload)
        self.assertEqual(result.sensitivity["wacc"], [0.08, 0.1])
        self.assertEqual(len(result.sensitivity["values"]), 2)
        self.assertEqual(len(result.sensitivity["values"][0]), 2)
        self.assertIsNone(
            finance.dcf(**dict(payload, sensitivity={"wacc": [0.03], "terminal_growth": [0.03]})).sensitivity["values"][0][0]
        )

    def _comparable_fact(self, **overrides):
        fact = {
            "metric_key": "revenue",
            "unit": "USD million",
            "currency": "USD",
            "period_type": "FY",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
            "accounting_basis": "GAAP",
            "scope": "consolidated",
        }
        fact.update(overrides)
        return fact

    def test_comparability_requires_all_dimensions_even_when_both_blank(self):
        first = self._comparable_fact(scope="")
        second = self._comparable_fact(scope="")
        result = finance.comparability([first, second])
        self.assertFalse(result["comparable"])
        self.assertTrue(any("scope" in reason and "缺少" in reason for reason in result["reasons"]))

    def test_comparability_fails_period_unit_currency_scope_and_accounting_mismatch(self):
        first = self._comparable_fact()
        second = self._comparable_fact(
            unit="USD billion",
            currency="CNY",
            period_start="2023-01-01",
            period_end="2023-12-31",
            accounting_basis="IFRS",
            scope="segment",
        )
        result = finance.comparability([first, second])
        self.assertFalse(result["comparable"])
        for dimension in ("unit", "currency", "period_start", "period_end", "accounting_basis", "scope"):
            self.assertTrue(any(dimension in reason for reason in result["reasons"]))

    def test_comparability_currency_independence_requires_explicit_parameter(self):
        first = self._comparable_fact(currency="USD")
        second = self._comparable_fact(currency="CNY")
        blocked = finance.comparability([first, second])
        self.assertFalse(blocked["comparable"])
        allowed = finance.comparability([first, second], allow_currency_independent=True)
        self.assertTrue(allowed["comparable"])

    def test_calculate_preserves_structured_api_for_all_operations(self):
        payloads = [
            {"operation": "growth", "current": 2, "previous": 1},
            {"operation": "margin", "numerator": 1, "denominator": 2},
            {"operation": "cagr", "start": 1, "end": 2, "periods": 2},
            {"operation": "unit_conversion", "value": 1, "from_unit": "USD million", "to_unit": "USD"},
            {"operation": "valuation_multiple", "numerator": 2, "denominator": 1},
            {"operation": "enterprise_value", "market_cap": 10},
            {"operation": "fcf", "cfo": 10, "capex": 2},
            {"operation": "dcf", **self._dcf_payload()},
        ]
        for payload in payloads:
            with self.subTest(operation=payload["operation"]):
                result = finance.calculate(payload)
                for key in ("operation", "value", "unit", "formula", "inputs", "warnings", "status"):
                    self.assertIn(key, result)

    def test_calculate_comparability_has_structured_response(self):
        facts = [self._comparable_fact(), self._comparable_fact()]
        result = finance.calculate({"operation": "comparability", "facts": facts})
        self.assertEqual(result["operation"], "comparability")
        self.assertTrue(result["value"])
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
