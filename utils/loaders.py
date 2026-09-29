import json
import logging
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from utils.errors import PipelineError

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


def read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise PipelineError(f"Required input file not found: {path}") from exc
    except OSError as exc:
        raise PipelineError(f"Cannot read {path}: {exc}") from exc


def load_json_model(path: Path, model: type[T]) -> T:
    raw = read_bytes(path)
    try:
        return model.model_validate(json.loads(raw))
    except json.JSONDecodeError as exc:
        raise PipelineError(f"{path.name} is not valid JSON: {exc}") from exc
    except ValidationError as exc:
        raise PipelineError(f"{path.name} failed schema validation:\n{exc}") from exc
