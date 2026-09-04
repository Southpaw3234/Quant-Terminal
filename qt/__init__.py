"""qt — Quant Terminal core, v28 Phase 0.

The replacement for `quant_runner.py` (404KB) and `trading_model_v25.1.ipynb`
(516KB). Not a rewrite of the model — the model is measured and retired — but
of the machinery around it, which is the part that was always worth keeping and
was never testable where it lived.

WHY THIS PACKAGE EXISTS
-----------------------
Thirteen dated model changes across v25 and **not one** was "the model needs
better features". Every incident was measurement integrity: a scorer that
silently died, four distinct duplicate-retrain sources, drive-sync resurrecting
frozen state, an evidence-eating checkout race, signals computed on bars 5-10
sessions stale.

Most of those were only *possible* because a 900KB two-file system had no seams
to test at — the notebook rewrites its own cells at runtime via `[src rewrite]`
patches, and `SECTOR_MAP` lives inside a string literal, so `ast` cannot even
parse it. Phase 0 gives the machinery seams.

SCOPE — Phase 0 ONLY
--------------------
Phase 0: `ledger` and `guards`. Phase 1 adds `measurement` (the only role
permitted to compute a return) and `referee` (owns the pre-registration and can
veto). All are ports or enforcement of existing contracts, not inventions, not inventions: the semantics come from
code that ran live for months. Where this package deliberately differs from v25,
it says so at the call site rather than quietly improving.

⚠️ **NOTHING HERE IS WIRED INTO THE LIVE PIPELINE.** S4 reads obs #30 on
2026-09-28 and the crons retire 09-29. Until then `quant_runner.py` remains the
production path untouched, and this package runs only under its own CI. See
`docs/V28_AGENT_ARCHITECTURE.md` §④.
"""

__version__ = "0.2.0"
__phase__ = 1

from . import guards, ledger, measurement, referee  # noqa: F401

__all__ = ["guards", "ledger", "measurement", "referee"]
