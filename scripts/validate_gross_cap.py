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

# ── 5. behavioral replay of the kill-switch window (era gate + temporal fix) ─
# Era gate (2026-07-16): the window must count only fresh-era
# (>= QT_STAGE1_START) equity BUY/SELL predictions — on 7/16 the matured 7/10
# stale-era batch (5 straight losses, wrong price_at_pred baselines) halted the
# new strategy's first open morning.
# Temporal fix (2026-08-07): the window was `.tail(N)` over FILE ORDER, i.e.
# the Cell-11 generation loop, so it read "the last N tickers processed on the
# most recent scored day" — and the universe is sector-grouped, so that tail is
# a correlated block. It misfired live on 8/07 (run 31183325178, ZERO entries):
# 7/31 scored 5W/9L, no streak, but its last five file rows were
# CAG/COP/PSX/MPC/VLO — four energy names, all losers. The window is now
# DAY-level: one synthetic row per pred DATE, verdict = that day's hit rate,
# streak measured in DATE order. Sorting the raw rows by pred_ts is NOT a fix
# and must never be mistaken for one — the intra-day timestamps ARE loop order,
# which the "file order is not time" check below pins.
import pandas as pd
import textwrap

ks_pair = next((new for old, new in pairs if old.lstrip().startswith("scored = plog[")), None)
assert ks_pair, "kill-switch rewrite pair not found in _SRC_REPLACE"
ks_src = textwrap.dedent(ks_pair)

def ks_scored(rows, env=None):
    df = pd.DataFrame(rows, columns=["pred_ts", "ticker", "action", "scored", "was_correct"])
    ns = {"plog": df, "KILL_CONSECUTIVE_LOSSES": 5, "__builtins__": __builtins__}
    os.environ.pop("QT_STAGE1_START", None)
    for k in ("QT_KILL_MIN_DAY_TRADES", "QT_KILL_DAY_HIT"):
        os.environ.pop(k, None)
    for k, v in (env or {}).items():
        os.environ[k] = v
    try:
        exec(ks_src, ns)
    finally:
        for k in (env or {}):
            os.environ.pop(k, None)
    return ns["scored"]

def ks_tripped(rows, env=None):
    scored = ks_scored(rows, env)
    return (len(scored) == 5
            and not scored["was_correct"].astype(str).isin(["True", "true"]).any())

