# Thesis ledger

`ledger.jsonl` is append-only and hash-chained. Each entry carries the hash
of the entry before it, so altering any past line breaks every hash after it
and `python thesis_ledger.py verify` reports exactly where.

**Never edit this file.** There is no edit command and no delete command,
and that is not a convention — it is an absence of code to call. A
correction is a new entry that references the old one, so the original claim
survives beside the correction.

If the chain breaks, restore from git history. Do not "fix" it in place:
a repaired chain and an honest one are indistinguishable afterwards, which
is the whole property being protected.

```bash
python thesis_ledger.py open --id grpn-2026q4 --ticker GRPN \
    --claim "trading below net cash with the ad business stabilising" \
    --falsifier "two consecutive quarters of declining active customers" \
    --falsifier "net cash per share falls below the share price" \
    --review 2026-02-15 --stance balanced

python thesis_ledger.py status      # every thesis, with fired falsifiers
python thesis_ledger.py due         # reviews that have come up
python thesis_ledger.py verify      # the chain
```

A thesis cannot be opened without at least one falsifier and a review date.
That refusal is the feature: a thesis with no stated falsifier cannot be
wrong, which means it cannot be right either, and the moment to notice is
while writing it.

When a falsifier fires you either **close** the thesis or record an
**override** saying why it survives. Doing neither is *drift*, and `status`
reports it by name. Overrides are counted beside opens and closes, because
a ledger whose purpose is to catch somebody renegotiating with themselves
has to make that number impossible to look away from.
