"""现金流演算、赠款等值、渠道加回与阻止发布的手算级测试。"""
import json
import subprocess
import sys
import unittest
from datetime import date
from fractions import Fraction
from pathlib import Path

from analysis_data.io import load
from analysis_data import cashflow as cf
from analysis_data.cashflow import (
    ContractError, analyze, days_30e, weighted_distribution, year_fraction,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
F = Fraction


def report(name):
    v, h = load(DATA / name)
    return analyze(v, h)


def tool_result(report_obj, scenario_id, tool_id):
    sc = next(s for s in report_obj["scenario_results"] if s["scenario"] == scenario_id)
    return next(t for t in sc["tools"] if t["id"] == tool_id)


def fr(x):
    return F(str(x))


class DayCountTests(unittest.TestCase):
    def test_30e_contract_formula(self):
        # 文档公式：360*(Y2-Y1)+30*(M2-M1)+min(D2,30)-min(D1,30)
        self.assertEqual(days_30e(date(2025, 1, 15), date(2025, 2, 15)), 30)
        # 月末对月末（二月平）：1/31 -> 2/28 为 28 天，2/28 -> 3/31 为 32 天
        self.assertEqual(days_30e(date(2025, 1, 31), date(2025, 2, 28)), 28)
        self.assertEqual(days_30e(date(2025, 2, 28), date(2025, 3, 31)), 32)

    def test_actual_365f_leap_year(self):
        # 2024 是闰年，全年 366 天
        self.assertEqual(year_fraction(date(2024, 1, 1), date(2025, 1, 1),
                                       "Actual/365F"), F(366, 365))
        self.assertEqual(year_fraction(date(2024, 1, 1), date(2024, 7, 1),
                                       "Actual/365F"), F(182, 365))
        self.assertEqual(year_fraction(date(2024, 7, 1), date(2025, 1, 1),
                                       "Actual/365F"), F(184, 365))


class HandComputed30ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = report("month-end-30e.json")

    def test_segments_principal_and_interest(self):
        # loan-30e：120 本金，两段各 30 个 30E 天，s-low 利率 1/10 -> 每段利息 1
        t = tool_result(self.r, "s-low", "loan-30e")
        segs = t["ledger"]["segments"]
        self.assertEqual([(s["days_30e"], s["principal_at_risk"], s["interest_accrued"])
                          for s in segs],
                         [(30, "120", "1"), (30, "120", "1")])
        # 费用日在付息日同日：先结息 1，再放款（无），再记费 3，再还本（无）
        events = [(e["date"], e["type"], e["amount"]) for e in t["ledger"]["cash_events"]]
        self.assertEqual(events, [
            ("2025-01-15", "disbursement", "120"),
            ("2025-02-15", "interest_payment", "1"),
            ("2025-02-15", "fee_payment", "3"),
            ("2025-03-15", "interest_payment", "1"),
            ("2025-03-15", "principal_payment", "120"),
        ])

    def test_grant_equivalent_three_scenarios(self):
        # 折现因子与 fx 全为 1：GE = 120 - (120 + 利息 + 3 费用)
        low = tool_result(self.r, "s-low", "loan-30e")["grant_equivalent"]
        mid = tool_result(self.r, "s-mid", "loan-30e")["grant_equivalent"]
        high = tool_result(self.r, "s-high", "loan-30e")["grant_equivalent"]
        self.assertEqual(fr(low["grant_equivalent"]), F(-5))       # 120-125
        self.assertEqual(fr(mid["grant_equivalent"]), F(-7))       # 利息 4 + 费用 3
        self.assertEqual(fr(high["grant_equivalent"]), F(-11))     # 利息 8 + 费用 3
        # 负等值如实保留，百分比分母是放款现值
        self.assertEqual(fr(high["grant_equivalent_pct"]), F(-11, 120))

    def test_month_end_30e_loan_eur_fx(self):
        # 60 EUR；月末两段 28+32=60 个 30E 天，利息 60*0.1*60/360 = 1 EUR
        t = tool_result(self.r, "s-low", "loan-e")
        self.assertEqual(sum(fr(s["interest_accrued"]) for s in t["ledger"]["segments"]), F(1))
        # fx=3/2：先换报告币再折现，放款现值 60*3/2=90
        self.assertEqual(fr(t["grant_equivalent"]["pv_disbursements"]), F(90))
        self.assertEqual(fr(t["grant_equivalent"]["grant_equivalent"]), F(-3, 2))
        # 高成本情景 fx=5/2：同一合同 GE 随曲线变化
        high = tool_result(self.r, "s-high", "loan-e")["grant_equivalent"]
        self.assertEqual(fr(high["pv_disbursements"]), F(150))
        self.assertEqual(fr(high["grant_equivalent"]), F(-5, 2))

    def test_grant_is_disbursement_only(self):
        t = tool_result(self.r, "s-low", "grant-1")
        self.assertTrue(t["is_grant"])
        self.assertEqual(fr(t["grant_equivalent"]["grant_equivalent"]), F(20))
        self.assertEqual(fr(t["grant_equivalent"]["grant_equivalent_pct"]), F(1))
        for rc in t["reconciliation"]:
            self.assertEqual(rc["diff"], "0")

    def test_channels_allocate_and_add_back(self):
        for sc in self.r["scenario_results"]:
            recon = sc["channel_reconciliation"]
            self.assertEqual(fr(recon["diff"]), F(0))
            # 渠道 GE 用冻结份额分配，份额之和为 1
            ch = {c["channel"]: c for c in sc["by_channel"]}
            loan = tool_result(self.r, sc["scenario"], "loan-30e")["grant_equivalent"]
            ge = fr(loan["grant_equivalent"])
            self.assertEqual(fr(ch["public"]["grant_equivalent"])
                             - F(20), ge * F(3, 5))   # public 还含全额赠款 20
            self.assertEqual(fr(ch["bank"]["grant_equivalent"]),
                             ge * F(2, 5) + fr(tool_result(self.r, sc["scenario"],
                                                           "loan-e")
                                               ["grant_equivalent"]["grant_equivalent"]))

    def test_country_groups_no_double_count(self):
        for sc in self.r["scenario_results"]:
            total = sum((fr(c["pv_disbursements"]) for c in sc["by_country"]), F(0))
            self.assertEqual(total, fr(sc["portfolio"]["pv_disbursements"]))

    def test_weighted_quantiles_and_mean(self):
        # 组合 GE：s-low=27/2, s-mid=11, s-high=13/2，权重 1/4,1/2,1/4
        dist = self.r["distribution"]["portfolio"]["grant_equivalent"]
        ordered = [F(13, 2), F(11), F(27, 2)]
        self.assertEqual(fr(dist["q1/20"]), ordered[0])   # 首个累计权重 1/4 >= 1/20
        self.assertEqual(fr(dist["q1/2"]), ordered[1])    # 累计到 3/4 才 >= 1/2
        self.assertEqual(fr(dist["q19/20"]), ordered[2])
        self.assertEqual(fr(dist["mean"]),
                         (F(27, 2) + 2 * F(11) + F(13, 2)) / 4)


class LeapYearCapitalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = report("leap-year-capitalization.json")

    def test_segment_break_on_disbursement_and_capitalization(self):
        t = tool_result(self.r, "s1", "loan-1")
        segs = t["ledger"]["segments"]
        # 分批放款 7/1 切段；1/1 资本化日再切段；闰年真实天数
        self.assertEqual([(s["start"], s["end"], s["year_fraction"], s["principal_at_risk"])
                          for s in segs],
                         [("2024-01-01", "2024-07-01", "182/365", "100"),
                          ("2024-07-01", "2025-01-01", "184/365", "120"),
                          ("2025-01-01", "2026-01-01", "1", "47828/365")])

    def test_capitalized_interest_joins_principal(self):
        t = tool_result(self.r, "s1", "loan-1")
        # 两段应计 364/73 + 2208/365 = 4028/365，资本化后本金 = 120 + 4028/365
        cap = next(e for e in t["ledger"]["non_cash_events"]
                   if e["type"] == "interest_capitalized")
        self.assertEqual(fr(cap["amount"]), F(4028, 365))
        self.assertEqual(fr(segs_principal(t, "2025-01-01")), F(47828, 365))
        for rc in t["reconciliation"]:
            self.assertEqual(rc["diff"], "0", rc)

    def test_discount_interpolation_used_for_pv(self):
        # s1 折现节点 1 -> 9/10，2026-01-01 现值用 9/10；放款节点 df=1
        t = tool_result(self.r, "s1", "loan-1")["grant_equivalent"]
        self.assertEqual(fr(t["pv_disbursements"]), F(120))
        # 插值也保持精确有理数；近似值仅用于人工核对
        self.assertEqual(fr(t["grant_equivalent"]), F(-88743, 9125))
        self.assertAlmostEqual(float(fr(t["grant_equivalent"])),
                               -9.725260273972594, places=9)
        s2 = tool_result(self.r, "s2", "loan-1")["grant_equivalent"]
        self.assertEqual(fr(s2["grant_equivalent"]), F(-52872, 9125))


def segs_principal(t, start):
    return next(s["principal_at_risk"] for s in t["ledger"]["segments"]
                if s["start"] == start)


class CapitalizedFeeTests(unittest.TestCase):
    def test_fee_capitalized_then_repaid(self):
        r = report("capitalized-fee.json")
        t = tool_result(r, "s1", "loan-capfee")
        # 第一段利息 120*0.1/12=1；2/15 费用 10 资本化 -> 本金 130
        # 第二段利息 130*0.1/12=13/12；到期还本 130、付息 25/12
        self.assertEqual([s["interest_accrued"] for s in t["ledger"]["segments"]],
                         ["1", "13/12"])
        self.assertEqual(fr(t["grant_equivalent"]["pv_repayments"]), F(1585, 12))
        self.assertEqual(fr(t["grant_equivalent"]["grant_equivalent"]), F(-145, 12))
        for rc in t["reconciliation"]:
            self.assertEqual(rc["diff"], "0", rc)


class NormalMaterialTests(unittest.TestCase):
    def test_hand_computed_grant_equivalent(self):
        r = report("normal.json")
        s1 = tool_result(r, "s1", "loan-1")["grant_equivalent"]
        s2 = tool_result(r, "s2", "loan-1")["grant_equivalent"]
        # s1：利息 10，df 9/10 -> 偿还 PV (100+10)*0.9=99，GE=1
        self.assertEqual(fr(s1["pv_repayments"]), F(99))
        self.assertEqual(fr(s1["grant_equivalent"]), F(1))
        # s2：利息 20，df 4/5 -> 120*0.8=96，GE=4
        self.assertEqual(fr(s2["pv_repayments"]), F(96))
        self.assertEqual(fr(s2["grant_equivalent"]), F(4))
        # 等权均值 = (1+4)/2 = 5/2，不是先平均利率再算
        dist = r["distribution"]["portfolio"]["grant_equivalent"]
        self.assertEqual(fr(dist["mean"]), F(45, 2))  # 含赠款各 20：(21+24)/2

    def test_annual_borrower_schedule_is_cash_not_ge(self):
        r = report("normal.json")
        sc = next(s for s in r["scenario_results"] if s["scenario"] == "s1")
        annual = {t["id"]: t["annual"] for t in sc["tools"]}
        row_2026 = next(a for a in annual["loan-1"] if a["year"] == 2026)
        self.assertEqual(fr(row_2026["principal_rc"]), F(100))
        self.assertEqual(fr(row_2026["interest_rc"]), F(10))
        # 年度本息费是现金口径，绝不含赠款等值
        self.assertEqual(fr(row_2026["debt_service_rc"]), F(110))


class CurveInterpolationTests(unittest.TestCase):
    def _curve(self):
        return cf.Curve("U", {
            date(2025, 1, 1): (F(1), F(1)),
            date(2025, 3, 1): (F(4, 9), F(9, 4)),
        })

    def test_log_linear_midpoint(self):
        c = self._curve()
        mid = date(2025, 1, 31)  # 30/59 的位置
        # log 线性：sqrt(1 * 4/9) 当权重为 1/2 附近；用公式直接核对
        w = F((mid - date(2025, 1, 1)).days,
              (date(2025, 3, 1) - date(2025, 1, 1)).days)
        import math
        expected = math.exp((1 - float(w)) * math.log(1.0)
                            + float(w) * math.log(4 / 9))
        self.assertAlmostEqual(c.factor("discount", mid), expected)
        self.assertEqual(c.factor("discount", date(2025, 1, 1)), F(1))  # 节点取精确值

    def test_no_extrapolation(self):
        c = self._curve()
        with self.assertRaises(ContractError):
            c.factor("discount", date(2024, 12, 31))
        with self.assertRaises(ContractError):
            c.factor("fx", date(2025, 3, 2))


class BlockingTests(unittest.TestCase):
    def test_missing_curve_quote_blocks(self):
        with self.assertRaises(ContractError) as ctx:
            report("missing-curve-date.json")
        self.assertEqual(ctx.exception.tool, "loan-1")
        self.assertEqual(ctx.exception.on_date, "2026-01-01")

    def test_over_repayment_blocks(self):
        with self.assertRaises(ContractError) as ctx:
            report("over-repayment.json")
        self.assertEqual(ctx.exception.tool, "loan-over")
        self.assertIn("超过未偿本金", str(ctx.exception))

    def test_missing_rate_quote_blocks(self):
        with self.assertRaises(ContractError) as ctx:
            report("missing-rate-quote.json")
        self.assertEqual(ctx.exception.tool, "loan-norate")
        self.assertEqual(ctx.exception.on_date, "2025-01-15")

    def test_duplicate_tool_id_blocks(self):
        v, _ = load(DATA / "normal.json")
        v["tools"].append(json.loads(json.dumps(v["tools"][0])))
        with self.assertRaises(ContractError):
            analyze(v, "x")

    def test_channel_share_not_one_blocks(self):
        v, _ = load(DATA / "normal.json")
        v["channel_shares"][0]["share"] = "1/5"  # 3/5 -> 1/5，loan-1 份额和变为 3/5
        with self.assertRaises(ContractError):
            analyze(v, "x")

    def test_weight_sum_not_one_blocks(self):
        v, _ = load(DATA / "normal.json")
        v["scenarios"][0]["weight"] = "1/3"
        with self.assertRaises(ContractError):
            analyze(v, "x")

    def test_disbursement_after_zero_balance_blocks(self):
        v, _ = load(DATA / "normal.json")
        loan = next(t for t in v["tools"] if t["id"] == "loan-1")
        # 2026-01-01 先把 100 还清（本金清零），次日再追加一笔放款并延至
        # 2026-02-01 结清：零本金期间无法连续计息，必须拦截
        loan["maturity"] = "2026-02-01"
        loan["disbursements"].append({"date": "2026-01-02", "amount": "50"})
        loan["repayments"] = [
            {"date": "2026-01-01", "principal": "100"},
            {"date": "2026-02-01", "principal": "50"},
        ]
        loan["interest_payment_dates"] = ["2026-01-01", "2026-02-01"]
        loan["reset_dates"].append("2026-01-02")
        for s in v["scenarios"]:
            s["rates"]["loan-1"]["2026-01-02"] = "1/10"
            c = s["curves"]["U"]
            c["2026-01-02"] = c["2026-01-01"]
            c["2026-02-01"] = c["2026-01-01"]
        with self.assertRaises(ContractError) as ctx:
            analyze(v, "x")
        self.assertEqual(ctx.exception.tool, "loan-1")
        self.assertEqual(ctx.exception.on_date, "2026-01-02")

    def test_first_reset_after_first_disbursement_blocks(self):
        v, _ = load(DATA / "normal.json")
        loan = next(t for t in v["tools"] if t["id"] == "loan-1")
        loan["reset_dates"] = ["2025-02-01"]
        for s in v["scenarios"]:
            s["rates"]["loan-1"] = {"2025-02-01": "1/10"}
        with self.assertRaises(ContractError):
            analyze(v, "x")


class QuantileDefinitionTests(unittest.TestCase):
    def test_left_continuous_inverse(self):
        pairs = [(F(1, 4), F(2)), (F(1, 2), F(5)), (F(1, 4), F(9))]
        out = weighted_distribution(pairs)
        self.assertEqual(fr(out["q1/20"]), F(2))
        self.assertEqual(fr(out["q1/2"]), F(5))
        self.assertEqual(fr(out["q19/20"]), F(9))
        self.assertEqual(fr(out["mean"]), F(21, 4))


class EndToEndInterpolationTests(unittest.TestCase):
    def _two_node_manifest(self, discount_end, fx_end):
        import copy
        v, _ = load(DATA / "normal.json")
        v = copy.deepcopy(v)
        # 现金事件 2026-01-01 恰在 2025-01-01 与 2027-01-01 的时间中点
        for s in v["scenarios"]:
            s["curves"]["U"] = {
                "2025-01-01": {"discount": "1", "fx": "1"},
                "2027-01-01": {"discount": discount_end, "fx": fx_end},
            }
        return v

    def test_discount_log_linear_midpoint_in_ge(self):
        v = self._two_node_manifest("1/4", "1")  # 中点 df = sqrt(1/4) = 1/2
        r = analyze(v, "x")
        t = tool_result(r, "s1", "loan-1")
        # s1 利息 10，2026 偿还 110 * 1/2 = 55；放款 PV 100 -> GE 45
        self.assertEqual(fr(t["grant_equivalent"]["grant_equivalent"]), F(45))

    def test_fx_log_linear_midpoint_in_ge(self):
        v = self._two_node_manifest("1", "4")  # 中点 fx = sqrt(4) = 2
        r = analyze(v, "x")
        t = tool_result(r, "s1", "loan-1")
        # 放款 fx=1 -> PV 100；偿还 110 * fx 2 = 220 -> GE -120
        self.assertEqual(fr(t["grant_equivalent"]["grant_equivalent"]), F(-120))


class BindingTests(unittest.TestCase):
    def test_fingerprint_changes_with_manifest(self):
        r = report("normal.json")
        self.assertTrue(cf.verify_binding(r, r["binding"]["manifest_sha256"]))
        # 改曲线（=改清单哈希）必须使旧报告绑定失败，迫使新建结果
        self.assertFalse(cf.verify_binding(r, "0" * 64))


class CliTests(unittest.TestCase):
    def test_analyze_blocked_exit_code(self):
        proc = subprocess.run(
            [sys.executable, "-I", "-c",
             "import sys;sys.path.insert(0,%r);"
             "from analysis_data.cli import main;"
             "sys.argv=['cli','analyze',%r];main()"
             % (str(ROOT), str(DATA / "over-repayment.json"))],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("loan-over", payload["error"])

    def test_analyze_published_and_verify(self):
        proc = subprocess.run(
            [sys.executable, "-I", "-c",
             "import sys;sys.path.insert(0,%r);"
             "from analysis_data.cli import main;"
             "sys.argv=['cli','analyze',%r];main()"
             % (str(ROOT), str(DATA / "normal.json"))],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "published")
        out = DATA / "_tmp_report.json"
        out.write_text(proc.stdout, encoding="utf8")
        try:
            ver = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys;sys.path.insert(0,%r);"
                 "from analysis_data.cli import main;"
                 "sys.argv=['cli','verify',%r,'--report',%r];main()"
                 % (str(ROOT), str(DATA / "normal.json"), str(out))],
                capture_output=True, text=True, cwd=str(ROOT))
            self.assertEqual(ver.returncode, 0)
            self.assertTrue(json.loads(ver.stdout)["binding_ok"])
        finally:
            out.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
