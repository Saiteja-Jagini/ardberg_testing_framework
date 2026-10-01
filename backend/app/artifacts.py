from pathlib import Path
from .config import settings


def run_dir(run_id: str) -> Path:
    if not run_id or not all(char.isalnum() or char == "-" for char in run_id):
        raise ValueError("Invalid run ID")
    result = settings.data_dir / "runs" / run_id
    result.mkdir(parents=True, exist_ok=True)
    return result


def artifact_path(run_id: str, relative: str) -> Path:
    root = run_dir(run_id).resolve()
    target = (root / relative).resolve()
    if root not in target.parents or target == root:
        raise ValueError("Invalid artifact path")
    return target


def write_artifact(run_id: str, relative: str, content: str | bytes) -> str:
    target = artifact_path(run_id, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    return relative.replace("\\", "/")
