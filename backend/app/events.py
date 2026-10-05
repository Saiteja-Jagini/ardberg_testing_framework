from datetime import datetime, timezone
from sqlalchemy import select
from .db import session_scope
from .models import Run, RunEvent


def emit(run_id: str, stage: str, status: str, *, agent: str | None = None,
         node: str | None = None, success: bool | None = None,
         detail: dict | None = None) -> int:
    with session_scope() as session:
        record = RunEvent(
            run_id=run_id, stage=stage, agent=agent, node=node,
            status=status, success=success, detail=detail or {},
        )
        session.add(record)
        run = session.get(Run, run_id)
        if run is not None:
            run.stage = stage
            run.updated_at = datetime.now(timezone.utc)
        session.flush()
        return record.id


def set_run(run_id: str, **fields):
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise LookupError(f"Unknown run {run_id}")
        for key, value in fields.items():
            setattr(run, key, value)
        run.updated_at = datetime.now(timezone.utc)


def get_run(run_id: str) -> dict:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise LookupError(f"Unknown run {run_id}")
        return {
            "id": run.id, "repository": run.repository, "pr_number": run.pr_number,
            "title": run.title, "head_sha": run.head_sha, "base_sha": run.base_sha,
            "instruction": run.instruction, "selected_frameworks": run.selected_frameworks,
            "status": run.status, "stage": run.stage, "context": run.context,
            "error": run.error, "report": run.report,
            "created_at": run.created_at.isoformat(), "updated_at": run.updated_at.isoformat(),
        }


def get_events(run_id: str, after: int = 0) -> list[dict]:
    with session_scope() as session:
        records = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.id > after).order_by(RunEvent.id)
        ).all()
        return [{
            "id": item.id, "stage": item.stage, "agent": item.agent,
            "node": item.node, "status": item.status, "success": item.success,
            "detail": item.detail, "created_at": item.created_at.isoformat(),
        } for item in records]
