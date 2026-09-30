"""Immutable, hash-chained stage artifacts.

Each JSON artifact is an envelope ``{"meta": {...}, "payload": {...}}``. ``meta`` records the SHA-256 of the
canonical payload and of the *upstream* stage's payload, so any later edit to a persisted file (or a swapped
upstream file) is detected on read. Files are written exactly once and marked read-only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from utils.context import RunContext
from utils.errors import ArtifactIntegrityError, PipelineError

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1.1"
STAGE_FILES = {
    1: "01_raw_statement.json",
    2: "02_user_justified.json",
    3: "03_compliance_audited.json",
    4: "04_erp_journal_entry.csv",
}
CSV_MANIFEST = "04_erp_journal_entry.csv.manifest.json"

T = TypeVar("T", bound=BaseModel)


def canonical_bytes(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_immutable(path: Path, data: bytes) -> None:
    """Write-once: refuse to replace an existing artifact, then mark it read-only."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    try:
        os.link(tmp, path)  # atomic and fails if `path` exists
    except FileExistsError as exc:
        raise PipelineError(
            f"{path.name} already exists in this run and artifacts are immutable. Start a new run instead."
        ) from exc
    finally:
        tmp.unlink(missing_ok=True)
    path.chmod(0o444)


def write_json_artifact(
    ctx: RunContext, stage: int, payload: BaseModel, upstream_sha: str | None, counts: dict[str, int]
) -> str:
    payload_dict = payload.model_dump(mode="json")
    payload_sha = sha256_hex(canonical_bytes(payload_dict))
    meta = {
        "run_id": ctx.run_id,
        "stage": stage,
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "upstream_sha256": upstream_sha,
        "payload_sha256": payload_sha,
        "counts": counts,
    }
    meta["meta_sha256"] = sha256_hex(canonical_bytes(meta))  # so timestamps and counts cannot be edited unnoticed either
    envelope = {"meta": meta, "payload": payload_dict}
    path = ctx.run_dir / STAGE_FILES[stage]
    write_immutable(path, (json.dumps(envelope, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    log.info("Wrote %s (payload sha256=%s...)", path.name, payload_sha[:12])
    return payload_sha


def _load_envelope(path: Path) -> tuple[dict, dict, str]:
    if not path.exists():
        raise PipelineError(f"Missing artifact {path.name}; run the earlier stages first.")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        meta, payload = envelope["meta"], envelope["payload"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ArtifactIntegrityError(f"{path.name} is not a valid artifact envelope: {exc}") from exc
    if meta.get("schema_version") not in (None, "1.0") and meta.get("meta_sha256") is None:
        raise ArtifactIntegrityError(f"{path.name} is missing its metadata hash")
    if meta.get("meta_sha256") is not None:  # schema 1.0 artifacts predate this field and are still accepted
        body = {k: v for k, v in meta.items() if k != "meta_sha256"}
        if sha256_hex(canonical_bytes(body)) != meta["meta_sha256"]:
            raise ArtifactIntegrityError(f"{path.name} metadata (run id, timestamps, counts) has been modified")
    actual = sha256_hex(canonical_bytes(payload))
    if actual != meta.get("payload_sha256"):
        raise ArtifactIntegrityError(
            f"{path.name} has been modified: recorded sha256 {str(meta.get('payload_sha256'))[:12]}... "
            f"!= actual {actual[:12]}..."
        )
    return meta, payload, actual


def read_json_artifact(ctx: RunContext, stage: int, model: type[T]) -> tuple[dict, T, str]:
    path = ctx.run_dir / STAGE_FILES[stage]
    meta, payload, actual = _load_envelope(path)
    try:
        return meta, model.model_validate(payload), actual
    except ValidationError as exc:
        raise ArtifactIntegrityError(f"{path.name} does not match its schema: {exc}") from exc


def verify_run(run_dir: Path, allow_partial: bool = False) -> list[str]:
    """Re-verify every hash and the upstream chain of a finished run. Returns a list of problems (empty = clean)."""
    problems: list[str] = []
    shas: dict[int, str] = {}
    for stage in (1, 2, 3):
        if allow_partial and not (run_dir / STAGE_FILES[stage]).exists():
            continue  # batch still collecting justifications: later stages legitimately do not exist yet
        try:
            meta, _, actual = _load_envelope(run_dir / STAGE_FILES[stage])
        except PipelineError as exc:
            problems.append(str(exc))
            continue
        shas[stage] = actual
        if stage > 1 and shas.get(stage - 1) and meta.get("upstream_sha256") != shas[stage - 1]:
            problems.append(f"{STAGE_FILES[stage]}: upstream hash does not match {STAGE_FILES[stage - 1]}")
    log_path = run_dir / "submissions.jsonl"
    if log_path.exists():
        from utils.submissions import read_log  # local import: avoids a cycle at module load
        try:
            read_log(run_dir)
        except PipelineError as exc:
            problems.append(str(exc))
        try:
            committed = _load_envelope(run_dir / STAGE_FILES[2])[1].get("submissions_log_sha256")
            if committed and committed != sha256_hex(log_path.read_bytes()):
                problems.append("submissions.jsonl changed after the audit snapshot was taken")
        except PipelineError:
            pass  # stage 2 not written yet: batch is still collecting submissions
    csv_path, manifest_path = run_dir / STAGE_FILES[4], run_dir / CSV_MANIFEST
    if csv_path.exists() and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
        if manifest.get("manifest_sha256") and sha256_hex(canonical_bytes(body)) != manifest["manifest_sha256"]:
            problems.append(f"{manifest_path.name} has been modified")
        if sha256_hex(csv_path.read_bytes()) != manifest.get("csv_sha256"):
            problems.append(f"{csv_path.name} has been modified after export")
        if shas.get(3) and manifest.get("upstream_sha256") != shas[3]:
            problems.append(f"{csv_path.name}: upstream hash does not match {STAGE_FILES[3]}")
    elif csv_path.exists() or manifest_path.exists():
        problems.append("ERP CSV and its manifest must exist together")
    return problems
