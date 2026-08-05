#!/usr/bin/env python3
"""Validation suite for the gross-cap hard gate (fix/gross-cap-hard-gate, 2026-07-08).

Run from the repo root (CI: see .github/workflows/validate_gross_cap.yml).

1. quant_runner.py compiles + all patch strings AST-parse (preflight steps 1-3)
2. every gross-cap _SRC_REPLACE anchor appears EXACTLY ONCE in notebook Cell 13
   and the fully patched Cell 13 source compiles
3. behavioral replay of _gross_cap_allows:
   (a) 2026-07-08 reality — equity $117,503 / gross $277,383 (2.36x), morning:
       every BUY refused + all 30 signals pre-blocked (would have stopped the
       $131k batch that re-levered the account that morning)
   (b) de-levered account (0.51x): orders allowed until the 1.0x cap, then cut
   (c) run_type=scoring: all BUYs refused regardless of room (7/7 11 PM bug)
   (d) account read fails while keys set: fail-closed, all BUYs refused
   (e) no broker keys (local paper mode): ledger fallback budgets correctly
"""
import ast, contextlib, io, json, os, py_compile, re, sys, tempfile, types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(REPO, "quant_runner.py")
NB = os.path.join(REPO, "trading_model_v25.1.ipynb")
fails = []

# ── 1. compile + patch strings parse ─────────────────────────────────────────
py_compile.compile(RUNNER, doraise=True)
print("1a. quant_runner.py py_compile               PASS")
src = open(RUNNER, encoding="utf-8-sig").read()
tree = ast.parse(src)
print("1b. quant_runner.py ast.parse                PASS")
n_patch = 0
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and re.match(r"(CELL_\d+_(PRE|POST)PATCH|_CELL_\d+_\w+)$", t.id):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    n_patch += 1
                    try:
                        ast.parse(node.value.value)
                    except SyntaxError as e:
                        fails.append(f"patch string {t.id} SYNTAX FAIL line {e.lineno}: {e.msg}")
print(f"1c. {n_patch} patch strings ast.parse              {'PASS' if not fails else 'FAIL'}")

# ── 2. anchors unique in Cell 13, patched source compiles ────────────────────
nb = json.load(open(NB, encoding="utf-8"))
cell13 = "".join(nb["cells"][13]["source"])
pairs = None
for node in tree.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "_SRC_REPLACE":
                pairs = ast.literal_eval(node.value)
assert pairs, "_SRC_REPLACE not found"
new_anchors = pairs[-3:]
for i, (old, new) in enumerate(new_anchors, 1):
    n = cell13.count(old)
    tag = old.splitlines()[0][:52]
    if n != 1:
        fails.append(f"anchor {i} ({tag!r}) found {n}x in Cell 13, want exactly 1")
        print(f"2.{i} anchor {tag!r}  FAIL ({n}x)")
    else:
        print(f"2.{i} anchor unique in Cell 13                 PASS  ({tag!r})")
patched = cell13
for old, new in pairs:
    patched = patched.replace(old, new)
try:
    compile(patched, "cell13_patched", "exec")
    print("2.4 patched Cell 13 compiles                 PASS")
except SyntaxError as e:
    fails.append(f"patched cell13 syntax error line {e.lineno}: {e.msg}")
for needle in ('_gross_cap_allows(ticker, qty * price)', 'sig.get("exec_blocked")',
               'result.get("reason") in ("gross_cap", "sector_cap", "stale_bar", "oversell")',
               '_oversell_cap(ticker, qty)',
               '_sector_cap_allows(ticker, qty * price)',
               '_sector_cap_release(ticker, qty * price)'):
    if needle not in patched:
        fails.append(f"patched cell13 missing {needle!r}")
print("2.5 all 6 gate hooks present in patched src  "
      + ("PASS" if not any("missing" in f for f in fails) else "FAIL"))
# Ordering contract (2026-07-31): the sector gate must run BEFORE the gross cap.
# _gross_cap_allows commits its budget on success, so a sector refusal after it
# would charge gross for an order never submitted and starve later BUYs.
_i_sec = patched.find("_sector_cap_allows(ticker, qty * price)")
_i_grs = patched.find("_gross_cap_allows(ticker, qty * price)")
_ord_ok = 0 <= _i_sec < _i_grs
if not _ord_ok:
    fails.append("sector gate must precede gross cap in execute_trade")
print("2.6 sector gate precedes gross cap          "
      + ("PASS" if _ord_ok else "FAIL"))

# ── 3. behavioral replay of the prepatch gate ────────────────────────────────
prepatch = None
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "CELL_13_PREPATCH":
                if prepatch is None and isinstance(node.value, ast.Constant):
                    prepatch = node.value.value
gross_src = prepatch[prepatch.index("# ── Fix (2026-07-08): HARD gross-exposure cap"):]

def _stub_alpaca(equity, gross, boom=False, positions=None):
    class _Acct: pass
    class _Pos: pass
    class _TC:
        def __init__(self, *a, **k):
            if boom:
                raise RuntimeError("api down")
        def get_account(self):
            a = _Acct(); a.equity = equity; return a
        def get_all_positions(self):
            if positions is not None:
                out = []
                for _sym, _mv in positions:
                    p = _Pos(); p.symbol = _sym; p.market_value = _mv; p.qty = 1.0
                    out.append(p)
                return out
            p = _Pos(); p.symbol = "SPY"; p.market_value = gross; p.qty = 1.0
            return [p]
    mod_a = types.ModuleType("alpaca")
    mod_t = types.ModuleType("alpaca.trading")
    mod_c = types.ModuleType("alpaca.trading.client")
    mod_c.TradingClient = _TC
    mod_a.trading = mod_t; mod_t.client = mod_c
    sys.modules["alpaca"] = mod_a
    sys.modules["alpaca.trading"] = mod_t
    sys.modules["alpaca.trading.client"] = mod_c

