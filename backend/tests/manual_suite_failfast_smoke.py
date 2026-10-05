"""Verify a failed suite leaves a concurrently running sibling to finish."""

import asyncio
import subprocess
from time import monotonic
from uuid import uuid4

from sqlalchemy import delete

from app.artifacts import run_dir
from app.db import init_db, session_scope
from app.events import get_events
from app.models import Run, RunEvent
from app.runner import execute_tests


async def main():
    init_db()
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(
            id=run_id, repository="local/suite-failfast", pr_number=1,
            installation_id=0,
            instruction="A failing suite must leave its concurrently running sibling active.",
        ))
    source = run_dir(run_id) / "source"
    source.mkdir()
    (source / "README.md").write_text("Suite cancellation smoke test\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    context = {
        "source_path": str(source), "selected_frameworks": [],
        "analysis": {"install_commands": [], "has_native_test_framework": True,
                     "app_start_command": "", "app_ready_url": "",
                     "test_environment": []},
    }
    started = monotonic()
    result = await execute_tests(run_id, context, [{
        "agent": "builtin", "patch": {"patch": "", "test_files": []},
        "commands": [
            "python3 -c 'raise AssertionError(\"expected failure\")'",
            "python3 -c 'import time; time.sleep(2); print(\"sibling completed\")'",
        ],
    }, {"agent": "vitest", "patch": {"patch": "", "test_files": []},
        "commands": ["echo invalid-command"]}])
    elapsed = monotonic() - started
    events = get_events(run_id)
    assert not result["success"], result
    assert any(item["node"] == "builtin-1" and item["status"] == "failed"
               for item in events)
    assert any(item["node"] == "builtin-2" and item["status"] == "passed"
               for item in events)
    assert any(item["node"] == "vitest-commands" and item["status"] == "failed"
               for item in events)
    assert result["rejected_commands"][0]["agent"] == "vitest"
    assert any(item["node"] == "collect" and item["success"] is True for item in events)
    print({"elapsed_seconds": round(elapsed, 2), "result": result})
    with session_scope() as session:
        session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
        session.execute(delete(Run).where(Run.id == run_id))


if __name__ == "__main__":
    asyncio.run(main())
