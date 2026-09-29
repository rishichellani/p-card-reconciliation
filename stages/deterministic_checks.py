"""Hard-rule compliance checks: pure functions, no LLM, fully unit-testable.

The LLM may *escalate* the result of these checks but can never downgrade them.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from difflib import SequenceMatcher
import re

from schemas.models import (
    AuditStatus,
    Employee,
    Finding,
    JustifiedTransaction,
    PolicyRules,
    Severity,
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def vendors_match(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    return na in nb or nb in na or SequenceMatcher(None, na, nb).ratio() >= 0.6


def status_from_findings(findings: list[Finding]) -> AuditStatus:
    if any(f.severity is Severity.REJECT for f in findings):
        return AuditStatus.REJECTED
    if any(f.severity is Severity.FLAG for f in findings):
        return AuditStatus.FLAGGED
    return AuditStatus.APPROVED


def run_checks(
    items: list[JustifiedTransaction], employees: dict[str, Employee], rules: PolicyRules
) -> dict[str, list[Finding]]:
    findings: dict[str, list[Finding]] = {i.routed.transaction.txn_id: [] for i in items}

    def add(txn_id: str, code: str, severity: Severity, message: str) -> None:
        findings[txn_id].append(Finding(code=code, severity=severity, message=message))

    ordered = sorted(items, key=lambda i: (i.routed.transaction.post_date, i.routed.transaction.txn_id))

    # ---- per-transaction rules -------------------------------------------------
    for item in ordered:
        txn, routed, just = item.routed.transaction, item.routed, item.justification
        emp = employees[routed.employee_id]

        if txn.mcc in rules.blocked_mccs:
            add(txn.txn_id, "BLOCKED_MCC", Severity.REJECT, rules.blocked_mccs[txn.mcc])
        if not routed.employee_active:
            add(txn.txn_id, "INACTIVE_CARDHOLDER", Severity.REJECT,
                f"{routed.employee_name} ({routed.employee_id}) is no longer an active employee")
        if txn.amount > emp.single_txn_limit:
            add(txn.txn_id, "OVER_SINGLE_LIMIT", Severity.FLAG,
                f"${txn.amount} exceeds single-transaction limit ${emp.single_txn_limit}")
        missing = just.is_missing()
        if missing:
            if txn.amount > 0:  # refunds need no explanation
                add(txn.txn_id, "MISSING_JUSTIFICATION", Severity.FLAG, "the cardholder has not submitted a justification")
        elif len(just.business_purpose.strip()) < rules.min_justification_chars:
            add(txn.txn_id, "SHORT_JUSTIFICATION", Severity.FLAG,
                f"business purpose is under {rules.min_justification_chars} characters")
        if txn.post_date.weekday() >= 5:
            add(txn.txn_id, "WEEKEND_TXN", Severity.INFO, f"posted on a weekend ({txn.post_date:%A})")

        if txn.amount > rules.receipt_required_over and not missing:  # one clear finding beats two noisy ones
            r = just.receipt
            if not r.present:
                add(txn.txn_id, "MISSING_RECEIPT", Severity.FLAG,
                    f"receipt required over ${rules.receipt_required_over}")
            else:
                if r.total is not None:
                    tol = txn.amount * rules.receipt_amount_tolerance_pct / Decimal(100)
                    if abs(r.total - txn.amount) > tol:
                        add(txn.txn_id, "RECEIPT_AMOUNT_MISMATCH", Severity.FLAG,
                            f"receipt total ${r.total} != charged ${txn.amount}")
                if r.vendor_name and not vendors_match(r.vendor_name, txn.merchant_name):
                    add(txn.txn_id, "RECEIPT_VENDOR_MISMATCH", Severity.FLAG,
                        f"receipt vendor '{r.vendor_name}' != merchant '{txn.merchant_name}'")

    # ---- duplicates: same employee + merchant + amount within the window -------
    window = timedelta(days=rules.duplicate_window_days)
    for idx, item in enumerate(ordered):
        t = item.routed.transaction
        for earlier in ordered[:idx]:
            e = earlier.routed.transaction
            if (
                earlier.routed.employee_id == item.routed.employee_id
                and _norm(e.merchant_name) == _norm(t.merchant_name)
                and e.amount == t.amount
                and t.amount > 0
                and t.post_date - e.post_date <= window
            ):
                add(t.txn_id, "POSSIBLE_DUPLICATE", Severity.FLAG, f"same merchant/amount as {e.txn_id}")
                break

    # ---- split transactions: same employee + merchant + day, sum over limit ----
    groups: dict[tuple[str, str, object], list[JustifiedTransaction]] = defaultdict(list)
    for item in ordered:
        t = item.routed.transaction
        if t.amount > 0:
            groups[(item.routed.employee_id, _norm(t.merchant_name), t.post_date)].append(item)
    for (emp_id, _, _), grp in groups.items():
        limit = employees[emp_id].single_txn_limit
        total = sum((g.routed.transaction.amount for g in grp), Decimal("0"))
        if len(grp) > 1 and total > limit and all(g.routed.transaction.amount <= limit for g in grp):
            ids = ", ".join(g.routed.transaction.txn_id for g in grp)
            for g in grp:
                add(g.routed.transaction.txn_id, "POSSIBLE_SPLIT", Severity.FLAG,
                    f"same-day charges ({ids}) total ${total}, above single limit ${limit}")

    # ---- monthly limit: flag the charge that crosses it and every one after ----
    running: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for item in ordered:
        t = item.routed.transaction
        key = (item.routed.employee_id, f"{t.post_date:%Y-%m}")
        running[key] += t.amount
        limit = employees[item.routed.employee_id].monthly_limit
        if running[key] > limit:
            add(t.txn_id, "OVER_MONTHLY_LIMIT", Severity.FLAG,
                f"month-to-date ${running[key]} exceeds monthly limit ${limit}")

    return findings
