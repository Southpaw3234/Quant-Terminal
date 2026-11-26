#!/usr/bin/env python3
"""Pure-logic validation for ACTUARY. No network, no clock.

The class of wrongness this file exists to prevent: **a number that reads as
advice.** It has three shapes here, and each looks reasonable on a page:

  1. a "distribution" computed from four cases
  2. a base rate shown without the verdict of the read that produced it
  3. a concentration figure that moves when the operator changes stance

and one more, which is the cheapest to ship by accident:

  4. a base rate computed from a ledger no pre-registered read has paid for
"""

from __future__ import annotations

import ast
import inspect
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from qt import actuary as act
import actuary as agent

PASS, FAIL = [], []
VERDICT_WORDS = ("buy", "sell", "hold", "rating", "target", "confidence",
                 "recommend", "score", "forecast", "signal", "conviction",
                 "upside", "downside_target")


def check(name: str, ok: bool, why: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  — {why}" if why else ""))


def _imports(mod) -> set:
    out = set()
    for n in ast.walk(ast.parse(inspect.getsource(mod))):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
            out.update(f"{n.module}.{a.name}" for a in n.names)
    return out


def _identifiers(mod) -> set:
    out = set()
    for n in ast.walk(ast.parse(inspect.getsource(mod))):
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            out.add(n.name.lower())
        elif isinstance(n, ast.Name):
            out.add(n.id.lower())
        elif isinstance(n, ast.arg):
            out.add(n.arg.lower())
        elif isinstance(n, ast.Attribute):
            out.add(n.attr.lower())
    return out


def _keys(obj) -> set:
    """Every dict key anywhere inside an emitted structure."""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k).lower())
            out |= _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _keys(v)
    return out


def _numbers(obj, path="") -> dict:
    """{path: number} for every numeric leaf — used to prove stance changes
    no number."""
    out = {}
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out[path] = obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_numbers(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_numbers(v, f"{path}[{i}]"))
    return out


# ── 1. a distribution or nothing ─────────────────────────────────────────────

def test_distribution():
    print("\na distribution with n, or insufficient history — nothing between")
    few = act.distribution([0.5, -0.1, 0.2, 0.9])
    check("four-cases-is-insufficient", few["sufficient"] is False
          and few["n"] == 4 and "insufficient history" in few["reason"])
    check("🔑-insufficient-carries-NO-statistics",
          not ({"median", "mean", "q25", "q75", "worst", "best",
                "share_positive"} & set(few)),
          "a median from four cases is a number that will be read as "
          "information; the caveat beside it will not be")
    check("one-below-the-bar-is-still-insufficient",
          act.distribution(list(range(act.MIN_N - 1)))["sufficient"] is False)
    check("exactly-the-bar-is-sufficient",
          act.distribution(list(range(act.MIN_N)))["sufficient"] is True)
    check("empty-is-insufficient-with-n-0",
          act.distribution([])["n"] == 0
          and act.distribution(None)["sufficient"] is False)

    vals = [i / 100 for i in range(-50, 51)]          # 101 values, median 0
    d = act.distribution(vals)
    check("n-is-reported", d["n"] == 101)
    check("median-and-quartiles", d["median"] == 0.0 and d["q25"] == -0.25
          and d["q75"] == 0.25, f"{d['q25']} / {d['median']} / {d['q75']}")
    check("worst-and-best-are-the-range", d["worst"] == -0.5 and d["best"] == 0.5)
    check("share-positive", abs(d["share_positive"] - 50 / 101) < 1e-3)
    check("zero-is-not-positive",
          act.distribution([0.0] * 40)["share_positive"] == 0.0)

    dirty = act.distribution([0.1] * 30 + ["", None, "x", float("nan"),
                                           float("inf")])
    check("blank-and-nan-are-dropped-not-counted-as-zero", dirty["n"] == 30
          and dirty["mean"] == 0.1,
          "a blank outcome folded in as 0.0 would pull every median toward "
          "zero and inflate n")
    check("quantile-interpolates", act.quantile([0.0, 1.0], 0.25) == 0.25)


# ── 2. base rates travel with their verdict ──────────────────────────────────

