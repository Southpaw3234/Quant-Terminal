# The pre-registered phrase list

`phrases.json` is LIBRARIAN's pre-registration. It is to a document what the
registry is to a specification.

**A phrase may only be tested against filings published after it was
declared.** `qt.librarian.check_declared_before` refuses otherwise, and
`librarian_diff.py` counts the refusals rather than skipping them, so the
cost of declaring late is visible in the brief instead of invisible.

Why the line sits at the *current* filing rather than the baseline:

* declaring after the **baseline** filing is selecting on information that
  already existed when you declared. You know the phrase was there; you do
  not know whether it will leave. That is legitimate.
* declaring after the **current** filing is selecting on the **outcome**.
  That is the thing this repository exists to prevent.

## Adding a phrase

Append it with today's date and a stated reason, and commit. It becomes
testable against filings published from the next day onward. Nothing makes
it apply backwards, and nothing should: a phrase chosen after a document was
published could have been chosen by reading it.

A phrase with no `why` is refused by CI. A phrase nobody can justify in
advance is a phrase that was chosen later.

## Why the digest matters

Every brief records this file's SHA-256. Editing the list is therefore
visible against briefs already published, rather than quietly replacing the
pre-registration with a better-looking one.

## What a brief is not

It is **not** a risk-factor or MD&A diff. Filing bodies are served from
`www.sec.gov`, which answers 403 to this client, so:

* only declared phrases are tested — language nobody declared is invisible
* section attribution (Item 1A vs Item 7) is **not observed**, reported as
  `null` with a reason rather than guessed
* a phrase absent from a filing that is missing from the full-text index is
  `indeterminate`, never `removed`

That last one is the module's most important guard. Removals are the
headline signal, so a filing missing from the index would otherwise
manufacture a removal for every declared phrase at once.
