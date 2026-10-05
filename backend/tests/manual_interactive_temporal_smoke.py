"""Verify Temporal owns preview startup, stop signal, and Docker cleanup."""

import asyncio
from urllib.request import urlopen
from uuid import uuid4

from sqlalchemy import delete
from temporalio.client import Client
from temporalio.worker import Worker

from app.artifacts import run_dir
from app.db import init_db, session_scope
from app.interactive_preview import (
    PreviewWorkflow, launch_preview_activity, preview_health_activity, preview_record,
    stop_preview, stop_preview_activity,
)
from app.models import InteractivePreview, Run, RunEvent


async def main():
    init_db()
    run_id, preview_id = str(uuid4()), str(uuid4())
    source = run_dir(run_id) / "source"
    source.mkdir()
    (source / "index.html").write_text("temporal-preview-ok", encoding="utf-8")
    context = {"source_path": str(source), "analysis": {
        "install_commands": [], "test_environment": [], "services": [],
        "app_start_command": "python3 -m http.server 8766 --bind 0.0.0.0",
        "app_ready_url": "http://127.0.0.1:8766/index.html",
    }}
    with session_scope() as session:
        session.add(Run(id=run_id, repository="local/temporal-preview", pr_number=1,
                        installation_id=0, instruction="Serve a pinned page via Temporal.",
                        context=context))
        session.flush()
        session.add(InteractivePreview(id=preview_id, run_id=run_id,
                                       command=context["analysis"]["app_start_command"],
                                       port=8766, ready_path="/index.html"))
    client = await Client.connect("localhost:7233")
    queue = "ardberg-preview-smoke-" + preview_id
    worker = Worker(client, task_queue=queue, workflows=[PreviewWorkflow],
                    activities=[launch_preview_activity, stop_preview_activity,
                                preview_health_activity])
    try:
        async with worker:
            handle = await client.start_workflow(PreviewWorkflow.run, preview_id,
                                                 id="ardberg-preview-smoke-" + preview_id,
                                                 task_queue=queue)
            for _ in range(40):
                record = preview_record(preview_id)
                if record["status"] in {"ready", "failed"}:
                    break
                await asyncio.sleep(1)
            assert record["status"] == "ready", record
            with urlopen(record["url"] + "/index.html", timeout=5) as response:
                assert response.read() == b"temporal-preview-ok"
            await handle.signal(PreviewWorkflow.stop)
            await asyncio.wait_for(handle.result(), timeout=30)
            assert preview_record(preview_id)["status"] == "stopped"
            print({"preview": record["url"], "temporal_stop": "passed"})
    finally:
        if preview_record(preview_id)["status"] not in {"stopped", "failed"}:
            await stop_preview(preview_id)
        with session_scope() as session:
            session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
            session.execute(delete(InteractivePreview).where(InteractivePreview.id == preview_id))
            session.execute(delete(Run).where(Run.id == run_id))


if __name__ == "__main__":
    asyncio.run(main())