def run_scenario(name, env_run, keys, equity, gross, n_sigs, expect_allow, expect_block,
                 order_notional=5000.0, boom=False):
    os.environ["RUN_TYPE"] = env_run
    os.environ.pop("QT_MAX_GROSS", None)
    ns = {
        "ALPACA_API_KEY": "k" if keys else "", "ALPACA_SECRET_KEY": "s" if keys else "",
        "PORTFOLIO_CAPITAL": 100_000.0, "MAX_POSITION_PCT": 0.05,
        "signals": {f"TK{i}": {"action": "BUY", "confidence": 0.60 + i * 0.001}
                    for i in range(n_sigs)},
        "__builtins__": __builtins__,
    }
    if keys:
        _stub_alpaca(equity, gross, boom=boom)
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)  # no real ledger in scope for the no-keys fallback
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                exec(gross_src, ns)
                allow = ns["_gross_cap_allows"]
                allowed = sum(1 for i in range(n_sigs) if allow(f"TK{i}", order_notional))
        finally:
            os.chdir(cwd)
    blocked = n_sigs - allowed
    ok = (allowed == expect_allow and blocked == expect_block)
    print(f"3.  {name:<46} allowed={allowed:<3} blocked={blocked:<3} "
          f"{'PASS' if ok else 'FAIL (want %d/%d)' % (expect_allow, expect_block)}")
    if not ok:
        fails.append(f"scenario {name}: allowed={allowed} blocked={blocked}")
    return ns

# (a) today's reality: 2.36x levered, morning run -> everything refused
ns_a = run_scenario("7/8 levered 2.36x, morning run", "morning", True,
                    117_503.43, 277_383.0, 30, expect_allow=0, expect_block=30)
pb = sum(1 for s in ns_a["signals"].values() if s.get("exec_blocked"))
print(f"3.  7/8 pre-trim marks exec_blocked on {pb}/30          {'PASS' if pb == 30 else 'FAIL'}")
if pb != 30:
    fails.append(f"pre-trim marked {pb}/30, want 30")

# (b) de-levered 0.51x: room $58k -> 11 x $5k allowed, rest refused at the cap
run_scenario("de-levered 0.51x, morning run", "morning", True,
             118_000.0, 60_000.0, 30, expect_allow=11, expect_block=19)

# (c) scoring run (7/7 11 PM incident): refused regardless of room
run_scenario("scoring run, flat account", "scoring", True,
             118_000.0, 0.0, 30, expect_allow=0, expect_block=30)

# (d) fail-closed: account read raises while keys are set
run_scenario("account read fails (fail-closed)", "morning", True,
             None, None, 5, expect_allow=0, expect_block=5, boom=True)

# (e) no keys, local paper mode, empty ledger: room $100k = 20 x $5k slots
run_scenario("no keys, paper mode, empty ledger", "morning", False,
             None, None, 30, expect_allow=20, expect_block=10)

# ── 4. behavioral replay of the oversell guard (2026-07-15 short-book fix) ───
def oversell_ns(keys=True, pos_ok=True, live=None):
    ns = {"ALPACA_API_KEY": "k" if keys else "", "ALPACA_SECRET_KEY": "s" if keys else "",
          "PORTFOLIO_CAPITAL": 100_000.0, "MAX_POSITION_PCT": 0.05,
          "signals": {}, "__builtins__": __builtins__}
    if keys:
        _stub_alpaca(100_000.0, 0.0)
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                exec(gross_src, ns)
        finally:
            os.chdir(cwd)
    ns["_OVERSELL"]["pos_ok"] = pos_ok
    ns["_LIVE_QTY"].update(live or {})
    return ns