def _ledger(n=40, et="form4_cluster_buy", horizon="63"):
    return [{"event_id": f"e{i}", "ticker": "T", "event_type": et,
             "event_ts": f"2024-01-{(i % 28) + 1:02d}T00:00:00",
             "horizon": horizon, "event_ret": str(0.01 * (i - 20)),
             "control": "SPY", "control_ret": "0.01",
             "abnormal_ret": str(0.01 * (i - 21)), "exit_status": "ok"}
            for i in range(n)]


REG = {"specifications": {
    "a": {"spec_id": "a", "declared_at": "2026-01-01", "read_at": "2026-02-01",
          "result": {"verdict": "NOT MET", "n": 40}},
    "b": {"spec_id": "b", "result": {"read": {"verdict": "NOT MET"}}},
    "c": {"spec_id": "c", "result": {"read": {"t": 0.4}}},
}}


def test_base_rates():
    print("\na base rate travels with the verdict of the read behind it")
    check("verdict-at-top-level", act.find_verdict(REG["specifications"]["a"]["result"]) == "NOT MET")
    check("verdict-nested-under-read", act.find_verdict(REG["specifications"]["b"]["result"]) == "NOT MET")
    check("no-verdict-is-None-not-a-guess", act.find_verdict(REG["specifications"]["c"]["result"]) is None)
    check("unknown-spec-is-named",
          act.registry_context(REG, "zzz")["verdict"] is None
          and "not found" in act.registry_context(REG, "zzz")["reason"])

    ctx = act.registry_context(REG, "a")
    src = {"path": "x.csv", "rows": 40, "sha256": "f" * 64}
    rates = act.base_rates(_ledger(40), src, ctx)
    r = rates[0]
    check("one-rate-per-event-type-and-horizon", len(rates) == 1
          and r["event_type"] == "form4_cluster_buy" and r["horizon_bars"] == 63)
    check("abnormal-and-raw-are-both-reported",
          r["abnormal"]["n"] == 40 and r["raw"]["n"] == 40
          and r["abnormal"]["median"] != r["raw"]["median"])
    check("🔑-the-verdict-is-in-the-sentence-that-travels",
          "NOT MET" in r["how_to_read"] and "not a forecast" in r["how_to_read"],
          "a median beside NOT MET is a different sentence from the same "
          "median alone")
    check("source-is-fingerprinted", r["source"]["sha256"] == "f" * 64)
    check("exits-are-counted", r["exits"] == {"ok": 40})
    check("sample-window-is-stated",
          r["sample"]["from"] == "2024-01-01" and r["sample"]["to"] == "2024-01-28")

    two = act.base_rates(_ledger(40) + _ledger(35, horizon="21"), src, ctx)
    check("horizons-are-not-pooled", [x["horizon_bars"] for x in two] == [21, 63],
          "a 21-bar and a 63-bar outcome are different questions")
    small = act.base_rates(_ledger(5), src, ctx)
    check("a-thin-ledger-reports-insufficient",
          small[0]["abnormal"]["sufficient"] is False
          and small[0]["n_events"] == 5)

    check("known-type-returns-its-rates",
          act.lookup("form4_cluster_buy", rates) == rates)
    miss = act.lookup("8k_item_2.02", rates)
    check("🔑-unknown-type-returns-insufficient-history-never-empty",
          len(miss) == 1 and miss[0]["sufficient"] is False
          and miss[0]["n"] == 0 and "insufficient history" in miss[0]["reason"],
          "the brief's done-when: a distribution with n, or 'insufficient "
          "history' — no third answer")

    obs = [{"kind": "filing", "root_form": "8-K", "items": ["2.02", "9.01"]},
           {"kind": "filing", "root_form": "8-K", "items": []},
           {"kind": "filing", "root_form": "4", "direction": None},
           {"kind": "filing", "root_form": "4", "direction": "purchase"},
           {"kind": "filing", "root_form": "10-Q"},
           {"kind": "derived_event", "event_type": "form4_cluster_buy"}]
    et = act.observed_event_types(obs)
    check("event-types-are-mechanical",
          et == {"8k_item_(no item listed)": 1, "8k_item_2.02": 1,
                 "8k_item_9.01": 1, "filing_10-Q": 1,
                 "form4_cluster_buy": 1, "form4_direction_not_observed": 1,
                 "form4_purchase": 1}, f"{et}")
    check("an-undirected-form4-is-not-counted-as-a-purchase",
          et["form4_direction_not_observed"] == 1 and et["form4_purchase"] == 1)


