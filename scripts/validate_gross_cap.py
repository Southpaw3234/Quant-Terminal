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

print()
if fails:
    print("VALIDATION FAILED:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("ALL VALIDATION CHECKS PASSED")
