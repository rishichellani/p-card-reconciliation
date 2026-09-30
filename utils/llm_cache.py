"""Verdicts already obtained from the model for a batch, so an interrupted audit can resume without paying twice.

Each line is bound to the exact prompt (system text plus transaction facts) by a hash, and the stored verdict is re-validated on
load. This file is a convenience cache, not evidence: the hash-chained Stage 3 artifact remains the record of what was decided.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from schemas.models import LLMAuditResult

log = logging.getLogger(__name__)
CACHE_NAME = "llm_cache.jsonl"
_LOCK = threading.Lock()


def load(run_dir: Path) -> dict[str, LLMAuditResult]:
    path = run_dir / CACHE_NAME
    out: dict[str, LLMAuditResult] = {}
    if not path.exists():
        return out
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            out[entry["key"]] = LLMAuditResult.model_validate(entry["result"])
        except (json.JSONDecodeError, KeyError, ValidationError):
            log.warning("Ignoring unreadable line %d of %s", n, CACHE_NAME)
    return out


def append(run_dir: Path, key: str, txn_id: str, result: LLMAuditResult) -> None:
    line = json.dumps({"key": key, "txn_id": txn_id, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "result": result.model_dump(mode="json")}, ensure_ascii=False)
    with _LOCK, (run_dir / CACHE_NAME).open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def count(run_dir: Path) -> int:
    return len(load(run_dir))
