"""Stage 4 - deterministic ERP journal entry export. No LLM.

Every statement transaction is posted so the P-Card clearing liability ties to the bank statement:
  APPROVED                      Dr expense GL (by MCC)        Cr 2100 P-Card Clearing
  FLAGGED / REJECTED / REVIEW   Dr 1450 Employee Receivable   Cr 2100 P-Card Clearing   (until resolved)
Refunds (negative amounts) reverse the sides.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from decimal import Decimal

from schemas.models import (
    AuditedTransaction,
    AuditStatus,
    JournalLine,
    PolicyRules,
    Stage3Payload,
)
from utils.artifacts import CSV_MANIFEST, STAGE_FILES, canonical_bytes, read_json_artifact, sha256_hex, write_immutable
from utils.context import RunContext
from utils.errors import PipelineError
from utils.loaders import load_json_model
from utils.safe import neutralize

log = logging.getLogger(__name__)

CSV_COLUMNS = [
    "journal_id", "line_no", "posting_date", "gl_account", "gl_account_name", "cost_center",
    "debit", "credit", "currency", "description", "txn_id", "employee_id", "audit_status",
]


def build_lines(items: list[AuditedTransaction], rules: PolicyRules, journal_id: str) -> list[JournalLine]:
    lines: list[JournalLine] = []

    def emit(a: AuditedTransaction, account: str, debit: Decimal, credit: Decimal) -> None:
        t, r = a.justified.routed.transaction, a.justified.routed
        lines.append(JournalLine(
            journal_id=journal_id, line_no=len(lines) + 1, posting_date=t.post_date, gl_account=account,
            gl_account_name=rules.gl_accounts.get(account, "UNKNOWN ACCOUNT"), cost_center=r.cost_center,
            debit=debit, credit=credit, description=f"{t.merchant_name} | {t.txn_id}"[:80],
            txn_id=a.txn_id, employee_id=r.employee_id, audit_status=a.final_status,
        ))

    for a in sorted(items, key=lambda x: x.txn_id):
        amount = a.justified.routed.transaction.amount
        expense = a.gl_account if a.final_status is AuditStatus.APPROVED else rules.suspense_account
        if expense not in rules.gl_accounts:
            raise PipelineError(f"{a.txn_id}: GL account {expense} is not in the chart of accounts")
        mag = abs(amount)
        if amount >= 0:
            emit(a, expense, mag, Decimal("0.00"))
            emit(a, rules.liability_account, Decimal("0.00"), mag)
        else:
            emit(a, rules.liability_account, mag, Decimal("0.00"))
            emit(a, expense, Decimal("0.00"), mag)
    return lines


def validate_journal(lines: list[JournalLine], control_total: Decimal, rules: PolicyRules, expected_txns: int) -> None:
    debits = sum((l.debit for l in lines), Decimal("0"))
    credits = sum((l.credit for l in lines), Decimal("0"))
    if debits != credits:
        raise PipelineError(f"Journal is out of balance: debits {debits} != credits {credits}")
    liability = sum((l.credit - l.debit for l in lines if l.gl_account == rules.liability_account), Decimal("0"))
    if liability != control_total:
        raise PipelineError(f"Clearing liability {liability} does not tie to statement control total {control_total}")
    if len({l.txn_id for l in lines}) != expected_txns:
        raise PipelineError("Journal does not cover every audited transaction")


def run(ctx: RunContext) -> str:
    meta3, s3, s3_sha = read_json_artifact(ctx, 3, Stage3Payload)
    rules = load_json_model(ctx.data_dir / "policy_rules.json", PolicyRules)

    end = max(a.justified.routed.transaction.post_date for a in s3.items)
    journal_id = f"PCARD-{end:%Y%m}-{ctx.run_id[-6:]}"
    lines = build_lines(s3.items, rules, journal_id)
    validate_journal(lines, s3.control_total_usd, rules, expected_txns=len(s3.items))

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for l in lines:
        row = l.model_dump(mode="json")
        row["debit"], row["credit"] = f"{l.debit:.2f}", f"{l.credit:.2f}"
        row["description"] = neutralize(row["description"])  # merchant text: never let a spreadsheet run it as a formula
        writer.writerow({k: row[k] for k in CSV_COLUMNS})
    data = buf.getvalue().encode("utf-8")

    write_immutable(ctx.run_dir / STAGE_FILES[4], data)
    manifest = {
        "run_id": ctx.run_id, "journal_id": journal_id, "csv_sha256": sha256_hex(data),
        "upstream_sha256": s3_sha, "line_count": len(lines),
        "total_debits": str(sum((l.debit for l in lines), Decimal("0"))),
        "control_total_usd": str(s3.control_total_usd), "auditor": s3.auditor,
    }
    manifest["manifest_sha256"] = sha256_hex(canonical_bytes(manifest))
    write_immutable(ctx.run_dir / CSV_MANIFEST, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    log.info("Stage 4: journal %s, %d lines, gross debits = gross credits = $%s; net clearing liability ties to control total $%s (purchases minus refunds)",
             journal_id, len(lines), manifest["total_debits"], s3.control_total_usd)
    return manifest["csv_sha256"]