def test_committed_sources():
    print("\nevery listed ledger has a read that paid for it")
    cfg = json.load(open(agent.SOURCES, encoding="utf-8"))
    check("sources-file-lists-ledgers", len(cfg["sources"]) >= 1)
    for s in cfg["sources"]:
        rows = agent.read_csv_rows(s["ledger"])
        check(f"ledger-exists-{s['spec_id']}", len(rows) > 0, s["ledger"])
        reg = json.load(open(s["registry"], encoding="utf-8"))
        ctx = act.registry_context(reg, s["spec_id"])
        # 🔑 The structural form of the note in sources.json.
        check(f"🔑-read-is-recorded-for-{s['spec_id']}",
              bool(ctx.get("verdict")),
              f"verdict {ctx.get('verdict')!r} — a ledger listed here "
              f"without a spent read would let a nightly job look at "
              f"outcomes the registry has not paid for")
    rates, report = agent.load_base_rates()
    check("real-ledgers-yield-distributions",
          rates and all(r["abnormal"]["sufficient"] for r in rates),
          ", ".join(f"n={r['abnormal']['n']}" for r in rates))
    check("no-listed-ledger-is-missing",
          all(x["status"] == "ok" for x in report))


# ── 3. exposure ──────────────────────────────────────────────────────────────

FACTS = {"AAA": {"sector": "Tech", "market_cap_musd": 150.0,
                 "adv_dollars": 100000.0, "beta": 1.5},
         "BBB": {"sector": "Tech", "market_cap_musd": 900.0,
                 "adv_dollars": 2000000.0, "beta": 0.5},
         "CCC": {"sector": None, "market_cap_musd": None,
                 "adv_dollars": None, "beta": None,
                 "reason": "finnhub returned no sector"}}
ROWS = [{"ticker": "aaa", "dollars": "6,000", "as_of": "2026-11-14"},
        {"ticker": "BBB", "dollars": "$3000"},
        {"ticker": "CCC", "dollars": "1000"}]


def test_exposure():
    print("\nexposure: stance flags lines, it never changes a number")
    none = act.exposure(act.parse_positions([]), {}, "balanced", ["GRPN"])
    check("🔑-no-positions-is-null-with-a-reason",
          none["status"] == "no_positions" and none["total_dollars"] is None
          and "positions.csv" in none["reason"],
          "'0% concentrated' would be a measurement of a portfolio nobody "
          "declared")
    check("theses-without-positions-are-listed",
          none["theses_without_a_position"] == ["GRPN"])

    bad = act.parse_positions([{"ticker": "X", "dollars": "abc"},
                               {"ticker": "", "dollars": "5"},
                               {"ticker": "Y", "dollars": "-5"},
                               {"ticker": "Z", "dollars": "0"},
                               {"ticker": "", "dollars": ""}])
    check("bad-rows-are-rejected-and-listed-not-valued-at-zero",
          bad["positions"] == [] and len(bad["rejected"]) == 4,
          "a position at $0 vanishes from every concentration figure")
    check("NA-is-a-ticker",
          act.parse_positions([{"ticker": "NA", "dollars": "10"}])
          ["positions"][0]["ticker"] == "NA")
    check("duplicate-rows-are-summed",
          act.parse_positions([{"ticker": "A", "dollars": "1"},
                               {"ticker": "a", "dollars": "2"}])
          ["positions"] == [{"ticker": "A", "dollars": 3.0}])

    parsed = act.parse_positions(ROWS)
    ex = act.exposure(parsed, FACTS, "balanced", ["AAA"])
    check("weights", [n["weight"] for n in ex["names"]] == [0.6, 0.3, 0.1]
          and ex["total_dollars"] == 10000.0)
    check("top3-and-effective-names", ex["top3"] == 1.0
          and ex["effective_names"] == round(1 / (0.36 + 0.09 + 0.01), 2))
    check("sector-weights", ex["by_sector"][0] == {
        "group": "Tech", "weight": 0.9, "dollars": 9000.0,
        "names": ["AAA", "BBB"]})
    check("🔑-unknown-sector-is-its-own-bucket",
          ex["by_sector"][1]["group"] == "not observed"
          and ex["by_sector"][1]["names"] == ["CCC"],
          "never dropped, never guessed")
    check("size-buckets", {g["group"] for g in ex["by_size"]}
          == {"micro (<$300M)", "small ($300M–$2B)", "not observed"})
    aaa = ex["names"][0]
    check("days-to-exit", aaa["days_to_exit"] == 0.6,
          "$6,000 sold at 10% of a $100k average day")
    check("🔑-unobserved-ADV-is-None-not-zero-days",
          ex["names"][2]["days_to_exit"] is None)
    check("beta-states-its-coverage",
          ex["beta"]["coverage"] == 0.9
          and ex["beta"]["weighted"] == round((0.6 * 1.5 + 0.3 * 0.5) / 0.9, 3),
          "a book beta over 90% of the book says so")
    check("liquidity-states-what-it-covers",
          ex["liquidity"]["observed_for"] == 2 and ex["liquidity"]["of"] == 3)
    check("positions-without-a-thesis-are-listed",
          ex["positions_without_a_thesis"] == ["BBB", "CCC"])

    # 🔑 The proof. Every number identical under every stance.
    per = {s: act.exposure(parsed, FACTS, s, ["AAA"]) for s in act.STANCES}

    def facts_only(e):
        e = dict(e)
        for k in ("stance", "limits", "flags"):
            e.pop(k)
        return e

    base = _numbers(facts_only(per["balanced"]))
    check("🔑-every-number-is-identical-under-every-stance",
          all(_numbers(facts_only(per[s])) == base for s in act.STANCES)
          and len(base) > 20, f"{len(base)} numeric fields compared")
    # An evenly split four-name book: 25% each. Over the conservative
    # single-name line (10%) and the balanced one (20%), under the
    # aggressive one (33%).
    even = act.parse_positions([{"ticker": t, "dollars": "25"}
                                for t in ("W", "X", "Y", "Z")])
    n_flags = {s: len(act.exposure(even, {}, s)["flags"]) for s in act.STANCES}
    check("stance-changes-what-is-FLAGGED",
          n_flags["conservative"] > n_flags["aggressive"], f"{n_flags}")
    check("and-the-same-book-has-the-same-numbers",
          len({json.dumps(_numbers(facts_only(act.exposure(even, {}, s))),
                          sort_keys=True) for s in act.STANCES}) == 1)
    check("unknown-stance-falls-back-to-balanced-and-says-so",
          act.exposure(parsed, FACTS, "yolo")["stance"] == "balanced")