def check5(name, got, want):
    ok = got == want
    print(f"5.  {name:<52} got={str(got):<5} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"kill-switch window {name}: got={got} want={want}")

def ks_day(date, wins, losses, action="BUY", tag=""):
    """One scored trading day: `wins` winners then `losses` losers, file order."""
    out = [(f"{date} 15:{i:02d}:00+0000", f"{tag}W{i}", action, "True", "True")
           for i in range(wins)]
    out += [(f"{date} 15:{30 + i:02d}:00+0000", f"{tag}L{i}", action, "True", "False")
            for i in range(losses)]
    return out

def ks_days(dates, wins, losses, action="BUY"):
    out = []
    for d in dates:
        out += ks_day(d, wins, losses, action, tag=d.replace("-", ""))
    return out

_L5 = ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24"]
_stale = ks_days(["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09",
                  "2026-07-10"], 0, 4)
_fresh_loss = ks_days(_L5, 0, 4)
_fresh_mixed = ks_days(_L5[:4], 0, 4) + ks_day("2026-07-27", 4, 0, tag="WIN")
_crypto = [(f"2026-07-2{i} 16:00:00+0000", "ETH-USD", "BUY", "True", "False")
           for i in range(5) for _ in range(4)]
_garbage = [("", "GARB1", "BUY", "True", "False"), ("nan", "GARB2", "BUY", "True", "False")]

# ── era gate (2026-07-16) — unchanged semantics, day-level fixtures ──
check5("7/16 reality: 5 stale-era loss days -> no trip", ks_tripped(_stale), False)
check5("stale-era rows fully excluded from window", len(ks_scored(_stale + _garbage)), 0)
check5("5 fresh-era loss days STILL trip the switch", ks_tripped(_stale + _fresh_loss), True)
check5("newest day a winner -> no trip", ks_tripped(_stale + _fresh_mixed), False)
check5("fresh-era crypto still excluded", len(ks_scored(_stale + _crypto)), 0)
check5("window = fresh equity DAYS only (mixed log)",
       len(ks_scored(_garbage + _stale + _crypto + ks_days(_L5[:3], 0, 4))), 3)

# ── temporal fix (2026-08-07) ──
# THE regression fixture: the exact 8/07 misfire. One day, 14 scored trades,
# 5W/9L, with the losers last in file order. `.tail(5)` sees five losses and
# trips; the day-level rule sees one 35.7% day and cannot trip on a single day.
_0731 = ks_day("2026-07-31", 5, 9, tag="J31")
assert [r[4] for r in _0731[-5:]] == ["False"] * 5, "fixture must end in 5 losing rows"
check5("8/07 misfire: 5 losing rows in ONE day -> no trip", ks_tripped(_0731), False)
check5("...and that day yields exactly one day-row", len(ks_scored(_0731)), 1)
# File order is not time. Five loss days are written AFTER a chronologically
# LATER winning day, so the raw tail is all losses while the newest DAY is a
# winner. A file-order (or pred_ts-sorted) window trips here; a date-ordered
# one must not. This is the check that fails if anyone reverts to .tail(rows).
_out_of_order = ks_day("2026-07-31", 6, 1, tag="LATE") + ks_days(_L5, 0, 4)
check5("file order is not time: newest DAY wins -> no trip",
       ks_tripped(_out_of_order), False)
check5("...same rows, streak measured in DATE order",
       list(ks_scored(_out_of_order).index)[-1], "2026-07-31")
# Thin days carry no verdict: below QT_KILL_MIN_DAY_TRADES they are skipped
# entirely, so one entry-starved session neither breaks nor extends a streak
# (7/28 really did score n=1).
check5("thin day (n<3) is skipped, not counted",
       len(ks_scored(ks_days(_L5[:3], 0, 4) + ks_day("2026-07-27", 0, 1, tag="THIN"))), 3)
check5("thin winning day cannot clear a real streak",
       ks_tripped(_fresh_loss + ks_day("2026-07-27", 1, 0, tag="THIN")), True)
check5("thin day counts once it clears the minimum",
       len(ks_scored(ks_days(_L5[:3], 0, 4) + ks_day("2026-07-27", 0, 3, tag="OK"))), 4)
# A day at exactly the hit threshold is a WIN day (>= QT_KILL_DAY_HIT), which
# is why the real 7/20 and 7/21 (both exactly 50%) broke the July streak.
check5("day at exactly 50% hit rate is a win day",
       ks_tripped(ks_days(_L5[:4], 0, 4) + ks_day("2026-07-27", 2, 2, tag="EVEN")), False)
check5("day just under the threshold is a loss day",
       ks_tripped(ks_days(_L5[:4], 0, 4) + ks_day("2026-07-27", 2, 3, tag="UNDER")), True)
# Knobs must actually bind, or the defaults are unadjustable in an incident.
check5("QT_KILL_DAY_HIT knob binds",
       ks_tripped(ks_days(_L5, 2, 3), {"QT_KILL_DAY_HIT": "0.3"}), False)
check5("QT_KILL_MIN_DAY_TRADES knob binds",
       len(ks_scored(ks_days(_L5[:3], 0, 4), {"QT_KILL_MIN_DAY_TRADES": "9"})), 0)
# SELL exits count the same as BUY entries, and an empty log never trips.
check5("SELL rows counted alongside BUY", ks_tripped(ks_days(_L5, 0, 4, "SELL")), True)
check5("empty prediction log -> no trip", ks_tripped([]), False)
check5("empty log yields an empty window", len(ks_scored([])), 0)
# The reworded trip message must actually land: a silent anchor miss would log
# "5 consecutive losses" while counting days.
_msg_pair = next(((old, new) for old, new in pairs
                  if old.startswith('f"{KILL_CONSECUTIVE_LOSSES} consecutive losses')), None)
check5("trip-message rewrite pair present", _msg_pair is not None, True)
_nb_all = "".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code")
check5("...its anchor appears exactly once in the notebook",
       _nb_all.count(_msg_pair[0]) if _msg_pair else -1, 1)
check5("...and rewrites to 'losing days'",
       "consecutive losing days" in (_msg_pair[1] if _msg_pair else ""), True)
# The window anchor itself must still match live source, or the whole rewrite
# is a silent no-op and the switch reverts to raw tail(5) of the file.
_ks_anchor = next((old for old, new in pairs if old.lstrip().startswith("scored = plog[")), None)
check5("window anchor appears exactly once in the notebook",
       _nb_all.count(_ks_anchor) if _ks_anchor else -1, 1)

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
    # Drop the trailing row before reading the signature. It is the LIVE
    # enriched snapshot (the only row carrying open_positions), stamped with
    # today's ET date by the today-row path — so on a Saturday or Sunday cycle
    # it legitimately IS a weekend row, and it self-clears on the next trading
    # day. Asserting over it would fail this suite every weekend for a reason
    # that has nothing to do with the bar-labelling bug being guarded here.
    _live12 = _all12[-1] if _all12 else None
    _settled12 = _all12[:-1]
    _scope12 = [_d for _d in _settled12 if not _fix_from12 or _d >= _fix_from12]
    _c12 = _Counter12(_dt12.date.fromisoformat(_d).strftime("%a") for _d in _scope12)
    _wk12 = _c12.get("Sat", 0) + _c12.get("Sun", 0)
    print(f"12. live pnl_history signature (n={len(_scope12)} settled, "
          f"from={_fix_from12 or 'ALL'}, live row {_live12} excluded)   "
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

# ── 13. rank_score + the Frame-1 v2 parallel series (2026-08-05) ────────────
# `confidence` is not a ranking score: Cell 13's ternary execution gate
# overwrites it to exactly 0.50 for every HOLD and SELL, and log_prediction
# runs AFTER that — so predictions.csv recorded the execution flag. Measured
# over 2026-07-14..07-29: 97.5% of all (day,name) rows sat at the modal value,
# ~7 of 279 names carried a distinct value, and on 17 of 17 days NOT ONE name
# scored below 0.5, so the short decile never held a model-selected name.
# rank_score captures the same value BEFORE the gate. This section pins the
# capture, the logging anchor, the dual-series plumbing, and the tie diagnostic.
def check13(name, got, want):
    ok = got == want
    print(f"13. {name:<52} got={str(got):<7} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"section13 {name}: got={got} want={want}")


# (a) the runner stashes rank_score BEFORE the gate flattens confidence.
_i_stash = src.find('signals[_tk13_r]["rank_score"]')
_i_flat = src.find('signals[_tk13_w]["confidence"] = 0.50')
check13("runner captures rank_score", _i_stash >= 0, True)
check13("...and does so BEFORE the 0.50 flatten", 0 <= _i_stash < _i_flat, True)

# (b) the log_prediction anchor is unique in Cell 13 and survives patching.
_anchor13 = '        "confidence":   sig["confidence"],\n'
check13("log_prediction anchor unique in Cell 13", cell13.count(_anchor13), 1)
check13("patched Cell 13 logs rank_score",
        '"rank_score":   sig.get("rank_score", None)' in patched, True)
# Guard the placement note in _SRC_REPLACE: section 2 asserts uniqueness on
# pairs[-3:], so the new anchor must NOT sit inside that window or it silently
# evicts gross-cap anchor (a) from the check.
check13("new anchor kept out of the pairs[-3:] window",
        any(_anchor13 in _o for _o, _n in pairs[-3:]), False)

# (c) analyzer plumbing: env-configurable, legacy defaults unchanged.
RIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "analyze_rank_ic.py")
ric = open(RIC, encoding="utf-8-sig").read()
try:
    py_compile.compile(RIC, doraise=True)
    print("13. analyze_rank_ic.py py_compile                    PASS")
except Exception as _e13:
    fails.append(f"analyze_rank_ic.py compile: {_e13}")
    print(f"13. analyze_rank_ic.py py_compile                    FAIL ({_e13})")
check13("score column is env-configurable",
        'os.environ.get("QT_RANK_SCORE_COL", "confidence")' in ric, True)
check13("legacy default output path unchanged",
        '"QT_RANK_IC_OUT", "data/shadow/rank_ic.csv"' in ric, True)
check13("no hardcoded g[\"confidence\"] left in the loop",
        'zip(g["ticker"], g["confidence"])' in ric, False)
check13("loop reads SCORE_COL", 'zip(g["ticker"], g[SCORE_COL])' in ric, True)
check13("missing score column exits 0, not abort",
        "series not started. Exiting 0." in ric, True)

# (d) the tie diagnostic — the check that would have caught this on day one.
check13("tie diagnostic present", "ranking-variable health" in ric, True)
check13("...flags a degenerate short side", "days with NO name below 0.5" in ric, True)
# Replay its arithmetic on a synthetic degenerate column: 270 of 279 pinned at
# 0.50 and nothing below it must trip the warning; a healthy spread must not.
import pandas as _pd13
from collections import Counter as _C13
_bad = _pd13.Series([0.5] * 270 + [0.55 + 0.01 * i for i in range(9)])
_good = _pd13.Series([0.30 + 0.002 * i for i in range(279)])
for _lbl, _s, _want_warn in (("degenerate", _bad, True), ("healthy", _good, False)):
    _pct = 100.0 * int(_s.value_counts().iloc[0]) / len(_s)
    _no_short = int((_s < 0.5).sum()) == 0
    check13(f"tie rule flags the {_lbl} column", (_pct >= 50.0 or _no_short), _want_warn)

# (f) BEHAVIOURAL replay of the gate itself: rank_score must survive the very
#     flatten that destroyed confidence. Source-order checks above prove the
#     capture is written first; this proves it actually SURVIVES, which is the
#     property the whole v2 series depends on.
_b0 = src.find('_orig_signals_13 = dict(signals)')
_b1 = src.find('SELL signals converted to close-long")', _b0)
if _b0 < 0 or _b1 < 0:
    fails.append("section13: could not locate the ternary-gate block for replay")
    print("13. ternary-gate replay                              FAIL (block not found)")
else:
    _gate_src = src[_b0:_b1 + len('SELL signals converted to close-long")')]
    _sig13 = {
        "AAA": {"confidence": 0.6412, "ternary_label": "BUY"},
        "BBB": {"confidence": 0.5183, "ternary_label": "HOLD"},
        "CCC": {"confidence": 0.3120, "ternary_label": "SELL"},   # the erased short
        "DDD": {"confidence": 0.4977, "ternary_label": "HOLD"},
    }
    _ns13 = {"signals": _sig13, "__builtins__": __builtins__}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(_gate_src, _ns13)
    _sg = _ns13["signals"]
    # confidence is flattened exactly as before — the execution gate is untouched
    check13("replay: HOLD confidence still flattened", _sg["BBB"]["confidence"], 0.50)
    check13("replay: SELL confidence still flattened", _sg["CCC"]["confidence"], 0.50)
    check13("replay: BUY confidence untouched", _sg["AAA"]["confidence"], 0.6412)
    # ...but rank_score preserves every original value, including the SELL's
    check13("replay: rank_score survives on HOLD", _sg["BBB"]["rank_score"], 0.5183)
    check13("replay: rank_score survives on the SELL", _sg["CCC"]["rank_score"], 0.3120)
    check13("replay: rank_score survives on BUY", _sg["AAA"]["rank_score"], 0.6412)
    # the decisive one: v2 gets a short side, the legacy series never could
    check13("replay: legacy col has NO name below 0.5",
            any(float(v["confidence"]) < 0.5 for v in _sg.values()), False)
    check13("replay: rank_score DOES have names below 0.5",
            sum(1 for v in _sg.values() if float(v["rank_score"]) < 0.5), 2)

# (e) the workflow actually runs the second series — an unwired env is inert.
WF13 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    ".github", "workflows", "quant_daily.yml")
wf13 = open(WF13, encoding="utf-8").read()
check13("workflow wires QT_RANK_SCORE_COL=rank_score",
        "QT_RANK_SCORE_COL: rank_score" in wf13, True)
check13("...to its OWN output files (legacy not overwritten)",
        ("QT_RANK_IC_OUT: data/shadow/rank_ic_v2.csv" in wf13
         and "QT_RANK_LS_OUT: data/shadow/cross_sectional_ls_v2.csv" in wf13), True)
check13("both invocations still run", wf13.count("python analyze_rank_ic.py"), 2)
check13("v2 step is morning-gated like the legacy one",
        wf13.count("if: steps.run_type.outputs.type == 'morning'") >= 2, True)


# ── SECTION 14 — settled-rows-only policy (2026-08-06) ──────────────────────
# The newest row of the Frame-1 series used to be PROVISIONAL: analyze_rank_ic
# runs at ~11:50 ET on the day each book matures, so its exit bar was that
# session's UNSETTLED intraday print, and the next run silently restated the
# row off the real close. Observed: 7/24 -0.1186 -> -0.0389 (-67%) and 7/27
# +0.0168 -> -0.0387 (SIGN FLIP). Fix: a day enters only once a LATER bar
# exists, which proves the exit bar is a completed session without consulting
# a clock, a timezone or a market calendar. This section drives _fwd_ret
# directly — the restatement scenario is reproduced, not just grepped for.
def check14(name, got, want):
    ok = got == want
    print(f"14. {name:<52} got={str(got):<7} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"section14 {name}: got={got} want={want}")


import importlib.util as _ilu

_spec14 = _ilu.spec_from_file_location("_ric14", RIC)
_ric14 = _ilu.module_from_spec(_spec14)
_spec14.loader.exec_module(_ric14)          # safe: main() is under __main__

# 8 consecutive bars, close = 100..107. The LAST bar (index 7) stands in for
# today's unsettled intraday print.
_idx14 = pd.date_range("2026-08-03", periods=8, freq="D")
_s14 = pd.Series([100.0 + i for i in range(8)], index=_idx14)
_px14 = {"AAA": _s14}
_h14 = 5


def _fr14(day_i, settled):
    return _ric14._fwd_ret(_px14, "AAA", _idx14[day_i].strftime("%Y-%m-%d"),
                           _h14, settled_only=settled)


# entry index 0 -> exit index 5, two bars behind the newest: settled either way.
check14("settled row computes (exit 2 bars back)", round(_fr14(0, True), 6), 0.05)
check14("...and is IDENTICAL with the policy off", _fr14(0, True), _fr14(0, False))
# entry index 1 -> exit index 6, exactly one bar behind the newest: still settled.
check14("boundary: exit one bar back is settled", round(_fr14(1, True), 6),
        round(106.0 / 101.0 - 1.0, 6))
# entry index 2 -> exit index 7 == the newest bar. THE regression: this is the
# row that used to be written provisionally and then restated.
check14("PROVISIONAL row is withheld", _fr14(2, True), None)
check14("...but the old behaviour still reproduces it",
        round(_fr14(2, False), 6), round(107.0 / 102.0 - 1.0, 6))
# entry index 3 -> exit index 8, past the end: unmatured under both policies.
check14("unmatured row withheld either way",
        (_fr14(3, True), _fr14(3, False)), (None, None))
# a one-bar series can't settle anything and must not raise
check14("degenerate 1-bar series returns None",
        _ric14._fwd_ret({"AAA": _s14.iloc[:1]}, "AAA", "2026-08-03", 5), None)

# Default must be ON, and the escape hatch must actually work — the flag is
# read at import, and both series are rebuilt from scratch every run, so it
# round-trips exactly.
check14("QT_SETTLED_ONLY defaults ON", _ric14.SETTLED_ONLY, True)
check14("_fwd_ret defaults to settled_only=True",
        _ric14._fwd_ret(_px14, "AAA", _idx14[2].strftime("%Y-%m-%d"), _h14), None)
os.environ["QT_SETTLED_ONLY"] = "0"
_spec14.loader.exec_module(_ric14)
check14("QT_SETTLED_ONLY=0 restores provisional rows", _ric14.SETTLED_ONLY, False)
del os.environ["QT_SETTLED_ONLY"]
_spec14.loader.exec_module(_ric14)
check14("...and unsetting it restores the default", _ric14.SETTLED_ONLY, True)

# Wiring: BOTH call sites must honour the flag. Missing it on the SPY leg would
# hedge settled picks against an unsettled market return — a silent asymmetry.
check14("picks leg honours the flag",
        "_fwd_ret(prices, tk, date, h, settled_only=SETTLED_ONLY)" in ric, True)
check14("SPY/beta leg honours the flag",
        '_fwd_ret(prices, "SPY", d, int(h), settled_only=SETTLED_ONLY)' in ric, True)
check14("withheld day is reported, not silent", "withholding" in ric, True)
# The report must WALK the missing days newest-first, not read only the newest.
# Every pred-day inside the last HORIZON sessions is missing too (unmatured,
# n=0) and sits ABOVE the provisional day in date order, so reading index [0]
# prints nothing and the policy goes silent. Caught by hand on 8/06 before the
# first CI run; pinned here so it cannot come back.
check14("withheld-day report walks past unmatured days",
        "for _d in _missing[:10]:" in ric, True)
check14("...and stops at the first reportable day", ric.count("break") >= 1, True)
# The policy is series-agnostic: v2 reads the same code path, so it can never
# accumulate provisional rows the legacy series has been purged of.
check14("policy applies to BOTH series (single code path)",
        ric.count("settled_only=SETTLED_ONLY"), 2)

# ── 15. kill switch fails CLOSED when it cannot evaluate (2026-08-07) ────────
# The consecutive-loss block sat in a bare `except Exception: pass`, so any
# error in it silently DISABLED the brake and let the run trade on unprotected
# — the failure mode you never see, because the log looks clean. It now halts
# fail-closed, matching the gross-cap gate (section 3d). Two conditions stay
# benign (no history yet, not a broken check): a missing log and a zero-byte
# one. Escape hatch QT_KILL_STREAK_FAILOPEN=1 restores the old behaviour.
# Unlike section 5, which execs the window fragment, this replays the WHOLE
# patched check_kill_switch() end to end — the error path only exists in the
# function body, and a fragment test cannot see it.
import pathlib, tempfile

_ksf_i = _nb_all.index("def check_kill_switch(")
_ksf_src = _nb_all[_ksf_i:_nb_all.index("\ndef ", _ksf_i)]
for _o, _n in pairs:
    _ksf_src = _ksf_src.replace(_o, _n)

_ks_tmp = tempfile.mkdtemp(prefix="qt_ks_")
_ks_ns = {
    "pd": pd,
    "KILL_FLAG_FILE": pathlib.Path(_ks_tmp) / "no_such_kill_flag",
    "MACRO": {"vix": 15.0},
    "KILL_VIX_THRESHOLD": 40.0,
    "KILL_CONSECUTIVE_LOSSES": 5,
    "KILL_DAILY_LOSS_PCT": 0.05,
    "PRED_LOG_FILE": pathlib.Path(_ks_tmp) / "missing.csv",
    "__builtins__": __builtins__,
}
try:
    exec(_ksf_src, _ks_ns)
    _ks_fn = _ks_ns["check_kill_switch"]
except Exception as e:            # a broken patch must not pass silently
    _ks_fn = None
    fails.append(f"section 15: patched check_kill_switch failed to exec: {e}")

def ks_write(name, rows, header=True):
    p = pathlib.Path(_ks_tmp) / name
    cols = "pred_ts,ticker,action,scored,was_correct\n"
    p.write_text((cols if header else "") + "".join(",".join(r) + "\n" for r in rows))
    return p

def ks_call(path, env=None):
    """(killed, reason, printed) from the fully patched function."""
    os.environ.pop("QT_KILL_STREAK_FAILOPEN", None)
    for k, v in (env or {}).items():
        os.environ[k] = v
    _ks_ns["PRED_LOG_FILE"] = path
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            killed, reason = _ks_fn(None)
    finally:
        for k in (env or {}):
            os.environ.pop(k, None)
    return killed, reason, buf.getvalue()

def check15(name, got, want):
    ok = got == want
    print(f"15. {name:<52} got={str(got):<5} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"kill-switch fail-closed {name}: got={got} want={want}")

if _ks_fn:
    _ok_rows = [(f"2026-07-2{d} 15:{i:02d}:00+0000", f"T{d}{i}", "BUY", "True", "False")
                for d in range(5) for i in range(4)]
    _ok = ks_write("streak.csv", _ok_rows)
    _win = ks_write("win.csv", _ok_rows + [("2026-07-27 15:00:00+0000", f"W{i}",
                                            "BUY", "True", "True") for i in range(4)])
    # Baseline: the happy paths still behave, so a PASS below is not vacuous.
    _k, _r, _ = ks_call(_ok)
    check15("real 5-losing-day streak still trips", _k, True)
    check15("...and the reason says DAYS, not losses",
            "consecutive losing days" in _r, True)
    _k, _r, _ = ks_call(_win)
    check15("newest day a winner -> no trip", _k, False)

    # THE fix: a log the check cannot evaluate must HALT, loudly, not sail past.
    _broken = ks_write("broken.csv", [("2026-07-20 15:00:00+0000", "AAA", "BUY", "True")],
                       header=False)
    _broken.write_text("pred_ts,ticker,action,scored\n2026-07-20,AAA,BUY,True\n")
    _k, _r, _out = ks_call(_broken)
    check15("unreadable log -> FAIL-CLOSED halt", _k, True)
    check15("...reason names the fail-closed halt", "fail-closed" in _r, True)
    check15("...reason names the exception type", "KeyError" in _r, True)
    check15("...and it is LOUD (prints a traceback)", "Traceback" in _out, True)
    check15("...and says why it halted", "FAIL-CLOSED" in _out, True)
    # Escape hatch, for an incident where the brake itself is the problem.
    _k, _r, _out = ks_call(_broken, {"QT_KILL_STREAK_FAILOPEN": "1"})
    check15("QT_KILL_STREAK_FAILOPEN=1 restores fail-open", _k, False)
    check15("...and warns it is running unprotected", "UNPROTECTED" in _out, True)

    # Benign: no history yet is not a broken check. These must NOT halt, or a
    # fresh clone / first run of a new era can never place a trade.
    _k, _r, _out = ks_call(pathlib.Path(_ks_tmp) / "missing.csv")
    check15("missing prediction log -> no halt", _k, False)
    check15("...and is reported, not silent", "no usable prediction log" in _out, True)
    _empty = pathlib.Path(_ks_tmp) / "empty.csv"; _empty.write_text("")
    _k, _r, _out = ks_call(_empty)
    check15("zero-byte prediction log -> no halt", _k, False)
    # A header-only file needs no special case: it parses to an empty frame,
    # yields an empty window, and cannot reach the trip condition.
    _k, _r, _out = ks_call(ks_write("headeronly.csv", []))
    check15("header-only log -> no halt, no error", _k, False)
    check15("...and took the normal path, not the error path",
            "streak check FAILED" in _out, False)

    # The other checks in the function must be untouched by this edit.
    _ks_ns["MACRO"] = {"vix": 55.0}
    _k, _r, _ = ks_call(_win)
    check15("VIX check still trips independently", _k, True)
    check15("...with its own reason", "VIX" in _r, True)
    _ks_ns["MACRO"] = {"vix": 15.0}

# The anchor MUST carry the trailing comment: `except Exception:` alone appears
# ~119x in the notebook, so a bare anchor would rewrite arbitrary handlers.
_fc_pair = next(((o, n) for o, n in pairs if o.startswith("    except Exception:\n")), None)
check15("fail-closed rewrite pair present", _fc_pair is not None, True)
check15("...anchor appears exactly once in the notebook",
        _nb_all.count(_fc_pair[0]) if _fc_pair else -1, 1)
check15("...anchor is disambiguated by trailing context",
        _fc_pair[0].strip().endswith("# 4. Daily P&L from Alpaca") if _fc_pair else False, True)
check15("bare 'except Exception:' would NOT have been unique",
        _nb_all.count("    except Exception:\n") > 1, True)

# ── 16. drawdown brake fails CLOSED when it cannot evaluate (2026-08-07) ─────
# The Alpaca/pnl_history drawdown block already refused to GUESS a denominator
# (the 6/30 phantom trip) and refused to CLEAR a stale flag when blind. What it
# did NOT do was stop trading: both sources dead meant the run proceeded with
# no drawdown brake at all, announced by one "(non-fatal)" line.
# The halt is deliberately shaped by two facts about THIS brake:
#   (1) a real trip writes a PERSISTENT _KILL_FLAG, so an evaluation failure
#       must NOT write it or the halt latches until a valid reading appears;
#   (2) the flag path skips cells 10-13, which would stall all three evidence
#       clocks — so this blocks ENTRIES only, via the same `halt` path the
#       streak check uses, and predictions keep logging.
# ⚠️ Check 4 inside check_kill_switch() (`if api:` -> Alpaca daily loss) is
# DEAD CODE and is NOT what protects the account — every call site passes None.
# It is pinned dead below so it cannot come alive unnoticed.
def check16(name, got, want):
    ok = got == want
    print(f"16. {name:<52} got={str(got):<5} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"drawdown fail-closed {name}: got={got} want={want}")

# The decision block is plain control flow over module state, so replay it
# directly against the real source rather than re-implementing the rules here.
_dd_i = src.index("    _ks_keys = bool(")
_dd_src = textwrap.dedent(src[_dd_i:src.index("if _KILL_FLAG.exists():", _dd_i)])

def dd_run(triggered, evaluated, keys, failopen=False):
    ns = {"os": os, "print": lambda *a, **k: None,
          "_pnl_kill_triggered": triggered, "_ks_evaluated": evaluated,
          "__builtins__": __builtins__}
    for k, v in (("ALPACA_API_KEY", "k"), ("ALPACA_SECRET_KEY", "s")):
        os.environ[k] = v if keys else ""
    if failopen:
        os.environ["QT_KILL_DD_FAILOPEN"] = "1"
    else:
        os.environ.pop("QT_KILL_DD_FAILOPEN", None)
    try:
        exec(_dd_src, ns)
    finally:
        for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "QT_KILL_DD_FAILOPEN"):
            os.environ.pop(k, None)
    return ns["_ks_blind_halt"]

# THE fix: keys set, neither source could read -> halt.
check16("blind with keys set -> FAIL-CLOSED halt", dd_run(False, False, True), True)
# Benign: no keys is local paper mode, a bootstrap state, not a broken check.
# Same split the gross-cap gate draws (3d fail-closed vs 3e ledger fallback).
check16("blind without keys (paper mode) -> no halt", dd_run(False, False, False), False)
# A valid reading, or a real breach, must not be disturbed by any of this.
check16("valid reading -> no blind halt", dd_run(False, True, True), False)
check16("real breach -> no blind halt (flag path owns it)", dd_run(True, False, True), False)
check16("breach AND evaluated -> no blind halt", dd_run(True, True, True), False)
check16("escape hatch QT_KILL_DD_FAILOPEN=1", dd_run(False, False, True, True), False)
# The halt must be run-scoped. If it ever writes the persistent flag it can
# latch indefinitely whenever the data source itself is broken.
check16("blind halt NEVER writes the persistent flag",
        "_KILL_FLAG.write_text" in _dd_src, False)
check16("...and is a namespace entry, not a file",
        'namespace["_QT_DD_BLIND_HALT"]' in src, True)
# Entries only: it must NOT reach for SKIP_CELLS, which would stop signal
# generation and stall Frame 1/2/3 on a day the account is merely unreadable.
check16("blind halt does NOT skip cells 10-13 (clocks live)",
        "SKIP_CELLS" in _dd_src, False)
# Wiring: the notebook side must consult it, ABOVE every other check.
_dd_pair = next(((o, n) for o, n in pairs
                 if o.startswith("    # 1. Manual kill flag file")), None)
check16("check_kill_switch consults _QT_DD_BLIND_HALT", _dd_pair is not None, True)
check16("...anchor appears exactly once in the notebook",
        _nb_all.count(_dd_pair[0]) if _dd_pair else -1, 1)
check16("...and is checked BEFORE the manual flag",
        _dd_pair[1].index("_QT_DD_BLIND_HALT")
        < _dd_pair[1].index("KILL_FLAG_FILE.exists()") if _dd_pair else False, True)
if _ks_fn:      # end-to-end through the patched function from section 15
    _ks_ns["_QT_DD_BLIND_HALT"] = True
    _k, _r, _ = ks_call(pathlib.Path(_ks_tmp) / "win.csv")
    check16("patched function halts on the blind flag", _k, True)
    check16("...with a reason naming the cause", "drawdown unreadable" in _r, True)
    _ks_ns["_QT_DD_BLIND_HALT"] = False
    _k, _r, _ = ks_call(pathlib.Path(_ks_tmp) / "win.csv")
    check16("...and is inert when not set", _k, False)

# ⚠️ Check 4 (`if api:` daily loss) is DEAD: every call site passes None, so it
# has never executed and is NOT the drawdown brake. Pinned so that wiring an
# api in — which would quietly resurrect an unvalidated, fail-open check —
# fails the build and forces a deliberate decision.
_ks_calls = re.findall(r"check_kill_switch\(([^)]*)\)", _nb_all)
_ks_invocations = [c for c in _ks_calls if "api=None" not in c]
check16("every check_kill_switch call site passes None",
        all(c.strip() in ("None", "") for c in _ks_invocations), True)
check16("...so check 4 (`if api:`) is unreachable dead code",
        _nb_all.count("account = api.get_account()"), 1)
check16("...and the REAL brake is the Alpaca/pnl_history block",
        "[KILL SWITCH · Alpaca]" in src and "_ks_pnl_fallback(" in src, True)


# ── 17. close_long: driven by the held book, and never silent ────────────────
# The 8/10 review found `close_long: closed 0/4` was indistinguishable from a
# total broker outage. Root cause was two-fold: the loop was driven by the
# SELL-labelled *signal universe* (which never intersects the book, because the
# net-of-cost bar admits only names pinned at the 0.10 confidence clamp), and
# every per-name failure was swallowed by `except Exception: pass`. These
# scenarios pin both the new driver and the new logging.
def _patch_const(name):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    if isinstance(node.value, ast.Constant):
                        return node.value.value
    return None

# The body is a CALLABLE in the prepatch now (2026-08-11), so that the intraday
# branch can invoke it before Cell 13's SystemExit. Exec the definition, then
# call it — the scenarios below drive the real function, not a copy.
_cl_src = _patch_const("_CELL_13_CLOSE_LONG")
_postpatch = _patch_const("CELL_13_POSTPATCH")

def _stub_alpaca_cl(book, boom_read=False, boom_syms=()):
    """book: {symbol: signed qty}. Returns the list submitted orders land in."""
    sent = []
    class _Pos: pass
    class _Req:
        def __init__(self, **kw):
            self.symbol = kw["symbol"]; self.qty = kw["qty"]
    class _TC:
        def __init__(self, *a, **k): pass
        def get_all_positions(self):
            if boom_read:
                raise RuntimeError("alpaca 503")
            out = []
            for _s, _q in book.items():
                p = _Pos(); p.symbol = _s; p.qty = _q; out.append(p)
            return out
        def submit_order(self, req):
            if req.symbol in boom_syms:
                raise RuntimeError("order rejected")
            sent.append((req.symbol, req.qty))
    mod_a = types.ModuleType("alpaca")
    mod_t = types.ModuleType("alpaca.trading")
    mod_c = types.ModuleType("alpaca.trading.client");   mod_c.TradingClient = _TC
    mod_r = types.ModuleType("alpaca.trading.requests"); mod_r.MarketOrderRequest = _Req
    mod_e = types.ModuleType("alpaca.trading.enums")
    mod_e.OrderSide = types.SimpleNamespace(SELL="sell")
    mod_e.TimeInForce = types.SimpleNamespace(DAY="day")
    mod_a.trading = mod_t
    mod_t.client, mod_t.requests, mod_t.enums = mod_c, mod_r, mod_e
    for _n, _m in (("alpaca", mod_a), ("alpaca.trading", mod_t),
                   ("alpaca.trading.client", mod_c),
                   ("alpaca.trading.requests", mod_r),
                   ("alpaca.trading.enums", mod_e)):
        sys.modules[_n] = _m
    return sent

def run_cl(book, sell_labelled=(), wind_down=False, sold=None, keys=True,
           boom_read=False, boom_syms=(), where="postpatch", calls=1):
    sent = _stub_alpaca_cl(book, boom_read, boom_syms)
    os.environ["QT_WIND_DOWN"] = "1" if wind_down else ""
    ns = {
        "ALPACA_API_KEY":    "key" if keys else "",
        "ALPACA_SECRET_KEY": "sec" if keys else "",
        "signals":  {t: {"close_long": True} for t in sell_labelled},
        "_OVERSELL": {"sold": dict(sold or {})},
    }
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(_cl_src, "<close_long>", "exec"), ns)
        for _ in range(calls):
            ns["_qt_close_long_run"](where)
    return sent, buf.getvalue()

def check17(name, got, want):
    ok = got == want
    print(f"17. {name:<52} got={str(got):<5} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"close_long {name}: got={got} want={want}")

# (a) The 8/10 reality: 4 SELL labels, none of them held. Still closes nothing —
#     that is correct — but the log must now say WHY, and name the book size.
_BOOK_0810 = {t: 10.0 for t in ("IQV HSY ELV SNOW TMO MSFT EL NUE DLTR SJM ARKK ZBH "
                                "SLV CME BA GIS ETN SMH MRVL TDG AMZN DPZ CTAS RCL").split()}
_sent, _out = run_cl(_BOOK_0810, sell_labelled=("AMAT", "CSCO", "SPG", "TTWO"))
check17("8/10 replay: no held name selected", len(_sent), 0)
check17("...reports the book size, not a bare 0/N", "book 24" in _out, True)
check17("...and names the SELL-labelled misses", "not held 4" in _out, True)
check17("...listing them explicitly", "AMAT, CSCO, SPG, TTWO" in _out, True)

# (b) A SELL label that IS held must actually close, unchanged from old intent.
_sent, _out = run_cl(dict(_BOOK_0810, CSCO=7.0), sell_labelled=("CSCO", "SPG"))
check17("held SELL-labelled name closes", _sent, [("CSCO", 7)])
check17("...and the unheld one is still reported", "not held 1" in _out, True)

# (c) QT_WIND_DOWN=1 selects the whole long book — the lever that reaches flat.
_sent, _out = run_cl(_BOOK_0810, wind_down=True)
check17("wind-down closes every held long", len(_sent), 24)
check17("...and says so in the mode banner", "WIND-DOWN all longs" in _out, True)
check17("default (unset) does NOT liquidate", len(run_cl(_BOOK_0810)[0]), 0)

# (d) 7/15 short-book incident: a short must never be "closed" by a fresh SELL.
_sent, _out = run_cl({"CTAS": -160.0, "MSFT": 5.0}, wind_down=True)
check17("short is skipped, never doubled", _sent, [("MSFT", 5)])
check17("...and the skip is named", "skipped short: CTAS" in _out, True)

# (e) Qty already sold this run nets out — no oversell, and it is reported.
_sent, _out = run_cl({"MSFT": 5.0}, sell_labelled=("MSFT",), sold={"MSFT": 5})
check17("already-flat name is not re-sold", _sent, [])
check17("...and the skip is named", "skipped already-flat: MSFT" in _out, True)
_sent, _ = run_cl({"MSFT": 5.0}, sell_labelled=("MSFT",), sold={"MSFT": 2})
check17("partial fill sells only the remainder", _sent, [("MSFT", 3)])

# (f) THE REGRESSION THAT MOTIVATED THIS: a rejected order must be loud.
_sent, _out = run_cl(_BOOK_0810, wind_down=True, boom_syms=("MSFT", "BA"))
check17("rejected orders are counted", "errors 2" in _out, True)
check17("...named individually", "FAILED MSFT" in _out and "FAILED BA" in _out, True)
check17("...and flagged as a failed wind-down", "did" in _out and "NOT wind down" in _out, True)
check17("...while the rest of the book still closes", len(_sent), 22)

# (g) A broker outage must not render as "nothing to close".
_sent, _out = run_cl(_BOOK_0810, wind_down=True, boom_read=True)
check17("position-read failure closes nothing", len(_sent), 0)
check17("...and is reported as BLOCKED, not as 0", "BLOCKED" in _out, True)

# (h) No keys: inert, and says how many it would have considered.
_sent, _out = run_cl(_BOOK_0810, sell_labelled=("AMAT",), keys=False)
check17("no broker keys: skipped, nothing sent", len(_sent), 0)
check17("...and the skip names the count", "1 SELL tickers" in _out, True)
os.environ.pop("QT_WIND_DOWN", None)

# (i) The lever is wired in the workflow — an unset env is inert, and the two
#     flags are a PAIR: entries off without wind-down freezes the book (the
#     8/06-8/10 state), wind-down without entries off would re-buy what it just
#     sold. Both must be present, in the same env block, or this fails.
_wf17 = open(WF13, encoding="utf-8").read()
check17("workflow sets QT_WIND_DOWN=1", "QT_WIND_DOWN:          '1'" in _wf17, True)
check17("...alongside QT_MAX_GROSS=0 (no re-entry)",
        "QT_MAX_GROSS:          '0'" in _wf17, True)
_env17 = _wf17[_wf17.index("      - name: Run trading cycle"):]
_env17 = _env17[:_env17.index("GIT_USER_EMAIL")]
check17("...both inside the trading-cycle env block",
        "QT_WIND_DOWN" in _env17 and "QT_MAX_GROSS" in _env17, True)
# The stale "closes out naturally" rationale is what made the frozen book
# invisible for four sessions. Pinned so it cannot come back.
check17("stale 'closes out naturally' claim is gone",
        "closes out" in _wf17 and "naturally" in _wf17, False)

# (j) 2026-08-11 — THE INTRADAY SystemExit. CELL_13_POSTPATCH is appended to the
#     END of Cell 13, and intraday runs sys.exit(0) partway through, so the
#     postpatch never ran (proven by run 31426032814: QT_WIND_DOWN=1, cell 13
#     unskipped, zero close_long lines). The body is now a callable invoked from
#     BOTH sides. These checks pin the wiring, the anchor, and idempotence.
check17("body is a callable in the prepatch", _cl_src is not None, True)
check17("...appended onto CELL_13_PREPATCH",
        'CELL_13_PREPATCH += "\\n\\n" + _CELL_13_CLOSE_LONG' in src, True)
check17("postpatch calls it, does not inline it",
        "_qt_close_long_run(\"postpatch\")" in (_postpatch or ""), True)
check17("...and the postpatch no longer submits orders itself",
        "submit_order" in (_postpatch or ""), False)
# The intraday anchor must exist exactly once in the notebook, or the rewrite is
# a silent no-op — the same failure class as the kill-switch message anchor.
_EXIT_ANCHOR = "    import sys as _isys; _isys.exit(0)"
check17("intraday exit anchor appears exactly once", _nb_all.count(_EXIT_ANCHOR), 1)
_cl_pair = next(((o, n) for o, n in pairs if o == _EXIT_ANCHOR), None)
check17("...and _SRC_REPLACE targets it", _cl_pair is not None, True)
check17("...calling close_long BEFORE the exit",
        _cl_pair[1].index("_qt_close_long_run")
        < _cl_pair[1].index("_isys.exit(0)") if _cl_pair else False, True)
# End-to-end: build the intraday Cell 13 the way quant_runner does and confirm
# the rewritten source still compiles.
if _cl_pair:
    _nb13 = next((c for c in nb["cells"]
                  if c["cell_type"] == "code"
                  and _EXIT_ANCHOR in "".join(c["source"])), None)
    _s13 = "".join(_nb13["source"]).replace(_cl_pair[0], _cl_pair[1]) if _nb13 else ""
    try:
        compile(_s13, "<cell13-intraday>", "exec")
        check17("patched intraday Cell 13 compiles", True, True)
    except SyntaxError as _e13:
        check17("patched intraday Cell 13 compiles", f"SyntaxError {_e13.lineno}", True)
# Idempotence: whichever site fires first does the work; the second is a no-op.
# Without this a morning run would close the book twice and go short.
_sent, _out = run_cl(_BOOK_0810, wind_down=True, calls=2)
check17("two call sites close the book ONCE", len(_sent), 24)
check17("...and the second call prints nothing", _out.count("close_long ["), 1)
_sent, _out = run_cl(_BOOK_0810, wind_down=True, where="intraday")
check17("intraday call site closes the book", len(_sent), 24)
check17("...and names itself in the log", "intraday]" in _out, True)

# (k) 2026-08-11 — THE FLAT INVARIANT. "closed N" is not proof of flat: orders
#     can be rejected, and get_all_positions() has been observed to OMIT a held
#     name (HON, five consecutive 8/10 reads). A name the broker never returns
#     cannot appear in `errors`. So once armed, a non-empty book must page.
_WD_JSON = "data/predictions/wind_down_state.json"

def run_wd(book, state=None, wind_down=True, armed_age=99999, drill="", ack=""):
    """Run close_long inside a temp cwd with a seeded wind_down_state.json."""
    cwd0, tmp = os.getcwd(), tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "data", "predictions"))
    if state is not None:
        st = dict(state)
        if st.get("armed"):
            st.setdefault("armed_at", __import__("time").time() - armed_age)
            st.setdefault("armed_at_iso", "2026-08-11T15:48:00Z")
        with open(os.path.join(tmp, _WD_JSON), "w") as fh:
            json.dump(st, fh)
    os.chdir(tmp)
    os.environ["QT_WIND_DOWN_DRILL"] = drill
    os.environ["QT_FLAT_ACK_SHORT"] = ack
    try:
        sent, out = run_cl(book, wind_down=wind_down)
        after = {}
        if os.path.exists(_WD_JSON):
            after = json.load(open(_WD_JSON))
    finally:
        os.chdir(cwd0)
        os.environ.pop("QT_WIND_DOWN_DRILL", None)
        os.environ.pop("QT_FLAT_ACK_SHORT", None)
    return sent, out, after

# First wind-down run: nothing armed yet, so a full book is NOT a breach.
_s, _o, _st = run_wd(_BOOK_0810, state=None)
check17("first wind-down run is not a breach", _st.get("breach"), False)
check17("...and it ARMS the invariant", _st.get("armed"), True)
check17("...recording what it closed", _st.get("last_closed"), 24)

# 🔑 A wind-down run that closes NOTHING must still arm. The book went flat on
# 8/11 before this code existed, so no later run will ever close anything —
# gating arming on `closed > 0` would leave the invariant permanently disarmed
# in exactly the state the account is in, and a re-entry would go uncaught.
_s, _o, _st = run_wd({}, state=None)
check17("a wind-down run closing NOTHING still arms", _st.get("armed"), True)
check17("...on an already-flat book", _st.get("last_seen_book"), 0)
check17("...and that is not itself a breach", _st.get("breach"), False)

# Armed, and the next run still sees a full book → BREACH.
_s, _o, _st = run_wd(_BOOK_0810, state={"armed": True, "closed": 24})
check17("armed + non-empty book = BREACH", _st.get("breach"), True)
check17("...prints the banner", "FLAT INVARIANT BREACHED" in _o, True)
check17("...names the open positions", "still open" in _o or "MSFT" in _o, True)
check17("...and records them for the gate", len(_st.get("still_open") or []), 24)

# Armed and genuinely flat → clean, and the breach flag CLEARS (never latches).
_s, _o, _st = run_wd({}, state={"armed": True, "closed": 24, "breach": True})
check17("armed + empty book = no breach", _st.get("breach"), False)
check17("...breach does NOT latch", "FLAT INVARIANT BREACHED" in _o, False)
check17("...and stays armed", _st.get("armed"), True)

# Grace window: a run moments after arming must not page on unfilled orders.
_s, _o, _st = run_wd(_BOOK_0810, state={"armed": True, "closed": 24}, armed_age=60)
check17("no breach inside the 15-min fill grace", _st.get("breach"), False)

# Wind-down off: the invariant must be inert, not page on a normal book.
_s, _o, _st = run_wd(_BOOK_0810, state={"armed": True, "closed": 24},
                     wind_down=False)
check17("inert when QT_WIND_DOWN is unset", "FLAT INVARIANT BREACHED" in _o, False)

# The workflow must actually enforce it, and only after the state is committed.
# (m) 2026-08-11 — the state commit must never be dropped silently again.
#     Run 31525866788 lost every state file it produced to a rebase conflict
#     and still reported success.
check17("push retries instead of `push || true`",
        "git push origin master || true" in _wf17, False)
check17("...rebases with -X theirs (our run's file wins)",
        "git rebase -X theirs origin/master" in _wf17, True)
check17("...retries the push", "for attempt in 1 2 3" in _wf17, True)
check17("...and errors loudly when it cannot",
        "STATE COMMIT LOST" in _wf17, True)
check17("...leaving a sentinel the gate reads",
        _wf17.count(".qt_state_push_failed"), 2)
_i_sent = _wf17.index("touch .qt_state_push_failed")
check17("...checked AFTER the data-branch push (never costs it)",
        _wf17.index("Push dashboard data to orphan data branch") <
        _wf17.index("if [ -f .qt_state_push_failed ]"), True)

check17("workflow has the flat-invariant gate",
        "Flat-invariant gate" in _wf17, True)
_i_gate = _wf17.index("Flat-invariant gate")
check17("...placed AFTER the state commit (clocks safe)",
        _wf17.index("Commit updated state files") < _i_gate, True)
check17("...and AFTER the data-branch push",
        _wf17.index("Push dashboard data to orphan data branch") < _i_gate, True)
# (l) THE DRILL. A real breach needs genuinely unsold positions, which can only
#     be manufactured by buying stock — so the drill injects a synthetic book
#     into the CHECK ONLY. The load-bearing property is that it CANNOT TRADE:
#     the closing loop iterates the real broker book, which the drill never
#     touches. Everything else about it is cosmetic; this is not.
_s, _o, _st = run_wd({}, state={"armed": True, "closed": 24}, drill="DRILL1,DRILL2")
check17("DRILL on an empty book places NO order", _s, [])
check17("...yet still trips the breach", _st.get("breach"), True)
check17("...labelled DRILL in the banner", "BREACHED [DRILL]" in _o, True)
check17("...and stamped in the state file", _st.get("drill"), ["DRILL1", "DRILL2"])
check17("...recording the REAL book separately", _st.get("real_book"), [])
# The gate prints "<last_seen_book> position(s) still open: <still_open>". If
# these disagree the operator reads a self-contradicting page — the live 19:02Z
# drill printed "0 position(s) still open: DRILL1, DRILL2".
check17("...count matches the list it prints",
        _st.get("last_seen_book"), len(_st.get("still_open") or []))
# The drill must never displace a real close: with a real book present, every
# real name is still closed and no drill symbol is ever ordered.
_s, _o, _st = run_wd({"MSFT": 5.0, "BA": 3.0}, state={"armed": True},
                     drill="DRILL1")
check17("DRILL does not suppress real closes", sorted(_s), [("BA", 3), ("MSFT", 5)])
check17("...and never orders the drill symbol",
        any(t == "DRILL1" for t, _ in _s), False)
# Inert unless a human types into the dispatch form.
_s, _o, _st = run_wd({}, state={"armed": True, "closed": 24}, drill="")
check17("DRILL inert when unset", _st.get("breach"), False)

# (n) 2026-08-12 — THE SHORT DEADLOCK. HON went short; close_long refuses
#     shorts by design (d, above), while the invariant counted every position
#     regardless of side. The gate therefore demanded a state the closer was
#     structurally incapable of producing, and five consecutive runs failed on
#     a book no scheduled run could ever clear. Worse, a permanently red gate
#     cannot signal a NEW long re-entry — the one thing it exists to catch.
#     Longs and shorts are now separate states with separate remedies.
_ARMED17 = {"armed": True, "closed": 24}

_s, _o, _st = run_wd({"HON": -13.0}, state=_ARMED17)
check17("short-only book is NOT a long breach", _st.get("breach"), False)
check17("...but IS flagged as a short book", _st.get("short_breach"), True)
check17("...naming the manual cover remedy", "sleeve=short-cover" in _o, True)
check17("...and still submits no order for it", _s, [])
check17("...recorded separately from longs", _st.get("open_shorts"), ["HON"])
check17("...and never reported flat", _st.get("flat"), False)

# Acknowledged: silenced, but the book is still not flat and says so.
_s, _o, _st = run_wd({"HON": -13.0}, state=_ARMED17, ack="HON")
check17("acked short does not breach", _st.get("short_breach"), False)
check17("...and says it was acknowledged", "ACKNOWLEDGED" in _o, True)
check17("...but is still NOT flat", _st.get("flat"), False)
check17("...and is listed as acked", _st.get("short_acked"), ["HON"])

# 🔑 THE LOAD-BEARING PROPERTY: an acked short must never mask a long re-entry.
_s, _o, _st = run_wd({"HON": -13.0, "MSFT": 5.0}, state=_ARMED17, ack="HON")
check17("acked short does NOT mask a long re-entry", _st.get("breach"), True)
check17("...and the long is still closed", _s, [("MSFT", 5)])
check17("...while the short is still skipped", "skipped short: HON" in _o, True)

# The ack is qty-capped, so the 7/15 doubler's signature still breaks through.
check17("ack is qty-capped: a growing short re-breaches",
        run_wd({"HON": -26.0}, state=_ARMED17, ack="HON:13")[2].get("short_breach"),
        True)
check17("...within the cap it stays acked",
        run_wd({"HON": -13.0}, state=_ARMED17, ack="HON:13")[2].get("short_breach"),
        False)
check17("...and an ack never transfers to another symbol",
        run_wd({"HON": -13.0}, state=_ARMED17, ack="CTAS")[2].get("short_breach"),
        True)

# A genuinely flat book stays flat, acks or not — no false "not flat".
check17("empty book is flat", run_wd({}, state=_ARMED17)[2].get("flat"), True)

# An UNARMED run holding a short must not claim anyone acknowledged it. The
# 7/15 short book was unarmed and nobody had decided anything; a log line
# asserting a human decision that was never made is worse than no line.
_s, _o, _st = run_wd({"CTAS": -160.0, "MSFT": 5.0}, state=None)
check17("unarmed short is never called ACKNOWLEDGED", "ACKNOWLEDGED" in _o, False)
check17("...and is still reported as skipped", "skipped short: CTAS" in _o, True)

# The gate must act on the short side, and evaluate the long side FIRST so an
# ack can never short-circuit it.
check17("gate reads short_breach", "short_breach" in _wf17, True)
check17("...names the cover remedy", "sleeve=short-cover" in _wf17, True)
check17("...evaluates the long breach independently of the ack",
        _wf17.index("if breach:") < _wf17.index("if short_breach:"), True)
check17("...and no longer exits before the short check",
        "sys.exit(1)" in _wf17[_wf17.index("if breach:"):
                               _wf17.index("if short_breach:")], False)
check17("...refusing to print 'flat' while anything is open",
        "if rc == 0 and not longs and not shorts:" in _wf17, True)
check17("workflow wires the drill input, defaulting empty",
        "QT_WIND_DOWN_DRILL:    ${{ inputs.wind_down_drill }}" in _wf17
        and "wind_down_drill:" in _wf17, True)
check17("...and the gate labels a drill failure as such",
        "DRILL — synthetic, no order was ever placed" in _wf17, True)

check17("...it is the LAST step in the trade job",
        _wf17.index("notify_failure:") > _i_gate
        and "- name:" not in _wf17[_i_gate:_wf17.index("notify_failure:")]
            .split("Flat-invariant gate", 1)[1], True)

# ── 18. rank-IC series is append-only (2026-08-11) ──────────────────────────
# The analyzer is a full overwrite and written rows moved: 2026-07-15 went
# 278,0.0959 -> 279,0.0955 on the 8/10 run, and cross_sectional_ls.csv had all
# 13 rows rewritten. rank_ic_v2.csv — the S4 input due ~9/24 — is written by
# this same script, so the decision-day read has to be an accumulated ledger,
# not a recompute. First write wins.
def check18(name, got, want):
    ok = got == want
    print(f"18. {name:<52} got={str(got):<7} {'PASS' if ok else 'FAIL (want %s)' % want}")
    if not ok:
        fails.append(f"section18 {name}: got={got} want={want}")

_frz = getattr(_ric14, "_freeze_first_write", None)
check18("analyzer exposes the freeze", callable(_frz), True)
if _frz:
    _t18 = tempfile.mkdtemp()
    _p18 = pathlib.Path(_t18) / "rank_ic.csv"

    # No file yet -> the first computation passes through untouched.
    _new = pd.DataFrame([{"date": "2026-08-13", "n": 279, "rank_ic": 0.011}])
    check18("no existing file: passes through", len(_frz(_new, _p18, "t")), 1)
    _frz(_new, _p18, "t").to_csv(_p18, index=False)

    # THE REGRESSION: a recompute that changes a written row must be REFUSED.
    _recomp = pd.DataFrame([{"date": "2026-08-13", "n": 280, "rank_ic": 0.099}])
    _buf18 = io.StringIO()
    with contextlib.redirect_stdout(_buf18):
        _out18 = _frz(_recomp, _p18, "t")
    check18("recomputed row is REFUSED", float(_out18.iloc[0]["rank_ic"]), 0.011)
    check18("...including its n", int(_out18.iloc[0]["n"]), 279)
    check18("...and the drift is reported, not hidden",
            "FROZE" in _buf18.getvalue(), True)

    # New dates still append — freezing must not stall the clock.
    _mixed = pd.DataFrame([{"date": "2026-08-13", "n": 280, "rank_ic": 0.099},
                           {"date": "2026-08-14", "n": 279, "rank_ic": -0.02}])
    _out18 = _frz(_mixed, _p18, "t")
    check18("new dates still append", len(_out18), 2)
    check18("...old row still frozen", float(_out18.iloc[0]["rank_ic"]), 0.011)
    check18("...new row takes the fresh value",
            float(_out18.iloc[1]["rank_ic"]), -0.02)

    # The escape hatch must be explicit and off by default.
    os.environ["QT_RANK_IC_MUTABLE"] = "1"
    _out18 = _frz(_recomp, _p18, "t")
    check18("QT_RANK_IC_MUTABLE=1 restores recompute",
            float(_out18.iloc[0]["rank_ic"]), 0.099)
    os.environ.pop("QT_RANK_IC_MUTABLE", None)
    check18("...and is off when unset",
            float(_frz(_recomp, _p18, "t").iloc[0]["rank_ic"]), 0.011)

    # Both write sites must use it — the L/S file drifted worst (22% on a
    # three-week-old row), and the gate reads the frozen frame, not the
    # throwaway recompute.
    _ric_src = open(RIC, encoding="utf-8").read()
    check18("rank_ic write site is frozen",
            "res = _freeze_first_write(res, OUT_CSV" in _ric_src, True)
    check18("L/S write site is frozen",
            "lsdf = _freeze_first_write(lsdf, LS_CSV" in _ric_src, True)
    check18("no bare full-overwrite left",
            _ric_src.count("to_csv(OUT_CSV") + _ric_src.count("to_csv(LS_CSV"), 2)

print()
if fails:
    print("VALIDATION FAILED:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("ALL VALIDATION CHECKS PASSED")
