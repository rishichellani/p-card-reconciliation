"""Gate for the live LLM option. Offline (mock) auditing is always open; live calls spend free-tier quota."""
from __future__ import annotations

import hmac
import os

import streamlit as st


def live_passcode() -> str:
    return os.environ.get("LIVE_LLM_PASSCODE", "")


def live_allowed() -> bool:
    """Non-rendering check, used as a last guard right before a live call."""
    return not live_passcode() or bool(st.session_state.get("live_ok"))


def live_unlocked(key: str) -> bool:
    """True when live auditing may be used. Otherwise shows a passcode box (unique `key` per place it appears)."""
    code = live_passcode()
    if not code or st.session_state.get("live_ok"):
        return True
    st.caption("Live LLM auditing is locked on this shared demo to protect its free-tier quota. "
               "The offline auditor is open to everyone. Enter the owner's passcode to unlock live mode.")
    with st.form(f"live_gate_{key}", border=False):
        entered = st.text_input("Live LLM passcode", type="password", key=f"live_code_{key}")
        if st.form_submit_button("Unlock live mode"):
            if hmac.compare_digest(entered.encode(), code.encode()):
                st.session_state["live_ok"] = True
                st.rerun()
            st.error("That passcode is not correct.")
    return False
