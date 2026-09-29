#!/usr/bin/env python3
"""Export a finished run to an Excel workbook and per-sheet CSVs for human review.

    python export_review.py            # latest run
    python export_review.py RUN_ID
Writes to exports/<run_id>/ (outside the immutable output/ run folder).
"""
from __future__ import annotations

import sys
from pathlib import Path

from utils.errors import PipelineError
from utils.export import write_csvs, write_workbook

ROOT = Path(__file__).resolve().parent


def main() -> int:
    runs = sorted((d.name for d in (ROOT / "output").glob("run_*") if (d / "04_erp_journal_entry.csv").exists()), reverse=True)
    run_id = sys.argv[1] if len(sys.argv) > 1 else (runs[0] if runs else None)
    if not run_id:
        print("No completed runs in output/. Run: python run_pipeline.py --mock-llm", file=sys.stderr)
        return 1
    try:
        out = ROOT / "exports" / run_id
        xlsx = write_workbook(ROOT / "output" / run_id, out / f"pcard_review_{run_id}.xlsx")
        csvs = write_csvs(ROOT / "output" / run_id, out / "csv")
    except PipelineError as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    print(f"Workbook: {xlsx}")
    for p in csvs:
        print(f"CSV:      {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