def check4(name, got, want):
    ok = got == want
    print(f"4.  {name:<52} got={got:<4} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"oversell {name}: got={got} want={want}")

with contextlib.redirect_stdout(io.StringIO()) as _buf4:
    ns4 = oversell_ns(live={"LONG10": 10.0, "SHORTED": -160.0})
    r_within  = ns4["_oversell_cap"]("LONG10", 5)     # within holdings
    ns4b = oversell_ns(live={"LONG10": 10.0})
    r_capped  = ns4b["_oversell_cap"]("LONG10", 25)   # capped at live qty
    r_drained = ns4b["_oversell_cap"]("LONG10", 5)    # already sold out this run
    r_flat    = ns4["_oversell_cap"]("NOPOS", 5)      # flat -> refuse
    r_short   = ns4["_oversell_cap"]("SHORTED", 5)    # short -> refuse (abs() trap)
    ns4c = oversell_ns(pos_ok=False)
    r_failcl  = ns4c["_oversell_cap"]("LONG10", 5)    # map failed -> fail-closed
    ns4d = oversell_ns(keys=False)
    r_local   = ns4d["_oversell_cap"]("ANYTHING", 7)  # local paper mode passthrough
check4("SELL within live long qty allowed in full", r_within, 5)
check4("SELL beyond live long qty capped", r_capped, 10)
check4("second SELL same run sees drained position", r_drained, 0)
check4("SELL while flat refused (no naked short)", r_flat, 0)
check4("SELL while SHORT refused (no short doubling)", r_short, 0)
check4("position map failed -> fail-closed", r_failcl, 0)
check4("no keys (local paper) -> passthrough", r_local, 7)

# ── 5. behavioral replay of the kill-switch era gate (2026-07-16 fix) ────────
# The consecutive-loss window must count only fresh-era (>= QT_STAGE1_START)
# equity BUY/SELL predictions: on 7/16 the matured 7/10 stale-era batch (5
# straight losses, wrong price_at_pred baselines) halted the new strategy's
# first open morning. The switch must still trip on a real fresh-era streak.
import pandas as pd
import textwrap

ks_pair = next((new for old, new in pairs if old.lstrip().startswith("scored = plog[")), None)
assert ks_pair, "kill-switch rewrite pair not found in _SRC_REPLACE"
ks_src = textwrap.dedent(ks_pair)

def ks_scored(rows):
    df = pd.DataFrame(rows, columns=["pred_ts", "ticker", "action", "scored", "was_correct"])
    ns = {"plog": df, "KILL_CONSECUTIVE_LOSSES": 5, "__builtins__": __builtins__}
    os.environ.pop("QT_STAGE1_START", None)
    exec(ks_src, ns)
    return ns["scored"]

def ks_tripped(rows):
    scored = ks_scored(rows)
    return (len(scored) == 5
            and not scored["was_correct"].astype(str).isin(["True", "true"]).any())

def check5(name, got, want):
    ok = got == want
    print(f"5.  {name:<52} got={str(got):<5} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"kill-switch era gate {name}: got={got} want={want}")

_stale = [(f"2026-07-10 15:5{i}:00+0000", f"OLD{i}", "BUY", "True", "False") for i in range(5)]
_fresh_loss = [(f"2026-07-2{i} 15:00:00+0000", f"NEW{i}", "BUY", "True", "False") for i in range(5)]
_fresh_mixed = _fresh_loss[:3] + [("2026-07-23 15:00:00+0000", "WIN1", "BUY", "True", "True"),
                                  ("2026-07-24 15:00:00+0000", "NEW4", "SELL", "True", "False")]
_crypto = [(f"2026-07-2{i} 16:00:00+0000", "ETH-USD", "BUY", "True", "False") for i in range(5)]
_garbage = [("", "GARB1", "BUY", "True", "False"), ("nan", "GARB2", "BUY", "True", "False")]

check5("7/16 reality: 5 stale-era losses -> no trip", ks_tripped(_stale), False)
check5("stale-era rows fully excluded from window", len(ks_scored(_stale + _garbage)), 0)
check5("5 fresh-era losses STILL trip the switch", ks_tripped(_stale + _fresh_loss), True)
check5("fresh-era window with a win -> no trip", ks_tripped(_stale + _fresh_mixed), False)
check5("fresh-era crypto still excluded", len(ks_scored(_stale + _crypto)), 0)
check5("window = fresh equity rows only (mixed log)",
       len(ks_scored(_garbage + _stale + _crypto + _fresh_loss[:3])), 3)

# ── 6. stale-era gates on the remaining predictions.csv consumers (2026-07-16) ─
# Follow-up to section 5: the rule engine (LEARNED_RULES dampeners -> live
# composite scores), the staleness detector (RETRAIN_NEEDED.flag), and the
# _WL_RATIO Kelly win/loss cache all consumed stale-era scored outcomes.
# Each gate must drop pre-QT_STAGE1_START and garbage-pred_ts rows while
# keeping fresh-era rows intact.

def find_pair(marker):
    p = next(((old, new) for old, new in pairs if marker in old), None)
    assert p, f"era-gate pair with marker {marker!r} not found in _SRC_REPLACE"
    return p

pair_rules = find_pair('plog = pd.read_csv(PRED_LOG_FILE)')
pair_stale = find_pair('STALENESS_WINDOW')
pair_wl    = find_pair('_wl_log')

def check6(name, got, want):
    ok = got == want
    print(f"6.  {name:<52} got={str(got):<5} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"stale-era gate {name}: got={got} want={want}")

# 6a. anchors unique across the ENTIRE notebook (pairs apply to every cell —
#     the one-line variants of these anchors exist in reporting Cells 16/21).
nb_all = "".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code")
for nm, (old, _n) in [("rule-engine", pair_rules), ("staleness", pair_stale), ("wl-ratio", pair_wl)]:
    check6(f"anchor unique across all cells ({nm})", nb_all.count(old), 1)

# 6b. patched Cell 15 compiles (section 2 only compiles Cell 13).
cell15 = "".join(nb["cells"][15]["source"])
patched15 = cell15
for old, new in pairs:
    patched15 = patched15.replace(old, new)
try:
    compile(patched15, "cell15_patched", "exec")
    print("6.  patched Cell 15 compiles                         PASS")
except SyntaxError as e:
    fails.append(f"patched cell15 syntax error line {e.lineno}: {e.msg}")
    print(f"6.  patched Cell 15 compiles                         FAIL (line {e.lineno})")

# 6c. behavioral: synthetic log = 6 stale losses + garbage + 3 fresh rows.
_mix = ([{"pred_ts": f"2026-07-10 15:5{i}:00+0000", "ticker": f"OLD{i}", "action": "BUY",
          "scored": "True", "was_correct": "False", "actual_return": -0.05} for i in range(6)]
        + [{"pred_ts": "", "ticker": "GARB1", "action": "BUY", "scored": "True",
            "was_correct": "False", "actual_return": -0.9},
           {"pred_ts": "nan", "ticker": "GARB2", "action": "SELL", "scored": "True",
            "was_correct": "False", "actual_return": 0.9}]
        + [{"pred_ts": f"2026-07-2{i} 15:00:00+0000", "ticker": f"NEW{i}", "action": "BUY",
            "scored": "True", "was_correct": "True", "actual_return": 0.02} for i in range(3)])
_mix_df = pd.DataFrame(_mix)

# rule-engine gate: exec the replacement with PRED_LOG_FILE -> temp csv
os.environ.pop("QT_STAGE1_START", None)
with tempfile.TemporaryDirectory() as _tmp6:
    _csv6 = os.path.join(_tmp6, "predictions.csv")
    _mix_df.to_csv(_csv6, index=False)
    ns6 = {"pd": pd, "PRED_LOG_FILE": _csv6, "__builtins__": __builtins__}
    exec(textwrap.dedent(pair_rules[1]), ns6)
    check6("rule-engine gate keeps only fresh-era rows", len(ns6["scored"]), 3)
    check6("rule-engine gate: no stale/garbage tickers survive",
           sorted(ns6["scored"]["ticker"]), ["NEW0", "NEW1", "NEW2"])

# staleness gate: replacement ends mid-`if` — close the block to exec it
ns6b = {"plog": _mix_df.copy(), "STALENESS_WINDOW": 20, "__builtins__": __builtins__}
exec(textwrap.dedent(pair_stale[1]) + "\n    pass", ns6b)
check6("staleness gate keeps only fresh-era rows", len(ns6b["scored"]), 3)

# WL gate: fresh rows must keep their actual_return; stale returns must not
# reach the win/loss means
ns6c = {"_wl_log": _mix_df.copy(), "__builtins__": __builtins__}
exec(textwrap.dedent(pair_wl[1]), ns6c)
check6("wl-ratio gate keeps only fresh-era rows", len(ns6c["_wl_log"]), 3)
check6("wl-ratio gate: stale returns out of the mean",
       round(float(pd.to_numeric(ns6c["_wl_log"]["actual_return"]).mean()), 2), 0.02)

# ── 7. kill-switch pnl_history fallback resurrection (2026-07-24) ────────────
# Dead code 6/30→7/24: the fallback required a portfolio_value column the file
# never had, so every Alpaca outage ran with NO drawdown check (the blind spot
# during 7/24's midnight timeout). The writer now records portfolio_value and
# the logic lives in the pure _ks_pnl_fallback -- extract the SHIPPED source
# and replay it against synthetic curves. Contract: refuse rather than guess.
_ks_start = src.index("def _ks_pnl_fallback(")
_ks_end   = src.index("# ── end _ks_pnl_fallback")
ns7 = {"__builtins__": __builtins__}
exec(src[_ks_start:_ks_end], ns7)
_fb = ns7["_ks_pnl_fallback"]

NOW = pd.Timestamp("2026-07-24 21:00:00")
LIMS = (-0.10, -0.20, -0.15)   # daily / weekly / peak, mirrors the live constants

def curve(dailies, pv0=100000.0, pv_override=None, dates=None, cols=True):
    """Build a synthetic pnl_history frame: daily P&L rows + equity levels."""
    n = len(dailies)
    if dates is None:
        dates = [(NOW - pd.Timedelta(days=n - 1 - i)).strftime("%Y-%m-%d") for i in range(n)]
    pvs, running = [], pv0
    for d in dailies:
        running += (d if d is not None else 0)
        pvs.append(running)
    if pv_override is not None:
        pvs = pv_override
    data = {"date": dates,
            "unrealized_pnl": [""] * n, "realized_pnl": [""] * n,
            "total_pnl": ["" if d is None else d for d in dailies],
            "open_positions": [""] * n}
    if cols:
        data["portfolio_value"] = pvs
    return pd.DataFrame(data)

def check7(name, got, want):
    ok = got == want
    print(f"7.  {name:<52} got={str(got):<7} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"ks fallback {name}: got={got} want={want}")

# (a) pre-fix file (no portfolio_value column) -> refuses, stays blind
r = _fb(curve([100, -50, 200], cols=False), *LIMS, _now=NOW)
check7("old schema (no portfolio_value) refuses", r["evaluated"], False)

# (b) healthy curve -> evaluates, no trip (and denominator is REAL equity:
#     -2.6k on a $100k account reads -2.6%, NOT the 6/30 hardcoded-$10k -26%)
r = _fb(curve([500, -300, 800, -2600]), *LIMS, _now=NOW)
check7("healthy curve evaluates without tripping", (r["evaluated"], r["reason"] is None), (True, True))
check7("6/30 regression: real denominator (-2.6k/-100k)", round(r["daily_dd"], 3), -0.026)

# (c) single-day -11% -> daily trip
r = _fb(curve([200, 100, -11000]), *LIMS, _now=NOW)
check7("daily -11% trips the daily limit", "Daily drawdown" in str(r["reason"]), True)

# (d) five days summing -21% (each above the daily limit alone) -> weekly trip
r = _fb(curve([-4500, -4000, -4200, -4300, -4000]), *LIMS, _now=NOW)
check7("weekly -21% trips the weekly limit", "Weekly drawdown" in str(r["reason"]), True)

# (e) mild dailies but equity 16% off its high-water mark -> peak trip
r = _fb(curve([-500, -400, -300], pv_override=[118000, 100000, 99000]), *LIMS, _now=NOW)
check7("peak -16% off HWM trips the peak limit", "Peak drawdown" in str(r["reason"]), True)

# (f) legacy rows only (blank equity everywhere) -> refuses
r = _fb(curve([100, 200], pv_override=["", ""]), *LIMS, _now=NOW)
check7("all-blank portfolio_value refuses", r["evaluated"], False)

# (g) last row legacy-blank (total_pnl AND pv) -> falls back to last real day
r = _fb(curve([300, -700, None], pv_override=[100300, 99600, ""]), *LIMS, _now=NOW)
check7("blank last row -> uses last real daily", round(r["daily_dd"], 4), round(-700 / 99600, 4))

# (h) curve older than 7 days -> refuses (stale data says nothing about today)
old = [(NOW - pd.Timedelta(days=40 - i)).strftime("%Y-%m-%d") for i in range(3)]
r = _fb(curve([100, 200, 300], dates=old), *LIMS, _now=NOW)
check7("week-stale curve refuses", r["evaluated"], False)

# (i) zero/negative equity -> refuses (never a garbage denominator)
r = _fb(curve([100, 200], pv_override=[0, -5]), *LIMS, _now=NOW)
check7("non-positive equity refuses", r["evaluated"], False)

# ── 8. Gain-to-Pain repoint (2026-07-24) ─────────────────────────────────────
# GPR read daily_pnl_log.csv (permanently header-only) -> never computed once,
# and it can WRITE THE KILL FLAG below 0.20, so the repoint to pnl_history's
# daily total_pnl (monthly-equity-change basis) gets a behavioral replay of
# the SHIPPED patch string in an isolated cwd. Discord env is stripped.
gpr_src = next((n.value.value for n in ast.walk(tree)
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_CELL_15_GPR" for t in n.targets)
                and isinstance(n.value, ast.Constant)), None)
assert gpr_src, "_CELL_15_GPR patch string not found"
assert '_P15gpr("data/predictions/pnl_history.csv")' in gpr_src \
    and '_P15gpr("data/predictions/daily_pnl_log.csv")' not in gpr_src, \
    "GPR still wired to the dead daily_pnl_log.csv"

def run_gpr(csv_text):
    """Exec the shipped GPR block against a synthetic pnl_history in a tmp cwd."""
    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        try:
            os.chdir(td)
            os.makedirs("data/predictions", exist_ok=True)
            if csv_text is not None:
                with open("data/predictions/pnl_history.csv", "w") as fh:
                    fh.write(csv_text)
            os.environ.pop("DISCORD_WEBHOOK_URL", None)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exec(gpr_src, {"__builtins__": __builtins__})
            js = None
            if os.path.exists("data/predictions/gain_to_pain.json"):
                js = json.load(open("data/predictions/gain_to_pain.json"))
            flag = os.path.exists("data/KILL_SWITCH_ACTIVE.flag")
            return buf.getvalue(), js, flag
        finally:
            os.chdir(old_cwd)

def check8(name, got, want):
    ok = got == want
    print(f"8.  {name:<52} got={str(got):<7} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"gpr repoint {name}: got={got} want={want}")

def rows(month, day_vals):
    return "".join(f"2026-{month:02d}-{d:02d},,,{'' if v is None else v},,\n"
                   for d, v in day_vals)

HDR = "date,unrealized_pnl,realized_pnl,total_pnl,open_positions,portfolio_value\n"

# (a) healthy 3-month curve (+2000 / -1000 / +500), 60 valid rows + 3 blanks
healthy = HDR \
    + rows(5, [(d, 100) for d in range(1, 21)]) \
    + rows(6, [(d, -50) for d in range(1, 21)]) + rows(6, [(28, None)]) \
    + rows(7, [(d, 25) for d in range(1, 21)]) + rows(7, [(22, None), (23, None)])
out, js, flag = run_gpr(healthy)
check8("healthy curve computes (json lands)", js is not None, True)
check8("monthly-equity-change math (2500/1000)", js and js["gpr"], 2.5)
check8("healthy status OK, no kill flag", (js and js["status"], flag), ("OK", False))
check8("blank rows skipped, not zeroed (n_months=3)", js and js["n_months"], 3)

# (b) persistent bleed (+300 / -3000 / -1500 -> GPR 0.067) -> KILL + flag
bleed = HDR \
    + rows(5, [(d, 10) for d in range(1, 31)]) \
    + rows(6, [(d, -200) for d in range(1, 16)]) \
    + rows(7, [(d, -100) for d in range(1, 16)])
out, js, flag = run_gpr(bleed)
check8("bleed GPR 0.067 -> status KILL", js and js["status"], "KILL")
check8("bleed writes the kill flag", flag, True)

# (c) missing total_pnl column -> loud refusal, nothing computed
out, js, flag = run_gpr("date,net_pl\n" + "".join(f"2026-07-{d:02d},5\n" for d in range(1, 32)))
check8("missing column refuses loudly", ("no total_pnl column" in out, js, flag), (True, None, False))

# (d) under 30 rows -> skipped
out, js, flag = run_gpr(HDR + rows(7, [(d, 50) for d in range(1, 11)]))
check8("<30 rows skipped (no json)", ("need 30+" in out, js), (True, None))

# (e) no file at all -> benign print
out, js, flag = run_gpr(None)
check8("no file -> benign skip", "no pnl_history.csv yet" in out, True)

# ── 9. sector concentration cap (2026-07-31) ─────────────────────────────────
# Pinned to the 7/30 incident: an energy-concentrated book produced the first
# GENUINE kill-switch trip (7/23 batch 1W/10L, 7/24 batch 2W/12L, eleven of
# twelve losers energy). Slice from the SECTOR_MAP override so the scenarios
# exercise the REAL ticker->sector map, not a stub.
sector_src = prepatch[prepatch.index("SECTOR_MAP = {"):]

def sector_ns(equity, positions, ratio=None, keys=True, boom=False, run="morning"):
    os.environ["RUN_TYPE"] = run
    os.environ.pop("QT_MAX_GROSS", None)
    if ratio is None:
        os.environ.pop("QT_MAX_SECTOR", None)
    else:
        os.environ["QT_MAX_SECTOR"] = str(ratio)
    gross = sum(abs(mv) for _, mv in positions)
    ns = {
        "ALPACA_API_KEY": "k" if keys else "", "ALPACA_SECRET_KEY": "s" if keys else "",
        "PORTFOLIO_CAPITAL": equity, "MAX_POSITION_PCT": 0.05,
        "signals": {}, "__builtins__": __builtins__,
    }
    if keys:
        _stub_alpaca(equity, gross, boom=boom, positions=positions)
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                exec(sector_src, ns)
        finally:
            os.chdir(cwd)
    return ns, buf.getvalue()

def check9(name, got, want):
    ok = got == want
    print(f"9.  {name:<52} got={str(got):<7} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"section9 {name}: got={got} want={want}")

def call9(ns, tk, notional):
    """Call the gate and capture what it printed — refusals must be loud."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = ns["_sector_cap_allows"](tk, notional)
    return got, buf.getvalue()

# The REAL book, measured 2026-07-31 22:06Z via position_trim dry-run
# 30668967145 (read-only, nothing submitted): 35 positions, $74,644 gross on
# $113,690.49 equity = 0.66x. Energy is $35,635 = 31.3% of equity and 47.7% of
# the book, across five names; the top six positions are five energy names plus
# PRU. XOM/CVX/VLO/DVN/LNG/FANG had already been exited by this point.
ENERGY_BOOK = [
    ("MPC", 11467.0), ("PRU", 7325.0), ("COP", 7143.0), ("EOG", 6284.0),
    ("OXY", 5656.0), ("PSX", 5085.0), ("COR", 3425.0), ("ZBH", 3381.0),
    ("EXPD", 3190.0), ("AFL", 3060.0), ("CTAS", 2871.0), ("CME", 2157.0),
    ("RCL", 1902.0), ("TMO", 1723.0), ("GWW", 1382.0), ("GIS", 1290.0),
    ("TDG", 1254.0), ("EXPE", 1178.0), ("PH", 977.0), ("MCK", 856.0),
    ("SMH", 538.0), ("AMGN", 385.0), ("WAT", 377.0), ("SNOW", 291.0),
    ("DLTR", 254.0), ("BKNG", 192.0), ("MRVL", 186.0), ("PODD", 165.0),
    ("ABNB", 152.0), ("SJM", 119.0), ("CSCO", 116.0), ("EL", 83.0),
    ("ARKK", 71.0), ("APTV", 56.0), ("SLV", 52.0),
]
EQ = 113_690.49

# (a) the real map is loaded — the notebook's 16-ticker map would say "Other"
ns9, _ = sector_ns(EQ, ENERGY_BOOK)
check9("XOM maps to Energy (full map, not 'Other')", ns9["_sector_of"]("XOM"), "Energy")
check9("exposure aggregates by sector", round(ns9["_SECTOR_CAP"]["exposure"]["Energy"]), 35635)

# (b) energy at 31.3% of equity, cap 25% -> already over, so a further energy
#     BUY is refused, while an equal-sized BUY in an uncrowded sector passes.
ns9, _ = sector_ns(EQ, ENERGY_BOOK, ratio=0.25)
got9, printed9 = call9(ns9, "DVN", 3000.0)
check9("energy BUY refused at cap", got9, False)
check9("refusal names sector and is loud",
       ("[sector-cap] BLOCKED" in printed9 and "Energy" in printed9), True)
ns9, _ = sector_ns(EQ, ENERGY_BOOK, ratio=0.25)
check9("uncrowded-sector BUY still allowed", ns9["_sector_cap_allows"]("JNJ", 3000.0), True)

# (c) THE regression that matters: at the OLD 40% limit the 7/30 book passes.
#     This is why the pre-existing gate could not have prevented the incident.
ns9, _ = sector_ns(EQ, ENERGY_BOOK, ratio=0.40)
check9("at old 40% limit the 7/30 book still passes", ns9["_sector_cap_allows"]("DVN", 3000.0), True)

# (d) per-run accumulation: several energy BUYs in ONE run cannot each slip
#     through by being individually small. Ratio 0.40 leaves ~$9.8k of headroom
#     over the measured $35,635, so the first adds fit and the tail does not.
ns9, _ = sector_ns(EQ, ENERGY_BOOK, ratio=0.40)
seq = [ns9["_sector_cap_allows"]("DVN", 2500.0) for _ in range(5)]
check9("intra-run accumulation blocks the tail", (seq[0], seq[-1]), (True, False))

# (e) fail-CLOSED — the bug in the notebook gate was failing OPEN and silent.
ns9, _ = sector_ns(EQ, ENERGY_BOOK, boom=True)
got9, printed9 = call9(ns9, "XOM", 1000.0)
check9("broker read down -> BUY refused", got9, False)
check9("fail-closed refusal is printed, not silent", "fail-closed" in printed9, True)
ns9, _ = sector_ns(EQ, ENERGY_BOOK)
ns9["_SECTOR_CAP"]["ok"] = False
check9("exposure map absent -> BUY refused", ns9["_sector_cap_allows"]("XOM", 1.0), False)

# (f) release path: a gross-cap refusal must hand the sector reservation back,
#     or later BUYs are judged against exposure that was never submitted.
ns9, _ = sector_ns(EQ, ENERGY_BOOK, ratio=0.50)
ns9["_sector_cap_allows"]("DVN", 5000.0)
before = ns9["_SECTOR_CAP"]["submitted"]["Energy"]
ns9["_sector_cap_release"]("DVN", 5000.0)
check9("release returns the reservation", (before, ns9["_SECTOR_CAP"]["submitted"]["Energy"]),
       (5000.0, 0.0))

# (g) the cap never sells: an already-over-cap sector is frozen, and the gate
#     touches BUYs only — there is no SELL path in it at all.
ns9, out9 = sector_ns(EQ, ENERGY_BOOK, ratio=0.10)
check9("over-cap sector frozen, not liquidated", ns9["_sector_cap_allows"]("XOM", 1.0), False)
check9("over-cap sector surfaced in the log line", "OVER CAP" in out9, True)

# ── 10. SECTOR_MAP covers the whole traded universe (2026-07-31) ─────────────
# The sector cap is only as good as the map behind it. On 2026-07-31, 37 of the
# 307 traded tickers were absent and pooled into "Other" — including FANG, a
# pure energy name from the 7/24 losing batch, which meant the cap had a hole in
# the exact sector it was built for. An unmapped ticker is not a cosmetic gap:
# "Other" is enforced as though it were a real sector, so unrelated names can
# block each other while a genuinely concentrated sector goes uncounted.
# This check fails the build if the universe drifts ahead of the map again.
sector_map_src = prepatch[prepatch.index("SECTOR_MAP = {"):]
_ns10 = {}
exec(sector_map_src[:sector_map_src.index("\n}") + 2], _ns10)
SECTOR_MAP_LIVE = _ns10["SECTOR_MAP"]
print(f"10. SECTOR_MAP entries                               n={len(SECTOR_MAP_LIVE)}")

PRED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "predictions", "predictions.csv")
if not os.path.exists(PRED):
    print("10. predictions.csv absent — universe check SKIPPED (not a failure)")
else:
    import csv as _csv10
    _last, _rows10 = None, []
    with open(PRED, newline="", encoding="utf-8-sig") as _fh10:
        for _r10 in _csv10.DictReader(_fh10):
            _d10 = (_r10.get("pred_ts") or "")[:10]
            _tk10 = (_r10.get("ticker") or "").strip()
            if not _d10 or not _tk10:
                continue
            if _last is None or _d10 > _last:
                _last, _rows10 = _d10, []
            if _d10 == _last:
                _rows10.append(_tk10)
    universe = sorted(set(_rows10))
    unmapped = [t for t in universe if t not in SECTOR_MAP_LIVE]
    ok10 = not unmapped
    print(f"10. every traded ticker mapped ({_last}, n={len(universe)})   "
          f"{'PASS' if ok10 else 'FAIL'}")
    if not ok10:
        print(f"    unmapped -> would pool into 'Other': {' '.join(unmapped)}")
        fails.append(f"section10: {len(unmapped)} unmapped tickers: {','.join(unmapped)}")
    # No ticker may map to the literal "Other" — that is the pooling bucket the
    # _sector_of() fallback produces, never a sector anyone should assign.
    explicit_other = [t for t, s in SECTOR_MAP_LIVE.items() if s == "Other"]
    print("10. no ticker explicitly mapped to 'Other'           "
          + ("PASS" if not explicit_other else f"FAIL ({explicit_other})"))
    if explicit_other:
        fails.append(f"section10: explicit Other mappings {explicit_other}")
    # FANG regression: the specific miss that motivated this section.
    got_fang = SECTOR_MAP_LIVE.get("FANG")
    print(f"10. FANG maps to Energy (7/24 batch, was 'Other')    got={got_fang}  "
          f"{'PASS' if got_fang == 'Energy' else 'FAIL'}")
    if got_fang != "Energy":
        fails.append(f"section10: FANG maps to {got_fang}, want Energy")

# ── 11. sector-targeted trim sleeve (2026-07-31) ─────────────────────────────
# delever_account.py's `equity` sleeve is pro-rata across the whole book, so it
# shrinks gross without changing composition. The `sector` sleeve trims ONE
# sector. Only the guard rails are exercised here — every one of them returns
# BEFORE the first api() call, so this section never touches the network and
# never needs credentials.
import py_compile as _pyc11
DELEV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "delever_account.py")
try:
    _pyc11.compile(DELEV, doraise=True)
    print("11. delever_account.py py_compile                    PASS")
except Exception as _e11:
    fails.append(f"delever_account.py compile: {_e11}")
    print(f"11. delever_account.py py_compile                    FAIL ({_e11})")

def check11(name, got, want):
    ok = got == want
    print(f"11. {name:<52} got={str(got):<7} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"section11 {name}: got={got} want={want}")

os.environ.setdefault("ALPACA_API_KEY", "dummy")
os.environ.setdefault("ALPACA_SECRET_KEY", "dummy")
try:
    import importlib.util as _ilu11
    _spec11 = _ilu11.spec_from_file_location("delever_mod", DELEV)
    dv = _ilu11.module_from_spec(_spec11)
    _spec11.loader.exec_module(dv)

    # The map must come from quant_runner, not a copy — a trim that disagrees
    # with the cap about what "Energy" means is worse than no trim.
    sm11 = dv._load_sector_map()
    check11("sector map loaded from quant_runner", len(sm11), len(SECTOR_MAP_LIVE))
    check11("...and agrees on FANG", sm11.get("FANG"), "Energy")

    def guard(sector, ratio):
        buf = io.StringIO()
        dv.TRIM_SECTOR, dv.TARGET_RATIO = sector, ratio
        with contextlib.redirect_stdout(buf):
            rc = dv.trim_sector()
        return rc, buf.getvalue()

    rc11, out11 = guard("", 0.25)
    check11("empty sector refused", (rc11, "refusing" in out11), (1, True))
    rc11, out11 = guard("Nonsense", 0.25)
    check11("unknown sector refused, lists known", (rc11, "Known:" in out11), (1, True))
    rc11, out11 = guard("Energy", 1.0)
    check11("target>=1.0 refused (can never bind)", (rc11, "can never bind" in out11), (1, True))
    rc11, out11 = guard("energy", 1.5)
    check11("sector match is case-insensitive", "Energy <=" in out11, True)

    # Dispatch table: 'sector' must be a recognised sleeve.
    src11 = open(DELEV, encoding="utf-8").read()
    check11("main() dispatches sleeve=sector",
            ('"sector"' in src11 and "return trim_sector()" in src11), True)
    # It must never submit a BUY, on any path.
    sec_src11 = src11[src11.index("def trim_sector"):]
    sec_src11 = sec_src11[:sec_src11.index("\ndef ")] if "\ndef " in sec_src11 else sec_src11
    check11("sector trim never submits a BUY", '"side": "buy"' in sec_src11, False)
    check11("sector trim honours dry-run", 'MODE == "dry-run"' in sec_src11, True)
except Exception as _e11b:
    fails.append(f"section11 import/replay: {_e11b}")
    print(f"11. sector-sleeve replay                             FAIL ({_e11b})")

# Workflow must actually pass TRIM_SECTOR through, or the input is inert.
WF11 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    ".github", "workflows", "position_trim.yml")
wf11 = open(WF11, encoding="utf-8").read()
check11("workflow wires TRIM_SECTOR env",
        ("TRIM_SECTOR:" in wf11 and "inputs.sector" in wf11), True)
check11("workflow offers the sector choice", "short-cover, sector]" in wf11, True)

# ── 12. pnl_history bar dates are ET, not UTC (2026-08-05) ──────────────────
# quant_runner.py's portfolio/history loop rendered each 1D bar with
# utcfromtimestamp() while requesting extended_hours=true. An extended session
# ends 8 PM ET = EXACTLY 00:00 UTC next day in EDT, so every bar rolled over and
# every row in pnl_history.csv wore the following calendar day's label.
# Signature across the 48 rows written under the bug:
#     Tue 10  Wed 10  Thu 10  Fri 10  Sat 8  Mon 0
# Monday's session wore Tuesday's label and Friday's wore Saturday's, so no
# session ever landed on a Monday. (The two absent Saturdays are 2026-06-19
# Juneteenth and 2026-07-03 July-4th-observed — market holidays.)
# This poisoned two consumers: the Gain-to-Pain ratio buckets by MONTH, so every
# month boundary was off by one session; and the `date != _today_str` filter
# deleted the newest COMPLETED session every run, substituting an in-progress
# partial row (the 47/47/48-day curve stall observed 8/03-8/05).
# No network and no credentials — pure date arithmetic plus a source assertion.
import datetime as _dt12
from collections import Counter as _Counter12

def check12(name, got, want):
    ok = got == want
    print(f"12. {name:<52} got={str(got):<7} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"section12 {name}: got={got} want={want}")

# (a) the bug's exact call must be gone from the runner, and the ET conversion
#     must be present. A future edit that reinstates utcfromtimestamp here
#     silently re-arms a ten-week measurement corruption, so pin both halves.
check12("no utcfromtimestamp anywhere in quant_runner",
        "utcfromtimestamp" in src, False)
check12("history loop converts bars to ET",
        ('_ET_HIST' in src and 'ZoneInfo("America/New_York")' in src), True)
check12("history loop still requests extended_hours",
        '"extended_hours": "true"' in src, True)

try:
    from zoneinfo import ZoneInfo as _ZI12
    _ET12 = _ZI12("America/New_York")

    # (b) the mechanism itself: an extended-hours close instant must render to
    #     the SAME calendar day in ET and the NEXT one in UTC. If this ever
    #     stops holding, the premise behind the fix has changed.
    _close_edt = _dt12.datetime(2026, 8, 3, 20, 0, tzinfo=_ET12)      # 8 PM EDT
    _u12 = _close_edt.astimezone(_dt12.timezone.utc).strftime("%Y-%m-%d")
    _e12 = _close_edt.strftime("%Y-%m-%d")
    check12("Mon 8/03 20:00 ET renders 08-03 in ET", _e12, "2026-08-03")
    check12("...and 08-04 in UTC (the bug)", _u12, "2026-08-04")

    # (c) the regression signature. Replay a real trading week of extended-hours
    #     closes (Mon 7/27 - Fri 7/31) through both renderings. ET must produce
    #     no weekend label and a Monday; UTC must reproduce the observed
    #     zero-Monday/Saturday-present signature that motivated this section.
    _week12 = [_dt12.datetime(2026, 7, d, 20, 0, tzinfo=_ET12)
               for d in (27, 28, 29, 30, 31)]
    _dow_et = _Counter12(t.strftime("%a") for t in _week12)
    _dow_utc = _Counter12(t.astimezone(_dt12.timezone.utc).strftime("%a")
                          for t in _week12)
    check12("ET labels: zero weekend rows",
            _dow_et.get("Sat", 0) + _dow_et.get("Sun", 0), 0)
    check12("ET labels: Monday present", _dow_et.get("Mon", 0), 1)
    check12("UTC labels reproduce the bug (Sat row)", _dow_utc.get("Sat", 0), 1)
    check12("UTC labels reproduce the bug (no Monday)", _dow_utc.get("Mon", 0), 0)
except Exception as _e12b:
    fails.append(f"section12 date replay: {_e12b}")
    print(f"12. date-arithmetic replay                           FAIL ({_e12b})")

# (d) the live file. Rows written BEFORE the fix carry the broken labels and
#     cannot be retro-corrected here — the file is rebuilt from Alpaca every
#     run, so it self-heals on the first post-merge snapshot. This check is
#     therefore INFORMATIONAL until QT_PNLDATE_FIX_FROM names the first date
#     written by fixed code; from then on a returning zero-Monday window is a
#     hard failure. Set it in the workflow env once the fix has run once.
_PNLCSV12 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "predictions", "pnl_history.csv")
_fix_from12 = os.environ.get("QT_PNLDATE_FIX_FROM", "").strip()
if not os.path.exists(_PNLCSV12):
    print("12. pnl_history.csv absent — live signature SKIPPED (not a failure)")
else:
    import csv as _csv12
    with open(_PNLCSV12, newline="", encoding="utf-8-sig") as _fh12:
        _all12 = [(_r12.get("date") or "").strip()
                  for _r12 in _csv12.DictReader(_fh12)]
    _all12 = [_d for _d in _all12 if len(_d) == 10]
    _scope12 = [_d for _d in _all12 if not _fix_from12 or _d >= _fix_from12]
    _c12 = _Counter12(_dt12.date.fromisoformat(_d).strftime("%a") for _d in _scope12)
    _wk12 = _c12.get("Sat", 0) + _c12.get("Sun", 0)
    print(f"12. live pnl_history signature (n={len(_scope12)}, "
          f"from={_fix_from12 or 'ALL'})   "
          + "  ".join(f"{_k}={_c12.get(_k, 0)}"
                      for _k in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")))
    if not _fix_from12:
        print("    INFO only — QT_PNLDATE_FIX_FROM unset, so pre-fix rows are in "
              "scope and the broken signature is EXPECTED here.")
        print("    Set QT_PNLDATE_FIX_FROM to the first post-merge snapshot date "
              "to turn this into a hard regression gate.")
    elif len(_scope12) < 15:
        print(f"    INFO only — {len(_scope12)} rows since {_fix_from12} is too few "
              "to read a day-of-week signature (need >= 15).")
    else:
        check12("post-fix window has Monday rows", _c12.get("Mon", 0) > 0, True)
        check12("post-fix window has no weekend rows", _wk12, 0)

print()
if fails:
    print("VALIDATION FAILED:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("ALL VALIDATION CHECKS PASSED")
