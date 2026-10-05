"""Verify a human finding refreshes the durable report path without external calls."""

import asyncio
from uuid import uuid4

from sqlalchemy import delete
from temporalio.client import Client
from temporalio.worker import Worker

import app.workflow as orchestration
from app.artifacts import artifact_path
from app.db import init_db, session_scope
from app.events import emit, get_events, get_run, set_run
from app.models import Run, RunEvent


async def main():
    init_db()
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(id=run_id, repository="local/manual-report", pr_number=1,
                        installation_id=0, status="completed",
                        instruction="Record the observed result of a manual feature check."))
    emit(run_id, "manual", "failed", agent="reviewer", node="observation",
         success=False, detail={"verdict": "failed", "actual": "Form did not submit"})
    published = []

    async def fake_generate(_run_id, _outcome):
        assert any(event["agent"] == "reviewer" for event in get_events(_run_id))
        set_run(_run_id, report="Reviewer found a failed form submission.")
        return "Reviewer found a failed form submission."

    async def fake_publish(_run_id, markdown, success):
        published.append((markdown, success))

    orchestration.generate_report = fake_generate
    orchestration.publish_report = fake_publish
    client = await Client.connect("localhost:7233")
    queue = "ardberg-manual-report-smoke-" + run_id
    worker = Worker(client, task_queue=queue,
                    workflows=[orchestration.ManualReportWorkflow],
                    activities=[orchestration.report_activity,
                                orchestration.publish_activity,
                                orchestration.status_activity])
    try:
        async with worker:
            await client.execute_workflow(
                orchestration.ManualReportWorkflow.run,
                args=[run_id, {"success": False, "manual_review": {"verdicts": ["failed"]}}],
                id="ardberg-manual-report-smoke-" + run_id, task_queue=queue,
            )
        assert published == [("Reviewer found a failed form submission.", False)]
        assert get_run(run_id)["status"] == "failed"
        assert artifact_path(run_id, "outcome.json").is_file()
        print({"report_refresh": "passed", "status": get_run(run_id)["status"]})
    finally:
        with session_scope() as session:
            session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
            session.execute(delete(Run).where(Run.id == run_id))


if __name__ == "__main__":
    asyncio.run(main())
