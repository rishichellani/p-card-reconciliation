"""Shared look and feel: design tokens, CSS, and small HTML helpers used by every page."""
from __future__ import annotations

from decimal import Decimal

import streamlit as st

# --------------------------------------------------------------------------- design tokens
# Status is never colour-only: every status carries an icon and a text label. Colours are mixed with the inherited
# text colour (currentColor), so contrast holds in both Streamlit themes.
STATUS = {
    "APPROVED": ("Approved", "#15803d", "check"),
    "FLAGGED": ("Flagged", "#b45309", "alert"),
    "MANUAL_REVIEW": ("Manual review", "#1d4ed8", "eye"),
    "REJECTED": ("Rejected", "#b91c1c", "octagon"),
}
SEVERITY_RANK = {"REJECTED": 0, "MANUAL_REVIEW": 1, "FLAGGED": 2, "APPROVED": 3}
ICONS = {  # Lucide-style stroke icons
    "check": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "octagon": '<path d="M7.86 2h8.28L22 7.86v8.28L16.14 22H7.86L2 16.14V7.86z"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
    "eye": '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
}

CSS = """
<style>
.block-container { max-width: 1280px; padding-top: 2rem; padding-bottom: 3rem; }
.pc-num, .pc-mono { font-variant-numeric: tabular-nums; }
.pc-mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .92em; }
.pc-muted { color: color-mix(in srgb, currentColor 78%, transparent); }
.pc-icon { width: 1.1em; height: 1.1em; flex: none; fill: none; stroke: currentColor; stroke-width: 2;
           stroke-linecap: round; stroke-linejoin: round; }
.pc-badge { display: inline-flex; align-items: center; gap: .35rem; padding: .12rem .6rem; border-radius: 999px;
            border: 1px solid var(--c); background: color-mix(in srgb, var(--c) 14%, transparent);
            color: color-mix(in srgb, var(--c) 55%, currentColor); font-weight: 600; font-size: .85rem; white-space: nowrap; }
.pc-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: .75rem; margin: .25rem 0 1rem; }
.pc-card { border: 1px solid color-mix(in srgb, currentColor 18%, transparent); border-left: 4px solid var(--c);
           border-radius: 10px; padding: .8rem .95rem; background: color-mix(in srgb, currentColor 4%, transparent); }
.pc-card .l { display: flex; align-items: center; gap: .4rem; font-weight: 600; color: color-mix(in srgb, var(--c) 55%, currentColor); }
.pc-card .n { font-size: 2rem; font-weight: 700; line-height: 1.15; font-variant-numeric: tabular-nums; }
.pc-card .s { font-size: .85rem; color: color-mix(in srgb, currentColor 78%, transparent); font-variant-numeric: tabular-nums; }
.pc-panel { border: 1px solid color-mix(in srgb, currentColor 18%, transparent); border-radius: 10px; padding: .9rem 1rem; }
.pc-panel h3 { margin: 0 0 .6rem; font-size: 1.05rem; }
.pc-bar-row { display: grid; grid-template-columns: 8.5rem 1fr 10.5rem; gap: .7rem; align-items: center; margin: .45rem 0; }
.pc-bar-track { height: 14px; border-radius: 7px; background: color-mix(in srgb, currentColor 10%, transparent); overflow: hidden; }
.pc-bar { height: 100%; border-radius: 7px; background: var(--c); }
.pc-bar-val { text-align: right; font-variant-numeric: tabular-nums; }
.pc-list { list-style: none; padding: 0; margin: 0; }
.pc-list li { display: flex; gap: .5rem; align-items: flex-start; padding: .3rem 0; }
.pc-list .pc-icon { margin-top: .2em; }
.pc-steps { list-style: none; margin: .5rem 0 0; padding: 0 0 0 1.1rem; border-left: 2px solid color-mix(in srgb, currentColor 22%, transparent); }
.pc-steps li { position: relative; padding: 0 0 1.1rem 1.1rem; }
.pc-steps li::before { content: ""; position: absolute; left: -1.62rem; top: .35rem; width: .75rem; height: .75rem; border-radius: 50%;
                       background: var(--c, #1d4ed8); border: 2px solid var(--background-color, Canvas); }
.pc-steps .h { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; font-weight: 600; }
.pc-stage { font-size: .75rem; padding: .05rem .5rem; border-radius: 6px; border: 1px solid color-mix(in srgb, currentColor 35%, transparent); }
.pc-steps .d { margin-top: .2rem; }
.pc-todo { border: 1px dashed color-mix(in srgb, currentColor 35%, transparent); border-radius: 10px; padding: .6rem .9rem; margin: .5rem 0; }
.pc-finding { display: flex; gap: .5rem; align-items: flex-start; margin: .25rem 0; }
@media (prefers-reduced-motion: no-preference) { .pc-bar { transition: width .3s ease-out; } }
@media (max-width: 640px) { .pc-bar-row { grid-template-columns: 1fr; gap: .2rem; } .pc-bar-val { text-align: left; } }
</style>
"""


def icon(name: str) -> str:
    return f'<svg class="pc-icon" viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>'


def badge(status: str) -> str:
    label, color, ic = STATUS[status]
    return f'<span class="pc-badge" style="--c:{color}">{icon(ic)}{label}</span>'


def check_badge(ok: bool) -> str:
    """Pass/Fail for a control check. Audit outcomes (Approved, Rejected...) are for transactions, not for checks."""
    color, ic, label = ("#15803d", "check", "Pass") if ok else ("#b91c1c", "octagon", "Fail")
    return f'<span class="pc-badge" style="--c:{color}">{icon(ic)}{label}</span>'


def money(v) -> str:
    return f"${Decimal(str(v)):,.2f}"


def inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
