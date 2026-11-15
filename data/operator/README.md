# Operator inputs

Two files the operator owns. The agents READ them and never write them.

## `stance.json`

`conservative | balanced | aggressive`. It sets what the daily brief shows
first and which concentration lines ACTUARY flags. **It never changes a
number** — `validate_actuary.py` and `validate_chief_of_staff.py` both prove
that the same facts are produced under every stance.

A message sent from the dashboard's operator channel carries a stance too.
The most recent one from the repository owner overrides this file for the
next brief, and the brief states which source it used.

## `positions.csv`

```
ticker,dollars,as_of
GRPN,4200,2026-11-14
```

What you hold, in dollars. This is the ONLY source of positions: no agent
has a broker connection, and none may be given one.

An empty file is reported as **"no positions declared"** — exposure is
`null` with that reason, never "0% concentrated". A row with a missing
ticker or a non-positive amount is rejected and listed in the analysis
rather than silently counted as $0, because a position valued at zero
disappears from every concentration figure.