# ── 4. falsifiers ────────────────────────────────────────────────────────────

def test_falsifiers():
    print("\nfalsifiers: the ledger's record, never this agent's judgment")
    theses = [
        {"thesis_id": "calm", "ticker": "AAA", "claim": "c",
         "falsifiers": ["f1"], "fired": [], "overridden": [],
         "review_date": "2027-01-01"},
        {"thesis_id": "drift", "ticker": "BBB", "claim": "c",
         "falsifiers": ["f1", "f2"], "fired": ["f1", "f2"],
         "overridden": ["f2"], "review_date": "2027-01-01"},
        {"thesis_id": "due", "ticker": "CCC", "claim": "c",
         "falsifiers": ["f1"], "fired": [], "overridden": [],
         "review_date": "2026-11-01"},
    ]
    bearing = [{"thesis_id": "calm", "observations": [{"id": "edgar:1"}],
                "days_searched": ["2026-11-13"]}]
    rep = act.falsifier_report(theses, bearing, "2026-11-14")
    check("drifting-first-then-due", [t["thesis_id"] for t in rep]
          == ["drift", "due", "calm"])
    d = rep[0]
    check("per-falsifier-status",
          [f["ledger_status"] for f in d["falsifiers"]]
          == ["fired_unaddressed", "fired_overridden"])
    check("🔑-met-is-null-with-the-reason",
          all(f["met"] is None and "operator" in f["met_reason"]
              for t in rep for f in t["falsifiers"]),
          "a free-text condition cannot be tested by this agent, and it "
          "does not pretend to")
    check("review-due-is-by-date", rep[1]["review_due"] and not rep[2]["review_due"])
    check("evidence-arrived-is-a-count-with-ids",
          rep[2]["evidence_arrived"]["observations"] == 1
          and rep[2]["evidence_arrived"]["ids"] == ["edgar:1"])
    check("no-evidence-is-zero-with-the-days-searched-empty",
          rep[0]["evidence_arrived"]["observations"] == 0)
    check("no-theses-no-rows", act.falsifier_report([], [], "2026-11-14") == [])


