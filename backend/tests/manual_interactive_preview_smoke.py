"""Exercise a real loopback Docker preview without GitHub or model calls."""

import asyncio
from urllib.request import urlopen
from uuid import uuid4

from sqlalchemy import delete

from app.artifacts import run_dir
from app.db import init_db, session_scope
from app.interactive_preview import launch_preview, preview_record, stop_preview
from app.models import InteractivePreview, InteractivePreviewOptions, Run, RunEvent


async def main():
    init_db()
    run_id = str(uuid4())
    preview_id = str(uuid4())
    source = run_dir(run_id) / "source"
    source.mkdir()
    (source / "index.html").write_text("pr-preview-ok", encoding="utf-8")
    context = {
        "source_path": str(source),
        "analysis": {"install_commands": [], "test_environment": [], "services": [{
            "kind": "postgres", "connection_environment_keys": ["DATABASE_URL"],
            "setup_commands": [],
        }],
                     "app_start_command": "python3 -m http.server 8765 --bind 0.0.0.0",
                     "app_ready_url": "http://127.0.0.1:8765/index.html"},
    }
    with session_scope() as session:
        session.add(Run(id=run_id, repository="local/preview-smoke", pr_number=1,
                        installation_id=0, instruction="Serve a pinned HTML file for human review.",
                        context=context))
        session.flush()
        session.add(InteractivePreview(id=preview_id, run_id=run_id,
                                       command=context["analysis"]["app_start_command"],
                                       port=8765, ready_path="/index.html"))
        session.flush()
        session.add(InteractivePreviewOptions(
            preview_id=preview_id, environment={"APPLICATION_MODE": "preview"},
            setup_commands=['test "$APPLICATION_MODE" = preview'],
        ))
    try:
        result = await launch_preview(preview_id)
        assert result["success"], result
        assert preview_record(preview_id)["status"] == "ready"
        with urlopen(result["url"] + "/index.html", timeout=5) as response:
            assert response.read() == b"pr-preview-ok"
        await stop_preview(preview_id)
        assert preview_record(preview_id)["status"] == "stopped"
        print({"preview": result, "cleanup": "passed"})
    finally:
        if preview_record(preview_id)["status"] not in {"stopped", "failed"}:
            await stop_preview(preview_id)
        with session_scope() as session:
            session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
            session.execute(delete(InteractivePreviewOptions).where(
                InteractivePreviewOptions.preview_id == preview_id))
            session.execute(delete(InteractivePreview).where(InteractivePreview.id == preview_id))
            session.execute(delete(Run).where(Run.id == run_id))


if __name__ == "__main__":
    asyncio.run(main())
