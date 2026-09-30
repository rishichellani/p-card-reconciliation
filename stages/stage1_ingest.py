"""Stage 1 - ingest the raw bank statement, validate every row, route to a cardholder. Deterministic only."""
from __future__ import annotations

import csv
import io
import logging
from collections import Counter
from decimal import Decimal

from pydantic import ValidationError

from schemas.models import (
    EmployeeDirectory,
    RawTransaction,
    RejectedRow,
    RoutedTransaction,
    Stage1Payload,
)
from utils.artifacts import sha256_hex, write_json_artifact
from utils.context import RunContext
from utils.errors import PipelineError
from utils.loaders import load_json_model, read_bytes

log = logging.getLogger(__name__)

REQUIRED_COLUMNS = {"txn_id", "post_date", "card_last4", "merchant_name", "mcc", "amount"}


def _detect_delimiter(text: str) -> str:
    """Comma, semicolon or tab, whichever makes the header line contain the required columns (Excel in some locales exports ';')."""
    first = text.lstrip("\ufeff").splitlines()[0] if text.strip() else ""
    for cand in (",", ";", "\t"):
        cols = {c.strip().lower() for c in next(csv.reader([first], delimiter=cand), [])}
        if REQUIRED_COLUMNS <= cols:
            return cand
    return ","


def _reasons(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc']) or 'row'}: {e['msg']}" for e in exc.errors()]


def run(ctx: RunContext) -> str:
    directory = load_json_model(ctx.data_dir / "employees.json", EmployeeDirectory)
    by_card = directory.by_card()

    csv_path = ctx.data_dir / "transactions.csv"
    raw = read_bytes(csv_path)
    try:
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text), delimiter=_detect_delimiter(text))
        found = [(f or "").strip().lower() for f in (reader.fieldnames or [])]  # header case and stray spaces do not matter
        reader.fieldnames = found
        dupes = sorted(n for n, c in Counter(found).items() if c > 1 and n)
        if dupes:
            raise PipelineError(f"transactions.csv has duplicate column names {dupes}; each column may appear once")
        missing = REQUIRED_COLUMNS - set(found)
        if missing:
            raise PipelineError(f"transactions.csv is missing required columns {sorted(missing)}; found columns: {found}")
        rows = list(enumerate(reader, start=2))  # row 1 is the header
    except UnicodeDecodeError as exc:
        raise PipelineError(f"transactions.csv is not valid UTF-8: {exc}") from exc
    except csv.Error as exc:
        raise PipelineError(f"transactions.csv is malformed: {exc}") from exc
    if not rows:
        raise PipelineError("transactions.csv contains no data rows")

    routed: list[RoutedTransaction] = []
    quarantined: list[RejectedRow] = []
    seen_ids: set[str] = set()

    for row_number, row in rows:
        extra = row.pop(None, None)  # type: ignore[call-overload]  # csv puts overflow cells under key None
        snapshot = {k: v for k, v in row.items()}
        reasons: list[str] = []
        if extra:
            reasons.append(f"row: has {len(extra)} unexpected extra field(s)")
        try:
            txn = RawTransaction.model_validate(row)
        except ValidationError as exc:
            quarantined.append(RejectedRow(row_number=row_number, raw=snapshot, reasons=reasons + _reasons(exc)))
            log.warning("Row %d quarantined: %s", row_number, "; ".join(reasons + _reasons(exc)))
            continue
        if reasons:
            quarantined.append(RejectedRow(row_number=row_number, raw=snapshot, reasons=reasons))
            continue
        if txn.txn_id in seen_ids:
            quarantined.append(
                RejectedRow(row_number=row_number, raw=snapshot, reasons=[f"txn_id: duplicate of an earlier row ({txn.txn_id})"])
            )
            log.warning("Row %d quarantined: duplicate txn_id %s", row_number, txn.txn_id)
            continue
        seen_ids.add(txn.txn_id)

        emp = by_card.get(txn.card_last4)
        if emp is None:
            quarantined.append(
                RejectedRow(row_number=row_number, raw=snapshot, reasons=[f"card_last4: no cardholder for card ending {txn.card_last4}"])
            )
            log.warning("Row %d quarantined: unknown card %s", row_number, txn.card_last4)
            continue
        if not emp.active:
            log.warning("%s: card of inactive employee %s - routed with flag", txn.txn_id, emp.employee_id)
        routed.append(
            RoutedTransaction(
                transaction=txn,
                employee_id=emp.employee_id,
                employee_name=emp.name,
                department=emp.department,
                cost_center=emp.cost_center,
                approver_id=emp.manager_id,
                employee_active=emp.active,
            )
        )

    if not routed:
        why = "; ".join(f"row {q.row_number}: {q.reasons[0][:90]}" for q in quarantined[:3])
        raise PipelineError(f"No valid transactions survived validation ({len(quarantined)} rows rejected). First reasons: {why}")

    dates = [r.transaction.post_date for r in routed]
    payload = Stage1Payload(
        source_file=csv_path.name,
        source_sha256=sha256_hex(raw),
        statement_period_start=min(dates),
        statement_period_end=max(dates),
        control_total_usd=sum((r.transaction.amount for r in routed), Decimal("0.00")),
        routed=routed,
        quarantined=quarantined,
    )
    log.info(
        "Stage 1: %d rows read -> %d routed, %d quarantined, control total $%s",
        len(rows), len(routed), len(quarantined), payload.control_total_usd,
    )
    return write_json_artifact(
        ctx, 1, payload, upstream_sha=payload.source_sha256,
        counts={"rows_read": len(rows), "routed": len(routed), "quarantined": len(quarantined)},
    )
