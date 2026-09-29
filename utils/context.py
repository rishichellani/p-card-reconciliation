from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunContext:
    run_id: str
    run_dir: Path
    data_dir: Path
    mock_llm: bool
    providers: str | None = None  # comma-separated priority override, e.g. "groq,gemini"
    seed: int = 20260929
    justifications: str = "simulate"  # "simulate" (demo) or "portal" (entered by people in the app)
