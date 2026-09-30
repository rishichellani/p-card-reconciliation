#!/usr/bin/env python3
"""P-Card reconciliation pipeline: bank statement -> justification -> compliance audit -> ERP journal entry."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from stages import stage1_ingest, stage2_justify, stage3_audit, stage4_erp
from utils.artifacts import verify_run
from utils.context import RunContext
from utils.env import load_dotenv
from utils.errors import LLMAuthError, PipelineError
from utils.logging_config import setup_logging

ROOT = Path(__file__).resolve().parent
STAGES = {1: stage1_ingest.run, 2: stage2_justify.run, 3: stage3_audit.run, 4: stage4_erp.run}
log = logging.getLogger("pipeline")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, default=ROOT / "data")
    p.add_argument("--output-dir", type=Path, default=ROOT / "output")
    p.add_argument("--run-id", help="Resume an existing run (use with --start-stage). Default: new timestamped run.")
    p.add_argument("--start-stage", type=int, choices=STAGES, default=1)
    p.add_argument("--mock-llm", action="store_true", help="Use the offline heuristic auditor instead of a live LLM")
    p.add_argument("--providers", default=None,
                   help="Comma-separated LLM priority order (default: env LLM_PROVIDERS or gemini,groq,openrouter)")
    p.add_argument("--verify", metavar="RUN_ID", help="Only re-verify the hash chain of an existing run and exit")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def main() -> int:
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT.parent / ".env")  # also accept a .env one level up (project root)
    args = parse_args()

    if args.verify:
        if not (args.output_dir / args.verify).is_dir():
            print(f"Run '{args.verify}' was not found in {args.output_dir}", file=sys.stderr)
            return 1
        problems = verify_run(args.output_dir / args.verify)
        for prob in problems:
            print(f"INTEGRITY FAILURE: {prob}", file=sys.stderr)
        print("OK: all artifacts match their recorded hashes" if not problems else "")
        return 1 if problems else 0

    run_id = args.run_id or datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ")
    run_dir = args.output_dir / run_id
    if args.run_id is None and run_dir.exists():
        print(f"Run directory {run_dir} already exists", file=sys.stderr)
        return 2
    setup_logging(run_dir, args.log_level)
    ctx = RunContext(run_id=run_id, run_dir=run_dir, data_dir=args.data_dir, mock_llm=args.mock_llm,
                     providers=args.providers)
    log.info("Run %s | data=%s | auditor=%s", run_id, ctx.data_dir, "mock" if ctx.mock_llm else "free-tier LLM router")

    try:
        for stage in range(args.start_stage, 5):
            log.info("=== Stage %d ===", stage)
            STAGES[stage](ctx)
        problems = verify_run(run_dir)
        if problems:
            raise PipelineError("Post-run integrity check failed: " + "; ".join(problems))
    except LLMAuthError as exc:
        log.error("%s\nSee README 'Setup' for free API keys, or re-run with --mock-llm.", exc)
        return 3
    except PipelineError as exc:
        log.error("Pipeline aborted: %s", exc)
        return 1
    except Exception:  # noqa: BLE001 - last-resort guard so the failure is logged to the audit log
        log.exception("Unexpected failure")
        return 1

    log.info("Run complete. Artifacts in %s", run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
