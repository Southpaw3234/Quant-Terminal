"""Current fundamentals for one company from SEC XBRL, for DISPLAY. Pure logic.

Not the research extractor. extract_fundamentals.py serves frozen,
point-in-time research panels and must stay byte-reproducible, so it takes
the first tag in a fallback chain that has any data. For a lookup page that
is wrong: NVIDIA's revenue tag changed in 2022, so "first tag with data"
returns its 2022 revenue; JPMorgan's cash comes back from 2018.

So here, for every figure:
  * every tag in the chain is read, and the one with the MOST RECENT period
    wins (ties go to the earlier tag in the chain);
  * flows are trailing twelve months where they can be formed
    (extract_fundamentals.ttm_as_of, unchanged), else the latest full fiscal
    year, and the basis is always stated;
  * a figure whose period ended more than STALE_DAYS before today is
    dropped and reported as not available, never shown as current.
"""

from __future__ import annotations

import pandas as pd

import extract_fundamentals as ef

STALE_DAYS = 460          # ~15 months: one missed annual report and it goes

# name -> (namespace, unit, kind, [tags in preference order])
METRICS = {
    "revenue": ("us-gaap", "USD", "flow", [
        "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
        "RevenuesNetOfInterestExpense", "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax"]),
    "gross_profit": ("us-gaap", "USD", "flow", ["GrossProfit"]),
    "op_income": ("us-gaap", "USD", "flow", ["OperatingIncomeLoss"]),
    "net_income": ("us-gaap", "USD", "flow", [
        "NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"]),
    "eps": ("us-gaap", "USD/shares", "flow", [
        "EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"]),
    "op_cash_flow": ("us-gaap", "USD", "flow", [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]),
    "capex": ("us-gaap", "USD", "flow", [
        "PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"]),
    "dividends_per_share": ("us-gaap", "USD/shares", "flow", [
        "CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"]),
    "assets": ("us-gaap", "USD", "stock", ["Assets"]),
    "liabilities": ("us-gaap", "USD", "stock", ["Liabilities"]),
    "equity": ("us-gaap", "USD", "stock", [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
    "cash": ("us-gaap", "USD", "stock", [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "Cash"]),
    "debt_long": ("us-gaap", "USD", "stock", [
        "LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebtAndFinanceLeaseObligations"]),
    "debt_current": ("us-gaap", "USD", "stock", [
        "LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings"]),
    "shares": ("dei", "shares", "stock", ["EntityCommonStockSharesOutstanding"]),
    "shares_gaap": ("us-gaap", "shares", "stock", [
        "CommonStockSharesOutstanding", "WeightedAverageNumberOfDilutedSharesOutstanding"]),
}


def _facts(cf: dict, ns: str, tag: str, unit: str) -> list:
    node = ((cf.get("facts") or {}).get(ns) or {}).get(tag) or {}
    out = []
    for r in (node.get("units") or {}).get(unit, []) or []:
        if r.get("end") and r.get("filed") and r.get("val") is not None:
            out.append({"end": str(r["end"]), "start": str(r.get("start") or ""),
                        "val": float(r["val"]), "filed": str(r["filed"]),
                        "form": str(r.get("form", "")), "fp": str(r.get("fp", "")),
                        "tag": tag})
    return out


def _latest_fy(facts: list, asof: str) -> "dict | None":
    fy = [f for f in facts if f["filed"] <= asof
          and ef._duration_days(f) is not None
          and ef._ANNUAL_DAYS[0] <= ef._duration_days(f) <= ef._ANNUAL_DAYS[1]]
    if not fy:
        return None
    end = max(f["end"] for f in fy)
    return max((f for f in fy if f["end"] == end), key=lambda f: f["filed"])


def value(cf: dict, name: str, asof) -> "dict | None":
    """{val, end, filed, basis, tag} for one metric as known on `asof`."""
    ns, unit, kind, tags = METRICS[name]
    d = str(asof)[:10]
    best = None
    for i, tag in enumerate(tags):
        facts = _facts(cf, ns, tag, unit)
        if not facts:
            continue
        if kind == "flow":
            got = ef.ttm_as_of(facts, d)
            basis = "trailing 12 months"
            fy = _latest_fy(facts, d)
            # A full year that ends later than the TTM's window is fresher.
            if fy and (not got or fy["end"] > got["end"]):
                got, basis = fy, "last fiscal year"
        else:
            got = ef.known_as_of(facts, d)
            basis = "as of"
        if not got:
            continue
        cand = {"val": got["val"], "end": got["end"], "filed": got.get("filed"),
                "basis": basis, "tag": tag, "rank": i}
        if best is None or cand["end"] > best["end"]:
            best = cand
    if not best:
        return None
    if (pd.Timestamp(d) - pd.Timestamp(best["end"])).days > STALE_DAYS:
        return None
    best.pop("rank")
    return best


def extract(cf: dict, asof) -> dict:
    """Every metric now, and the year-ago revenue, net income and EPS."""
    d = str(asof)[:10]
    out = {m: value(cf, m, d) for m in METRICS}
    if out.get("shares") is None:
        out["shares"] = out.get("shares_gaap")
    out.pop("shares_gaap", None)
    for m in ("revenue", "net_income", "eps"):
        out[m + "_year_ago"] = year_ago(cf, m, out.get(m))
    return out


def year_ago(cf: dict, name: str, now: "dict | None") -> "dict | None":
    """The twelve-month figure for the period ending about a year before
    `now`'s, as it stood ~100 days after that period ended (when its report
    had been filed). Both are twelve-month windows, TTM or fiscal year, so
    they compare like with like; anything not 300-430 days apart is None."""
    if not now:
        return None
    asof = (pd.Timestamp(now["end"]) - pd.Timedelta(days=365)
            + pd.Timedelta(days=100)).date().isoformat()
    ago = value(cf, name, asof)
    if not ago:
        return None
    gap = (pd.Timestamp(now["end"]) - pd.Timestamp(ago["end"])).days
    return ago if 300 <= gap <= 430 else None
