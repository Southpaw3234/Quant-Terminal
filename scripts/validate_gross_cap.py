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
               'result.get("reason") in ("gross_cap", "stale_bar", "oversell")',
               '_oversell_cap(ticker, qty)'):
    if needle not in patched:
        fails.append(f"patched cell13 missing {needle!r}")
print("2.5 all 4 gate hooks present in patched src  "
      + ("PASS" if not any("missing" in f for f in fails) else "FAIL"))

# ── 3. behavioral replay of the prepatch gate ────────────────────────────────
prepatch = None
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "CELL_13_PREPATCH":
                if prepatch is None and isinstance(node.value, ast.Constant):
                    prepatch = node.value.value
gross_src = prepatch[prepatch.index("# ── Fix (2026-07-08): HARD gross-exposure cap"):]

def _stub_alpaca(equity, gross, boom=False):
    class _Acct: pass
    class _Pos: pass
    class _TC:
        def __init__(self, *a, **k):
            if boom:
                raise RuntimeError("api down")
        def get_account(self):
            a = _Acct(); a.equity = equity; return a
        def get_all_positions(self):
            p = _Pos(); p.market_value = gross; return [p]
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

print()
if fails:
    print("VALIDATION FAILED:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("ALL VALIDATION CHECKS PASSED")
