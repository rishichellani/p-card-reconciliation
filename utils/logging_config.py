import logging
import sys
from pathlib import Path


def setup_logging(run_dir: Path, level: str = "INFO") -> None:
    """Console + per-run log file (kept next to the artifacts as part of the audit trail)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    logfile = logging.FileHandler(run_dir / "pipeline.log", encoding="utf-8")
    logfile.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(logfile)
    # httpx logs every request at INFO; keep the audit log readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
