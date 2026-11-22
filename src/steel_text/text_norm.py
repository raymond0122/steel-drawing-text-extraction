"""Text normalization and the fragment heuristic used by dedup.

The asterisk is deliberately kept everywhere: part numbers such as WT155*79
use it, and stripping it would turn that label into WT15579.
"""

from __future__ import annotations

import re


def norm_text(s: str) -> str:
    """Uppercase, unify the multiplication sign to X, collapse whitespace."""
    s = (s or "").strip().upper().replace("×", "X")
    return re.sub(r"\s+", " ", s)


def tokens(s: str) -> list[str]:
    """Split into alphanumeric tokens, keeping '*'.

    "SEE NOTE 6" -> ["SEE", "NOTE", "6"];  "WT155*79" -> ["WT155*79"]
    """
    return [t for t in re.split(r"[^A-Z0-9*]+", norm_text(s)) if t]


def digit_tokens(ts: list[str]) -> set[str]:
    return {t for t in ts if t.isdigit()}


def is_fragment(short_text: str, long_text: str) -> bool:
    """Strict check: is short_text a cut-off piece of long_text?

    Plain substring matching is not used here on purpose: it was too
    aggressive and removed legitimate short labels. Instead, short_text must
    share real tokens with long_text and be much shorter than it.
    """
    short, long_ = norm_text(short_text), norm_text(long_text)
    if not short or not long_ or short == long_:
        return False
    ts, tl = tokens(short), tokens(long_)

    # Case 1: shares a number and is a tiny piece of a longer phrase,
    # e.g. "6" or "OTE 6" next to "SEE NOTE 6 FOR DETAILS".
    if digit_tokens(ts) & digit_tokens(tl):
        if len(ts) <= 2 and len(tl) >= 4 and len(short) <= 0.5 * len(long_):
            return True

    # Case 2: all meaningful tokens appear in the longer text and the short
    # text is far shorter, e.g. "SEE" next to "SEE NOTE 6".
    if ts and len(tl) >= 2:
        set_s = {t for t in ts if len(t) >= 3}
        set_l = {t for t in tl if len(t) >= 3}
        if set_s and set_s <= set_l and len(set_s) >= len(ts) - 1:
            if len(short) <= 0.4 * len(long_):
                return True

    return False