# ── 5. 🔑 no verdict ─────────────────────────────────────────────────────────

def test_no_verdict():
    print("\nACTUARY never emits a verdict")
    ids = _identifiers(act) | _identifiers(agent)
    for w in VERDICT_WORDS:
        hit = sorted(i for i in ids if w in i)
        check(f"no-identifier-contains-{w}", not hit,
              f"found {hit}" if hit else "")

    parsed = act.parse_positions(ROWS)
    emitted = {
        "rates": act.base_rates(_ledger(40), {"path": "x"},
                                act.registry_context(REG, "a")),
        "miss": act.lookup("x", []),
        "exposure": act.exposure(parsed, FACTS, "conservative", ["AAA"]),
        "falsifiers": act.falsifier_report(
            [{"thesis_id": "t", "ticker": "A", "falsifiers": ["f"],
              "fired": [], "overridden": [], "review_date": "2027-01-01"}],
            [], "2026-11-14"),
    }
    keys = _keys(emitted)
    for w in VERDICT_WORDS:
        hit = sorted(k for k in keys if w in k)
        check(f"no-emitted-key-contains-{w}", not hit,
              f"found {hit}" if hit else "")

    imported = _imports(agent) | _imports(act)
    for m in ("qt.referee", "qt.wrc", "qt.calendar_time", "qt.prices",
              "yfinance", "alpaca", "alpaca_trade_api"):
        check(f"does-not-import-{m}", m not in imported,
              "describing a spent ledger spends nothing; evaluating a "
              "criterion would")
    roots = {m.split(".")[0] for m in _imports(act)}
    check("pure-module-is-stdlib-only", roots <= {"__future__", "math"},
          f"{sorted(roots)}")
    check("positions-are-read-without-pandas",
          "csv.DictReader" in inspect.getsource(agent.read_csv_rows),
          "pandas turns the ticker NA into NaN, and a position that parses "
          "as missing is a position that vanishes")
    d = agent.digest({
        "day": "2026-11-13", "base_rates": emitted["rates"],
        "observed_today": [], "exposure": emitted["exposure"],
        "falsifiers": emitted["falsifiers"], "ledger": {"ok": True}})
    check("digest-says-it-is-not-a-recommendation",
          "no recommendation" in d.lower())



def test_tonight():
    print("\nACTUARY counts what resolved, once")
    newest = [{"kind": "filing", "root_form": "8-K", "items": ["2.02"]},
              {"kind": "filing", "root_form": "4", "direction": None},
              {"kind": "filing", "root_form": "4", "direction": "purchase",
               "direction_observed_at": "RUN"},
              {"kind": "derived_event", "event_type": "form4_cluster_buy",
               "observed_at": "RUN"}]
    resolved = [newest[2], newest[3],
                {"kind": "filing", "root_form": "4", "direction": "sale",
                 "day": "2026-11-23"},
                {"kind": "derived_event", "event_type": "form4_cluster_buy",
                 "day": "2026-11-23"}]
    et = act.observed_event_types(act.tonight(newest, resolved))
    check("🔑-resolved-clusters-from-older-days-are-counted",
          et.get("form4_cluster_buy") == 2,
          "it matched 4 cluster buys to their base rate when 29 happened")
    check("a-newest-day-record-in-resolved-is-not-double-counted",
          et.get("form4_purchase") == 1)
    check("older-day-directions-are-counted", et.get("form4_sale") == 1)
    check("the-newest-days-pending-form4s-still-count-as-unobserved",
          et.get("form4_direction_not_observed") == 1)
    check("non-form4-filings-are-unchanged", et.get("8k_item_2.02") == 1)
    check("nothing-resolved-is-the-old-behaviour-minus-directed",
          act.observed_event_types(act.tonight(newest[:2], [])) ==
          {"8k_item_2.02": 1, "form4_direction_not_observed": 1})


def main():
    print("=" * 70)
    print("ACTUARY — pure logic (no network, no clock)")
    print("=" * 70)
    test_distribution()
    test_base_rates()
    test_committed_sources()
    test_exposure()
    test_falsifiers()
    test_tonight()
    test_no_verdict()
    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED  {f}")
        sys.exit(1)
    print("All checks passed. No network touched, no verdict emitted.")


if __name__ == "__main__":
    main()
