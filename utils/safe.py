"""Small helpers that keep untrusted text from doing harm downstream (spreadsheets, XML, prompts)."""
from __future__ import annotations

import unicodedata

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def strip_controls(text: str, keep: str = "") -> str:
    """Remove control characters (NUL, bell, escape, ...). Characters listed in ``keep`` survive."""
    return "".join(ch for ch in text if ch in keep or unicodedata.category(ch) != "Cc")


def neutralize(value):
    """Make a cell value safe to open in a spreadsheet: strip control characters and defuse formula prefixes.

    A leading apostrophe is the standard defence against CSV/formula injection. Non-strings pass through untouched,
    so real numbers (including negatives) are never altered.
    """
    if not isinstance(value, str):
        return value
    value = strip_controls(value, keep="\n")
    return "'" + value if value.startswith(_FORMULA_START) else value
