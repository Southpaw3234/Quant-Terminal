"""Deliver a message to the operator. Stdlib only, network imported lazily.

-----------------------------------------------------------------------------
🔑 WHY THIS FILE EXISTS: THREE WORKFLOWS SHARED ONE BROKEN SNIPPET

OBSERVER's first scheduled run collected its filings, published them, and
then died posting the digest:

    urllib.error.HTTPError: HTTP Error 403: Forbidden

Discord sits behind a CDN that refuses the default `Python-urllib/3.x`
User-Agent. The watchdog workflows in this repository have always posted
successfully -- with curl, which sends its own. The snippet was copied into
three workflows before it had been run once against the real endpoint, so
all three carried the bug. A green validate job is not a delivered message.

Two rules follow, and both are enforced here rather than remembered:

  1. A request always carries a descriptive User-Agent.
  2. 🔑 DELIVERY NEVER RAISES. By the time a message is posted the artifact
     is already published; a webhook outage must not turn a successful run
     red, and it must not be silent either. `post` returns what happened and
     the caller prints it.

Long messages are split on line boundaries, never truncated: the old snippet
cut a digest at 1,900 characters and discarded the rest, which on a busy
filing day was most of it.
"""

from __future__ import annotations

import json

DISCORD_LIMIT = 1900          # the API caps a message at 2000 characters
USER_AGENT = "Quant-Terminal agent (github-actions; no contact configured)"


def chunk(text: str, limit: int = DISCORD_LIMIT) -> list:
    """Split on line boundaries so no chunk exceeds `limit`.

    A single line longer than the limit is hard-split: losing the tail of
    one line is better than refusing to deliver the message at all, and it
    cannot happen silently because the pieces are all sent.
    """
    out, cur = [], ""
    for line in str(text or "").split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur.strip():
        out.append(cur)
    return [c for c in out if c.strip()]


def post(hook: str, text: str, pause: float = 0.7) -> dict:
    """POST `text` to a Discord webhook, in as many messages as it needs.

    -> {"ok": bool, "sent": int, "of": int, "reason": str}. Never raises.
    """
    parts = chunk(text)
    if not (hook or "").strip():
        return {"ok": False, "sent": 0, "of": len(parts),
                "reason": "no webhook configured"}
    if not parts:
        return {"ok": False, "sent": 0, "of": 0, "reason": "nothing to send"}

    import time
    import urllib.error
    import urllib.request

    sent = 0
    for part in parts:
        req = urllib.request.Request(
            hook.strip(),
            # `allowed_mentions` is empty on purpose: a brief quotes issue
            # titles and headlines, which are untrusted text, and an
            # "@everyone" inside one must not ping a channel.
            data=json.dumps({"content": part,
                             "allowed_mentions": {"parse": []}}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status >= 300:
                    return {"ok": False, "sent": sent, "of": len(parts),
                            "reason": f"webhook answered {r.status}"}
        except urllib.error.HTTPError as e:
            return {"ok": False, "sent": sent, "of": len(parts),
                    "reason": f"webhook answered {e.code}"}
        except Exception as e:            # noqa: BLE001 - see rule 2
            return {"ok": False, "sent": sent, "of": len(parts),
                    "reason": type(e).__name__}
        sent += 1
        if sent < len(parts):
            time.sleep(pause)
    return {"ok": True, "sent": sent, "of": len(parts), "reason": "ok"}
