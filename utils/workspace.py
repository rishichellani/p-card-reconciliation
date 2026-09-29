"""Batches for the interactive (portal) workflow: create, reopen, back up and restore.

A batch is an ordinary run folder plus ``batch.json`` and an ``inputs/`` snapshot of the exact files the run used,
so every result can be traced back to the inputs that produced it.
"""
from __future__ import annotations

import io
import json
import logging
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from stages import stage1_ingest, stage2_justify, stage3_audit, stage4_erp
from utils.artifacts import CSV_MANIFEST, STAGE_FILES, sha256_hex, verify_run, write_immutable
from utils.context import RunContext
from utils.errors import PipelineError
from utils.submissions import LOG_NAME, read_log

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
SAMPLE_DATA = ROOT / "data"
INPUT_FILES = ("employees.json", "policy_rules.json", "spending_policy.md")
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_RESTORE_BYTES = 50 * 1024 * 1024
STAGE_RUNNERS = {1: stage1_ingest.run, 2: stage2_justify.run, 3: stage3_audit.run, 4: stage4_erp.run}
log = logging.getLogger(__name__)


@contextmanager
def _batch_log(run_dir: Path):
    """Send log output to the batch's pipeline.log for the duration of a stage."""
    handler = logging.FileHandler(run_dir / "pipeline.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        handler.close()


def run_stage(ctx: RunContext, stage: int, **kwargs) -> str:
    with _batch_log(ctx.run_dir):
        return STAGE_RUNNERS[stage](ctx, **kwargs)


def _rmtree(path: Path) -> None:
    for p in path.rglob("*"):
        try:
            p.chmod(0o755 if p.is_dir() else 0o644)
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)


def create_batch(csv_bytes: bytes, label: str = "", mock_llm: bool = True, providers: str | None = None) -> RunContext:
    """Snapshot the inputs into a new run folder and run Stage 1. Removes the folder again if Stage 1 fails."""
    if not csv_bytes.strip():
        raise PipelineError("The statement file is empty.")
    if len(csv_bytes) > MAX_UPLOAD_BYTES:
        raise PipelineError(f"The statement file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    run_id = datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ")
    run_dir = OUTPUT / run_id
    if run_dir.exists():
        raise PipelineError("A batch was created in the same second; please try again.")
    inputs = run_dir / "inputs"
    inputs.mkdir(parents=True)
    try:
        for name in INPUT_FILES:
            shutil.copy(SAMPLE_DATA / name, inputs / name)
        (inputs / "transactions.csv").write_bytes(csv_bytes)
        hashes = {p.name: sha256_hex(p.read_bytes()) for p in sorted(inputs.iterdir())}
        for p in inputs.iterdir():
            p.chmod(0o444)
        meta = {"mode": "portal", "label": label.strip()[:80], "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "input_sha256": hashes}
        write_immutable(run_dir / "batch.json", (json.dumps(meta, indent=2) + "\n").encode())
        ctx = RunContext(run_id=run_id, run_dir=run_dir, data_dir=inputs, mock_llm=mock_llm, providers=providers,
                         justifications="portal")
        run_stage(ctx, 1)
        return ctx
    except Exception:
        _rmtree(run_dir)
        raise


def load_ctx(run_id: str, mock_llm: bool = True, providers: str | None = None) -> RunContext:
    run_dir = OUTPUT / run_id
    if not (run_dir / "batch.json").exists():
        raise PipelineError(f"{run_id} is not an interactive batch.")
    return RunContext(run_id=run_id, run_dir=run_dir, data_dir=run_dir / "inputs", mock_llm=mock_llm,
                      providers=providers, justifications="portal")


def batch_info(run_dir: Path) -> dict:
    meta = json.loads((run_dir / "batch.json").read_text())
    if (run_dir / STAGE_FILES[4]).exists():
        state = "complete"
    elif (run_dir / STAGE_FILES[2]).exists():
        state = "audit incomplete"
    elif (run_dir / STAGE_FILES[1]).exists():
        state = "collecting justifications"
    else:
        state = "not started"
    return {"run_id": run_dir.name, "label": meta.get("label", ""), "created_at": meta.get("created_at", ""), "state": state}


def list_batches() -> list[dict]:
    if not OUTPUT.exists():
        return []
    return sorted((batch_info(d) for d in OUTPUT.iterdir() if (d / "batch.json").exists()),
                  key=lambda b: b["run_id"], reverse=True)


def run_audit(ctx: RunContext, mock_llm: bool, providers: str | None, progress=None) -> None:
    """Close submissions and run Stages 2-4. Stage 2 is skipped if a previous attempt already wrote it."""
    ctx = replace(ctx, mock_llm=mock_llm, providers=providers)
    if not (ctx.run_dir / STAGE_FILES[2]).exists():
        run_stage(ctx, 2)
    if not (ctx.run_dir / STAGE_FILES[3]).exists():
        run_stage(ctx, 3, progress=progress)
    if not (ctx.run_dir / STAGE_FILES[4]).exists():
        run_stage(ctx, 4)
    problems = verify_run(ctx.run_dir)
    if problems:
        raise PipelineError("Integrity check failed: " + "; ".join(problems))


# --------------------------------------------------------------------------- backup / restore
def backup_zip(run_dir: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(run_dir.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                z.write(p, Path(run_dir.name) / p.relative_to(run_dir))
    return buf.getvalue()


def restore_zip(data: bytes) -> str:
    """Restore a backup made by backup_zip. Validates paths, size and integrity before anything is kept."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PipelineError("That is not a valid backup zip.") from exc
    infos = [i for i in z.infolist() if not i.is_dir()]
    if not infos or sum(i.file_size for i in infos) > MAX_RESTORE_BYTES:
        raise PipelineError("The backup is empty or too large.")
    tops = {Path(i.filename).parts[0] for i in infos}
    if len(tops) != 1 or not next(iter(tops)).startswith("run_"):
        raise PipelineError("The backup must contain exactly one run_* folder.")
    run_id = next(iter(tops))
    for i in infos:
        parts = Path(i.filename).parts
        if Path(i.filename).is_absolute() or ".." in parts:
            raise PipelineError("The backup contains unsafe file paths.")
    dest = OUTPUT / run_id
    if dest.exists():
        raise PipelineError(f"{run_id} already exists here; nothing was restored.")
    OUTPUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=OUTPUT) as tmp:
        z.extractall(tmp)
        staged = Path(tmp) / run_id
        if not (staged / "batch.json").exists():
            raise PipelineError("The backup is not an interactive batch (batch.json is missing).")
        problems = verify_run(staged, allow_partial=not (staged / STAGE_FILES[4]).exists())
        if not (staged / STAGE_FILES[1]).exists():
            problems.append("01_raw_statement.json is missing")
        if problems:
            raise PipelineError("The backup failed its integrity check: " + "; ".join(problems))
        shutil.move(str(staged), str(dest))
    for name in ("batch.json", *STAGE_FILES.values(), CSV_MANIFEST):
        if (dest / name).exists():
            (dest / name).chmod(0o444)
    if (dest / "inputs").exists():
        for p in (dest / "inputs").iterdir():
            p.chmod(0o444)
    return run_id
