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
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    relative = relative.replace("\\", "/")
    from .db import session_scope
    from .models import StoredArtifact
    with session_scope() as session:
        session.merge(StoredArtifact(run_id=run_id, path=relative, content=data))
    target.write_bytes(data)
    return relative


def restore_artifact(run_id: str, relative: str) -> Path:
    target = artifact_path(run_id, relative)
    if not target.is_file():
        from .db import session_scope
        from .models import StoredArtifact
        with session_scope() as session:
            saved = session.get(StoredArtifact, (run_id, relative.replace("\\", "/")))
            if saved is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(saved.content)
    return target
