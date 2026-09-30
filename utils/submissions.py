"""Append-only, hash-chained log of employee justification submissions for one batch.

Each line records who submitted what and when, and commits to the previous line's hash, so history cannot be edited
or removed without detection. A later submission for the same transaction supersedes an earlier one but never erases it.
The log closes when Stage 2 snapshots it.
"""
from __future__ import annotations

import json
import os
import threading
from functools import lru_cache
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from schemas.models import ReceiptMetadata, RoutedTransaction
from utils.artifacts import STAGE_FILES, canonical_bytes, sha256_hex
from utils.errors import ArtifactIntegrityError, PipelineError
from utils.safe import strip_controls

LOG_NAME = "submissions.jsonl"
GENESIS = "0" * 64
LOCK = threading.RLock()  # also held by Stage 2 while it snapshots the log


class SubmissionEntry(BaseModel):
    seq: int = Field(ge=1)
    # Who wrote it: a person in the app, or the "Load sample justifications" button (demo text / planted problem).
    source: Literal["employee", "sample_template", "sample_scenario"] = "employee"
    txn_id: str
    employee_id: str
    employee_name: str
    submitted_at: datetime
    business_purpose: str = Field(min_length=1, max_length=1000)
    attendees: list[str] = Field(default_factory=list, max_length=20)
    receipt: ReceiptMetadata
    prev_hash: str
    entry_hash: str


def _entry_hash(data: dict) -> str:
    return sha256_hex(canonical_bytes({k: v for k, v in data.items() if k != "entry_hash"}))


def read_log(run_dir: Path) -> list[SubmissionEntry]:
    path = run_dir / LOG_NAME
    if not path.exists():
        return []
    entries: list[SubmissionEntry] = []
    prev = GENESIS
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            entry = SubmissionEntry.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ArtifactIntegrityError(f"{LOG_NAME} line {n} is unreadable: {exc}") from exc
        if entry.seq != len(entries) + 1 or entry.prev_hash != prev or _entry_hash(raw) != entry.entry_hash:
            raise ArtifactIntegrityError(f"{LOG_NAME} was modified: chain breaks at line {n}")
        prev = entry.entry_hash
        entries.append(entry)
    return entries


@lru_cache(maxsize=8)
def _routed_index_cached(path: str, mtime_ns: int) -> dict[str, str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))["payload"]
    return {r["transaction"]["txn_id"]: r["employee_id"] for r in payload["routed"]}


def _routed_index(run_dir: Path) -> dict[str, str]:
    """txn_id -> employee_id for the batch's validated statement (cached by file mtime)."""
    path = run_dir / STAGE_FILES[1]
    try:
        return _routed_index_cached(str(path), path.stat().st_mtime_ns)
    except FileNotFoundError as exc:
        raise PipelineError("This batch has no validated statement yet.") from exc


def _tail(path: Path) -> tuple[int, str]:
    """(seq, entry_hash) of the last log line, read from the end of the file so a save stays fast as the log grows.

    The whole chain is verified whenever the log is read (every page load and at audit time), so a tampered earlier
    line is still caught; appending never hides it.
    """
    if not path.exists() or path.stat().st_size == 0:
        return 0, GENESIS
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - 65536))
        lines = [ln for ln in fh.read().split(b"\n") if ln.strip()]
    try:
        last = json.loads(lines[-1])
        return int(last["seq"]), str(last["entry_hash"])
    except (json.JSONDecodeError, KeyError, ValueError, IndexError) as exc:
        raise ArtifactIntegrityError(f"{LOG_NAME} ends with an unreadable line") from exc


MAX_ATTENDEES, MAX_ATTENDEE_CHARS, MAX_VENDOR_CHARS, MAX_RECEIPT_TOTAL = 20, 100, 120, Decimal("1000000")


def log_sha256(run_dir: Path) -> str | None:
    path = run_dir / LOG_NAME
    return sha256_hex(path.read_bytes()) if path.exists() else None


def latest_by_txn(entries: list[SubmissionEntry]) -> dict[str, SubmissionEntry]:
    return {e.txn_id: e for e in entries}  # later entries overwrite earlier ones


def submit(
    run_dir: Path,
    routed: RoutedTransaction,
    business_purpose: str,
    attendees: list[str],
    receipt: ReceiptMetadata,
    now: datetime | None = None,
    source: str = "employee",
) -> SubmissionEntry:
    """Record one justification. The cardholder of the transaction is the submitter."""
    purpose = strip_controls(business_purpose, keep="\n\t").strip()
    if not purpose:
        raise PipelineError("Business purpose is required.")
    if len(purpose) > 1000:
        raise PipelineError("Business purpose is limited to 1000 characters.")
    attendees = [strip_controls(a).strip() for a in attendees if strip_controls(a).strip()]
    if len(attendees) > MAX_ATTENDEES:
        raise PipelineError(f"At most {MAX_ATTENDEES} attendees can be listed (you entered {len(attendees)}).")
    if any(len(a) > MAX_ATTENDEE_CHARS for a in attendees):
        raise PipelineError(f"Each attendee entry is limited to {MAX_ATTENDEE_CHARS} characters.")
    vendor = strip_controls(receipt.vendor_name or "").strip()
    if receipt.present and (not vendor or receipt.total is None):
        raise PipelineError("A receipt needs a vendor name and a total.")
    if len(vendor) > MAX_VENDOR_CHARS:
        raise PipelineError(f"Vendor name is limited to {MAX_VENDOR_CHARS} characters.")
    if receipt.total is not None and not (Decimal("0") <= receipt.total <= MAX_RECEIPT_TOTAL):
        raise PipelineError(f"Receipt total must be between 0 and {MAX_RECEIPT_TOTAL:,}.")
    owner = _routed_index(run_dir).get(routed.transaction.txn_id)
    if owner is None:
        raise PipelineError(f"{routed.transaction.txn_id} is not a transaction in this batch.")
    if owner != routed.employee_id:
        raise PipelineError(f"{routed.transaction.txn_id} does not belong to {routed.employee_name}.")
    with LOCK:
        if (run_dir / STAGE_FILES[2]).exists():
            raise PipelineError("Submissions are closed: the audit has already been run for this batch.")
        last_seq, last_hash = _tail(run_dir / LOG_NAME)
        body = {
            "seq": last_seq + 1, "source": source, "txn_id": routed.transaction.txn_id, "employee_id": routed.employee_id,
            "employee_name": routed.employee_name,
            "submitted_at": (now or datetime.now(timezone.utc)).replace(microsecond=0, tzinfo=None).isoformat(),
            "business_purpose": purpose, "attendees": attendees,
            "receipt": ReceiptMetadata(present=receipt.present,
                                       vendor_name=vendor or None if receipt.present else None,
                                       total=receipt.total if receipt.present else None,
                                       receipt_date=receipt.receipt_date if receipt.present else None).model_dump(mode="json"),
            "prev_hash": last_hash,
        }
        body["entry_hash"] = _entry_hash(body)
        entry = SubmissionEntry.model_validate(body)
        with (run_dir / LOG_NAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return entry
