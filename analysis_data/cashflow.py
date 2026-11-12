"""合同现金流演算、赠款等值与组合汇总。

口径要点（与 docs/data-contract.md 对应）：
- 金额、利率、份额、权重一律解析为 Fraction，保留精确有理数；曲线节点为正有理数，
  节点之间按 log 线性插值时才转为 float，禁止外推。
- 同日事件顺序：先结算上一段利息（支付或资本化），再放款，再记费，再还本。
- 区间中途发生任何事件都切段；利率取最近且不晚于切段日的重置日报价，区间内不换利率。
- 赠款等值＝放款现值－本金/利息/必要费用偿还现值；现金流先按发生日 fx 换报告币再折现。
- 每个差额都由逐段利息台账与现金事件台账支撑，不把赠款等值当作现金或豁免。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from .io import load

METHOD_VERSION = "cashflow-1"
DAY_COUNTS = ("Actual/365F", "30E/360")
QUANTILES = (Fraction(1, 20), Fraction(1, 2), Fraction(19, 20))


class ContractError(ValueError):
    """可定位到工具与日期的合同/曲线错误，出现即阻止组合发布。"""

    def __init__(self, message, tool=None, on_date=None):
        self.tool = tool
        self.on_date = on_date.isoformat() if isinstance(on_date, date) else on_date
        parts = [f"工具 {tool}" if tool else None,
                 f"日期 {self.on_date}" if self.on_date else None]
        loc = "（{}）".format("，".join(p for p in parts if p))
        super().__init__(f"{message}{loc}")


# ---------- 基础解析 ----------

def iso_date(value, tool=None, label="日期"):
    if not isinstance(value, str):
        raise ContractError(f"{label}必须是 ISO 字符串", tool)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ContractError(f"{label}不是合法公历: {value!r}", tool, value)


def rational(value, label, *, tool=None, nonneg=False, positive=False):
    """把字符串分数/整数解析为 Fraction；布尔与浮点写法一律拒绝。"""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ContractError(f"{label}必须是有理数字符串", tool)
    try:
        out = Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise ContractError(f"{label}不是有理数: {value!r}", tool)
    if positive and out <= 0:
        raise ContractError(f"{label}必须为正: {value!r}", tool)
    if nonneg and out < 0:
        raise ContractError(f"{label}不能为负: {value!r}", tool)
    return out


def days_30e(d1: date, d2: date) -> int:
    return (360 * (d2.year - d1.year) + 30 * (d2.month - d1.month)
            + min(d2.day, 30) - min(d1.day, 30))


def year_fraction(d1: date, d2: date, convention: str, tool=None) -> Fraction:
    if convention == "Actual/365F":
        return Fraction((d2 - d1).days, 365)
    if convention == "30E/360":
        return Fraction(days_30e(d1, d2), 360)
    raise ContractError(f"未知计息规则 {convention!r}", tool)


# ---------- 曲线：正因子，log 线性插值，禁止外推 ----------

@dataclass(frozen=True)
class Curve:
    currency: str
    nodes: dict  # date -> (discount: Fraction, fx: Fraction)

    def factor(self, kind: str, on: date, tool=None):
        """节点取已登记的精确有理数；节点间 log 线性插值后转回 Fraction，
        保证下游 PV、费用与汇总全程为精确有理数（log/exp 仅在插值内部使用浮点）。"""
        pts = self.nodes
        if on in pts:
            return pts[on][0 if kind == "discount" else 1]
        keys = sorted(pts)
        if on < keys[0] or on > keys[-1]:
            raise ContractError(
                f"{kind} 曲线在 {on.isoformat()} 超出报价范围，禁止外推", tool, on)
        lo = max(k for k in keys if k < on)
        hi = min(k for k in keys if k > on)
        w = Fraction((on - lo).days, (hi - lo).days)
        idx = 0 if kind == "discount" else 1
        lo_v, hi_v = pts[lo][idx], pts[hi][idx]
        interpolated = math.exp(float((1 - w) * math.log(float(lo_v))
                                      + w * math.log(float(hi_v))))
        return Fraction(interpolated)


def _parse_curve(scenario, currency, tool=None):
    table = scenario.get("curves", {}).get(currency)
    if not isinstance(table, dict) or not table:
        raise ContractError(f"情景 {scenario['id']} 缺币种 {currency} 的曲线", tool)
    nodes = {}
    for ds, row in table.items():
        d = iso_date(ds, tool, "曲线节点日期")
        if not isinstance(row, dict):
            raise ContractError("曲线节点必须含 discount 与 fx", tool, d)
        nodes[d] = (rational(row.get("discount"), "discount", tool=tool, positive=True),
                    rational(row.get("fx"), "fx", tool=tool, positive=True))
    return Curve(currency, nodes)


# ---------- 结构校验（不演算，只保证日历与身份完整） ----------

def _validate_manifest(v):
    if not isinstance(v, dict):
        raise ContractError("顶层必须为对象")
    valuation = iso_date(v.get("valuation_date"), label="valuation_date")
    if not isinstance(v.get("report_currency"), str) or not v["report_currency"]:
        raise ContractError("report_currency 缺失")
    tools, scenarios = v.get("tools"), v.get("scenarios")
    if not isinstance(tools, list) or not tools:
        raise ContractError("tools 不能为空")
    if not isinstance(scenarios, list) or not scenarios:
        raise ContractError("scenarios 不能为空")

    seen_tools, seen_scenarios = set(), set()
    parsed_tools = []
    for t in tools:
        tid = t.get("id")
        if not isinstance(tid, str) or not tid:
            raise ContractError("工具 id 缺失")
        if tid in seen_tools:
            raise ContractError("同一工具 id 出现多份合同", tid)
        seen_tools.add(tid)
        parsed_tools.append(_validate_tool(t, valuation))

    total_weight = Fraction(0)
    for s in scenarios:
        sid = s.get("id")
        if not isinstance(sid, str) or not sid:
            raise ContractError("情景 id 缺失")
        if sid in seen_scenarios:
            raise ContractError("情景 id 重复", on_date=None)
        seen_scenarios.add(sid)
        w = rational(s.get("weight"), f"情景 {sid} 权重", nonneg=True)
        if w <= 0:
            raise ContractError(f"情景 {sid} 权重必须为正")
        total_weight += w
        if not isinstance(s.get("rates"), dict) or not isinstance(s.get("curves"), dict):
            raise ContractError(f"情景 {sid} 缺 rates/curves")
    if total_weight != 1:
        raise ContractError(f"情景权重之和必须为 1，实际 {total_weight}")

    # 重置日报价覆盖、曲线币种覆盖
    currencies = {t["currency"] for t in parsed_tools}
    for s in scenarios:
        for t in parsed_tools:
            tid = t["id"]
            if t["is_grant"]:
                continue
            registered = s["rates"].get(tid)
            if not isinstance(registered, dict) or not registered:
                raise ContractError(f"情景 {s['id']} 缺利率登记", tid)
            have = {iso_date(d, tid, "重置日") for d in registered}
            missing = set(t["reset_dates"]) - have
            if missing:
                d = min(missing)
                raise ContractError(f"情景 {s['id']} 缺重置日报价", tid, d)
            for ds, rv in registered.items():
                rational(rv, f"情景 {s['id']} 利率", tool=tid, nonneg=True)
        for ccy in currencies:
            curve = _parse_curve(s, ccy)
            if valuation not in curve.nodes:
                raise ContractError(f"情景 {s['id']} 曲线缺估值日节点 {ccy}")
            for t in parsed_tools:
                if t["currency"] != ccy:
                    continue
                out_of_range = [d for d in t["event_dates"]
                                if d < min(curve.nodes) or d > max(curve.nodes)]
                if out_of_range:
                    raise ContractError(
                        f"情景 {s['id']} 曲线未覆盖现金事件，禁止外推",
                        t["id"], min(out_of_range))

    channels = _validate_channels(v, seen_tools)
    return valuation, parsed_tools, channels


def _validate_tool(t, valuation):
    tid = t["id"]
    if not isinstance(t.get("currency"), str) or not t["currency"]:
        raise ContractError("currency 缺失", tid)
    if not isinstance(t.get("country"), str) or not t["country"]:
        raise ContractError("country 缺失", tid)
    convention = t.get("day_count")
    if convention not in DAY_COUNTS:
        raise ContractError(f"计息规则必须是 {DAY_COUNTS}", tid)

    disb = [(iso_date(d["date"], tid, "放款日"),
             rational(d.get("amount"), "放款本金", tool=tid, positive=True))
            for d in t.get("disbursements", [])]
    if not disb:
        raise ContractError("至少一笔放款", tid)
    fees = []
    for f in t.get("fees", []):
        fd = iso_date(f["date"], tid, "费用日")
        treatment = f.get("treatment")
        if treatment not in ("pay", "capitalize"):
            raise ContractError("费用 treatment 必须是 pay 或 capitalize", tid, fd)
        fees.append((fd, rational(f.get("amount"), "费用额", tool=tid, positive=True), treatment))

    is_grant = t.get("maturity") is None
    if is_grant:
        for field_name in ("repayments", "interest_payment_dates",
                           "reset_dates", "capitalization_dates"):
            if t.get(field_name):
                raise ContractError(f"赠款不得有 {field_name}", tid)
        if fees:
            raise ContractError("赠款不得挂费用", tid)
    else:
        maturity = iso_date(t["maturity"], tid, "到期日")
        repayments = []
        rows = t.get("repayments", [])
        if not rows:
            raise ContractError("贷款必须有还本安排", tid)
        for i, r in enumerate(rows):
            rd = iso_date(r["date"], tid, "还本日")
            principal = r["principal"]
            if principal == "all-outstanding":
                if i != len(rows) - 1:
                    raise ContractError("all-outstanding 只允许最后一次还本", tid, rd)
                repayments.append((rd, "all-outstanding"))
            else:
                repayments.append((rd, rational(principal, "还本额", tool=tid, positive=True)))
        if repayments[-1][0] != maturity:
            raise ContractError("最后一次还本必须在到期日", tid, maturity)
        ipd = [iso_date(d, tid, "付息日") for d in t.get("interest_payment_dates", [])]
        cap = [iso_date(d, tid, "资本化日") for d in t.get("capitalization_dates", [])]
        reset = [iso_date(d, tid, "重置日") for d in t.get("reset_dates", [])]
        if not ipd or maturity not in ipd:
            raise ContractError("到期日必须付息结清", tid, maturity)
        overlap = set(ipd) & set(cap)
        if overlap:
            raise ContractError("同一日不能既付息又资本化", tid, min(overlap))
        if not reset:
            raise ContractError("贷款必须至少一个重置日", tid)
        first_disbursement = min(d for d, _ in disb)
        if min(reset) > first_disbursement:
            raise ContractError(
                "首个重置日晚于首次放款，第一段无可用利率", tid, first_disbursement)
        all_dates = [d for d, _ in disb] + [d for d, _ in repayments] + ipd + cap + reset + [d for d, _, _ in fees]
        for d in all_dates:
            if d < valuation:
                raise ContractError("现金事件早于估值日", tid, d)
            if d > maturity:
                raise ContractError("现金事件晚于到期日", tid, maturity)
        if min(d for d, _ in disb) < valuation:
            raise ContractError("放款早于估值日", tid)
        event_dates = sorted(set(all_dates))
        return {"id": tid, "country": t["country"], "currency": t["currency"],
                "day_count": convention, "disbursements": disb, "fees": fees,
                "maturity": maturity, "repayments": repayments,
                "interest_payment_dates": set(ipd), "capitalization_dates": set(cap),
                "reset_dates": sorted(set(reset)), "event_dates": event_dates,
                "is_grant": False}

    for d, _ in disb:
        if d < valuation:
            raise ContractError("放款早于估值日", tid)
    return {"id": tid, "country": t["country"], "currency": t["currency"],
            "day_count": convention, "disbursements": disb, "fees": fees,
            "maturity": None, "repayments": [], "interest_payment_dates": set(),
            "capitalization_dates": set(), "reset_dates": [],
            "event_dates": sorted(d for d, _ in disb), "is_grant": True}


def _validate_channels(v, tool_ids):
    edges = v.get("channel_shares")
    if not isinstance(edges, list) or not edges:
        raise ContractError("channel_shares 不能为空")
    grouped = {}
    for e in edges:
        tid, channel = e.get("tool"), e.get("channel")
        if tid not in tool_ids:
            raise ContractError(f"渠道引用了未知工具 {tid!r}", tid)
        if not isinstance(channel, str) or not channel:
            raise ContractError("渠道名缺失", tid)
        share = rational(e.get("share"), f"渠道份额 {tid}/{channel}",
                         tool=tid, positive=True)
        grouped.setdefault(tid, []).append((channel, share))
    missing = tool_ids - set(grouped)
    if missing:
        raise ContractError(f"工具缺渠道份额: {sorted(missing)}")
    for tid, rows in grouped.items():
        total = sum((s for _, s in rows), Fraction(0))
        if total != 1:
            raise ContractError(f"渠道份额之和必须为 1，实际 {total}", tid)
    return grouped


# ---------- 单工具逐段演算 ----------

def _rate_at(rates: dict, reset_dates, segment_start: date, tid):
    # 只承认合同重置日：取最近且不晚于切段日的重置日，再读该日已登记报价
    candidates = [d for d in reset_dates if d <= segment_start]
    if not candidates:
        raise ContractError("放款/切段时没有不晚于该日的重置利率", tid, segment_start)
    return rates[max(candidates)]


def run_tool(tool: dict, scenario: dict, curve: Curve, valuation: date):
    tid, ccy = tool["id"], tool["currency"]
    rates = {iso_date(d, tid, "利率登记日"): rational(r, "利率", tool=tid, nonneg=True)
             for d, r in scenario["rates"].get(tid, {}).items()}
    disb_map, fee_map, repay_map = {}, {}, {}
    for d, a in tool["disbursements"]:
        disb_map[d] = disb_map.get(d, Fraction(0)) + a
    for d, a, treatment in tool["fees"]:
        fee_map.setdefault(d, []).append((a, treatment))
    for d, p in tool["repayments"]:
        repay_map.setdefault(d, []).append(p)

    principal = Fraction(0)
    accrued = Fraction(0)
    segments, cash_events, non_cash_events = [], [], []
    prev = None
    last_segment_end = None

    for d in tool["event_dates"]:
        # 1) 结算上一段利息（先切段计息，再按本日身份支付或资本化）。
        #    本金为零的期间不计息；下一段从再次承担本金的当日重新起算。
        if prev is not None and not tool["is_grant"] and principal > 0:
            yf = year_fraction(prev, d, tool["day_count"], tid)
            rate = _rate_at(rates, tool["reset_dates"], prev, tid)
            interest = principal * rate * yf
            accrued += interest
            segments.append({
                "start": prev.isoformat(), "end": d.isoformat(),
                "days_actual": (d - prev).days, "days_30e": days_30e(prev, d),
                "year_fraction": str(yf), "rate": str(rate),
                "principal_at_risk": str(principal), "interest_accrued": str(interest),
            })
            last_segment_end = d
        if d in tool["capitalization_dates"]:
            if accrued < 0:
                raise ContractError("应计利息异常", tid, d)
            if accrued > 0:
                non_cash_events.append({"date": d.isoformat(),
                                        "type": "interest_capitalized",
                                        "amount": str(accrued)})
            principal += accrued
            accrued = Fraction(0)
        elif d in tool["interest_payment_dates"]:
            if accrued > 0:
                cash_events.append({"date": d.isoformat(), "type": "interest_payment",
                                    "currency": ccy, "amount": str(accrued)})
            accrued = Fraction(0)

        # 2) 放款
        if d in disb_map:
            amount = disb_map[d]
            if principal == 0 and last_segment_end is not None and not tool["is_grant"]:
                raise ContractError(
                    "本金清零后再放款：零本金期间无法连续计息，请拆分工具", tid, d)
            cash_events.append({"date": d.isoformat(), "type": "disbursement",
                                "currency": ccy, "amount": str(amount)})
            principal += amount

        # 3) 费用：当日另记，capitalize 才进本金
        for amount, treatment in fee_map.get(d, ()):
            if treatment == "pay":
                cash_events.append({"date": d.isoformat(), "type": "fee_payment",
                                    "currency": ccy, "amount": str(amount)})
            else:
                non_cash_events.append({"date": d.isoformat(), "type": "fee_capitalized",
                                        "amount": str(amount)})
                principal += amount

        # 4) 还本
        for p in repay_map.get(d, ()):
            amount = principal if p == "all-outstanding" else p
            if amount <= 0:
                raise ContractError("还本时未偿本金为零", tid, d)
            if amount > principal:
                raise ContractError(
                    f"还本 {amount} 超过未偿本金 {principal}", tid, d)
            cash_events.append({"date": d.isoformat(), "type": "principal_payment",
                                "currency": ccy, "amount": str(amount)})
            principal -= amount
        prev = d

    if not tool["is_grant"]:
        if principal != 0:
            raise ContractError(f"到期仍有未偿本金 {principal}", tid, tool["maturity"])
        if accrued != 0:
            raise ContractError(f"到期仍有未结利息 {accrued}", tid, tool["maturity"])

    return _evaluate(tid, tool, cash_events, non_cash_events, segments, curve, valuation)


def _evaluate(tid, tool, cash_events, non_cash_events, segments, curve, valuation):
    pv_disb = pv_repay = Fraction(0)
    annual = {}
    for ev in cash_events:
        d = iso_date(ev["date"], tid)
        amount = Fraction(ev["amount"])
        fx = curve.factor("fx", d, tid)
        df = curve.factor("discount", d, tid)
        rc = amount * fx
        pv = rc * df
        ev["fx"] = str(fx)
        ev["report_amount"] = str(rc)
        ev["present_value"] = str(pv)
        row = annual.setdefault(d.year, {"year": d.year, "disbursements_rc": Fraction(0),
                                         "principal_rc": Fraction(0),
                                         "interest_rc": Fraction(0),
                                         "fees_rc": Fraction(0)})
        kind = ev["type"]
        if kind == "disbursement":
            pv_disb += pv
            row["disbursements_rc"] += rc
        else:
            pv_repay += pv
            row[{"principal_payment": "principal_rc",
                 "interest_payment": "interest_rc",
                 "fee_payment": "fees_rc"}[kind]] += rc

    ge = pv_disb - pv_repay
    ge_pct = ge / pv_disb if pv_disb > 0 else None
    annual_rows = []
    for year in sorted(annual):
        row = annual[year]
        row["debt_service_rc"] = (row["principal_rc"] + row["interest_rc"]
                                  + row["fees_rc"])
        annual_rows.append({k: (_num(v) if k.endswith("_rc") else v)
                            for k, v in row.items()})
    return {
        "id": tid,
        "country": tool["country"],
        "currency": tool["currency"],
        "is_grant": tool["is_grant"],
        "ledger": {"segments": segments, "cash_events": cash_events,
                   "non_cash_events": non_cash_events},
        "grant_equivalent": {
            "pv_disbursements": _num(pv_disb),
            "pv_repayments": _num(pv_repay),
            "grant_equivalent": _num(ge),
            "grant_equivalent_pct": _num(ge_pct),
        },
        "annual": [dict(row, **{k: _num(v) for k, v in row.items() if k.endswith("_rc")})
                   for row in annual_rows],
        "reconciliation": _reconcile(tool, cash_events, non_cash_events, segments),
    }


def _reconcile(tool, cash_events, non_cash_events, segments):
    """三条恒等式：每个差额都能回到具体现金事件。"""
    def total(kind):
        return sum((Fraction(e["amount"]) for e in cash_events if e["type"] == kind),
                   Fraction(0))
    disb = total("disbursement")
    principal_paid = total("principal_payment")
    interest_paid = total("interest_payment")
    fees_paid = total("fee_payment")
    cap_interest = sum((Fraction(e["amount"]) for e in non_cash_events
                        if e["type"] == "interest_capitalized"), Fraction(0))
    cap_fees = sum((Fraction(e["amount"]) for e in non_cash_events
                    if e["type"] == "fee_capitalized"), Fraction(0))
    interest_accrued = sum((Fraction(s["interest_accrued"]) for s in segments), Fraction(0))
    fee_scheduled = sum((a for _, a, _ in tool["fees"]), Fraction(0))

    def identity(name, lhs, rhs):
        return {"name": name, "lhs": str(lhs), "rhs": str(rhs), "diff": str(lhs - rhs)}

    if tool["is_grant"]:
        # 赠款：放款不构成债务，任何还本/付息/费用都必须为零
        zero = principal_paid + interest_paid + fees_paid + cap_interest + cap_fees
        return [
            identity("赠款：放款额 = 无偿还义务下的资金支持", disb, disb - zero),
            identity("赠款：所有偿还与资本化必须为零", zero, Fraction(0)),
            identity("赠款：合同费用必须为零", fee_scheduled, Fraction(0)),
        ]
    return [
        identity("本金：放款+资本化利息+资本化费用 = 还本",
                 disb + cap_interest + cap_fees, principal_paid),
        identity("利息：逐段应计 = 付息+资本化利息",
                 interest_accrued, interest_paid + cap_interest),
        identity("费用：合同费用 = 现付费用+资本化费用",
                 fee_scheduled, fees_paid + cap_fees),
    ]


# ---------- 组合汇总 ----------

def _aggregate(rows, key_pv="grant_equivalent"):
    pv_disb = sum((_fraction(r[key_pv]["pv_disbursements"]) for r in rows), Fraction(0))
    pv_repay = sum((_fraction(r[key_pv]["pv_repayments"]) for r in rows), Fraction(0))
    ge = pv_disb - pv_repay
    return {"pv_disbursements": _num(pv_disb),
            "pv_repayments": _num(pv_repay),
            "grant_equivalent": _num(ge),
            "grant_equivalent_pct": _num(ge / pv_disb if pv_disb > 0 else None)}


def _fraction(x):
    """报告中的数值都以有理数字符串保存，按需还原为 Fraction。"""
    return Fraction(x)


def run_scenario(tool_results, channels, scenario):
    portfolio = _aggregate(tool_results)
    by_channel = {}
    for r in tool_results:
        for channel, share in channels[r["id"]]:
            bucket = by_channel.setdefault(channel, [])
            bucket.append(_scale_tool(r, share))
    channel_rows = [{"channel": c, **_aggregate(rows)} for c, rows in sorted(by_channel.items())]
    by_country = {}
    for r in tool_results:
        by_country.setdefault(r["country"], []).append(r)
    country_rows = [{"country": c, **_aggregate(rows)} for c, rows in sorted(by_country.items())]

    channel_sum = sum((_fraction(row["pv_disbursements"]) for row in channel_rows), Fraction(0))
    return {
        "scenario": scenario["id"],
        "weight": str(scenario["weight_parsed"]),
        "tools": tool_results,
        "portfolio": portfolio,
        "by_channel": channel_rows,
        "by_country": country_rows,
        "channel_reconciliation": {
            "sum_channel_pv_disbursements": _num(channel_sum),
            "portfolio_pv_disbursements": portfolio["pv_disbursements"],
            "diff": _num(channel_sum - _fraction(portfolio["pv_disbursements"])),
        },
    }


def _scale_tool(result, share):
    ge = result["grant_equivalent"]
    scaled = {}
    for k, v in ge.items():
        if v is None:
            scaled[k] = None
        else:
            scaled[k] = _num(_fraction(v) * share)
    return {"id": result["id"], "share": str(share), "grant_equivalent": scaled}


# ---------- 跨情景：先逐情景演算，再做权重统计 ----------

def weighted_distribution(pairs):
    """pairs: [(weight: Fraction, value: Fraction|float)]。

    分位采用最小累计权重达到 q 的左连续逆分布；均值按权重线性。
    """
    ordered = sorted(pairs, key=lambda p: p[1])
    total_w = sum((w for w, _ in ordered), Fraction(0))
    mean = sum((w * v for w, v in ordered), Fraction(0)) / total_w
    out = {"mean": _num(mean)}
    cum = Fraction(0)
    for q in QUANTILES:
        cum = Fraction(0)
        chosen = ordered[-1][1]
        for w, v in ordered:
            cum += w
            if cum >= q:
                chosen = v
                break
        out[f"q{q}"] = _num(chosen)
    return out


def _annual_weighted_mean(scenario_outputs):
    years = sorted({row["year"] for s in scenario_outputs for t in s["tools"]
                    for row in t["annual"]})
    out = []
    for year in years:
        merged = {"year": year}
        for key in ("disbursements_rc", "principal_rc", "interest_rc",
                    "fees_rc", "debt_service_rc"):
            acc = Fraction(0)
            for s in scenario_outputs:
                w = Fraction(s["weight"])
                for t in s["tools"]:
                    row = next((r for r in t["annual"] if r["year"] == year), None)
                    if row:
                        acc += w * _fraction(row[key])
            merged[key] = _num(acc)
        out.append(merged)
    return out


# ---------- 顶层入口与绑定 ----------

def analyze(v, manifest_sha256):
    valuation, parsed_tools, channels = _validate_manifest(v)
    weights = {s["id"]: rational(s["weight"], "权重", positive=True)
               for s in v["scenarios"]}
    scenario_outputs, curve_summary = [], []
    for s in v["scenarios"]:
        s = dict(s, weight_parsed=weights[s["id"]])  # 不修改输入字典
        curves = {ccy: _parse_curve(s, ccy) for ccy in {t["currency"] for t in parsed_tools}}
        tool_results = [
            run_tool(t, s, curves[t["currency"]], valuation) for t in parsed_tools
        ]
        scenario_outputs.append(run_scenario(tool_results, channels, s))
        for ccy, curve in sorted(curves.items()):
            curve_summary.append({"scenario": s["id"], "currency": ccy,
                                  "node_dates": [d.isoformat() for d in sorted(curve.nodes)]})

    distribution = {"portfolio": {}, "tools": []}
    for metric in ("grant_equivalent", "grant_equivalent_pct"):
        distribution["portfolio"][metric] = weighted_distribution([
            (Fraction(s["weight"]), _fraction(s["portfolio"][metric]))
            for s in scenario_outputs])
    for t in parsed_tools:
        entry = {"id": t["id"]}
        for metric in ("grant_equivalent", "grant_equivalent_pct"):
            entry[metric] = weighted_distribution([
                (Fraction(s["weight"]),
                 _fraction(next(r for r in s["tools"] if r["id"] == t["id"])
                           ["grant_equivalent"][metric]))
                for s in scenario_outputs])
        distribution["tools"].append(entry)

    fingerprint = hashlib.sha256(
        f"{METHOD_VERSION}:{manifest_sha256}".encode("utf8")).hexdigest()
    return {
        "status": "published",
        "binding": {"method": METHOD_VERSION, "manifest_sha256": manifest_sha256,
                    "fingerprint": fingerprint},
        "valuation_date": v["valuation_date"],
        "report_currency": v["report_currency"],
        "curve_summary": curve_summary,
        "scenario_results": scenario_outputs,
        "distribution": distribution,
        "annual_weighted_mean": _annual_weighted_mean(scenario_outputs),
    }


def binding_fingerprint(method: str, manifest_sha256: str) -> str:
    return hashlib.sha256(f"{method}:{manifest_sha256}".encode("utf8")).hexdigest()


def verify_binding(report, manifest_sha256) -> bool:
    b = report["binding"]
    return (b["method"] == METHOD_VERSION and b["manifest_sha256"] == manifest_sha256
            and b["fingerprint"] == binding_fingerprint(b["method"], manifest_sha256))


def _num(x):
    """所有金额/利率/因子均为 Fraction，序列化为精确有理数字符串；None 透传。"""
    if x is None:
        return None
    return str(x)


def to_jsonable(obj):
    if isinstance(obj, Fraction):
        return str(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


def analyze_file(path):
    v, digest = load(path)
    return analyze(v, digest), digest
