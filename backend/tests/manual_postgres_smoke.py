"""Exercise disposable PostgreSQL setup and a generated test without GitHub or OpenAI."""

import asyncio
import subprocess
from uuid import uuid4

from sqlalchemy import delete

from app.artifacts import run_dir
from app.db import init_db, session_scope
from app.events import get_events
from app.models import Run, RunEvent
from app.runner import execute_tests


async def main() -> None:
    init_db()
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(
            id=run_id, repository="local/postgres-smoke", pr_number=1,
            installation_id=0,
            instruction="The generated test must query a disposable PostgreSQL database.",
        ))
    source = run_dir(run_id) / "source"
    source.mkdir()
    (source / "setup_database.py").write_text(
        "import os\nimport psycopg\n"
        "with psycopg.connect(os.environ['DATABASE_URL']) as connection:\n"
        "    connection.execute('CREATE TABLE example (value integer)')\n"
        "    connection.execute('INSERT INTO example (value) VALUES (42)')\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    patch = (
        "diff --git a/tests/test_database.py b/tests/test_database.py\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "--- /dev/null\n"
        "+++ b/tests/test_database.py\n"
        "@@ -0,0 +1,4 @@\n"
        "+import os\n"
        "+import psycopg\n"
        "+with psycopg.connect(os.environ['DATABASE_URL']) as connection:\n"
        "+    assert connection.execute('SELECT value FROM example').fetchone() == (42,)\n"
    )
    context = {
        "source_path": str(source), "selected_frameworks": [],
        "analysis": {
            "install_commands": [
                "python3 -m venv .venv && .venv/bin/pip install 'psycopg[binary]'"
            ],
            "native_test_framework": "Python", "has_native_test_framework": True,
            "app_start_command": "", "app_ready_url": "", "test_environment": [],
            "services": [{
                "kind": "postgres", "evidence_files": ["setup_database.py"],
                "connection_environment_keys": ["DATABASE_URL"],
                "setup_commands": [".venv/bin/python setup_database.py"],
            }],
        },
    }
    result = await execute_tests(run_id, context, [{
        "agent": "builtin", "patch": {"patch": patch, "test_files": ["tests/test_database.py"]},
        "commands": [".venv/bin/python tests/test_database.py"],
    }])
    events = get_events(run_id)
    print({"result": result, "events": [(event["node"], event["status"])
                                       for event in events]})
    if not result["success"]:
        raise SystemExit(1)
    assert any(event["node"] == "postgres_ready" and event["success"] is True
               for event in events)
    with session_scope() as session:
        session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
        session.execute(delete(Run).where(Run.id == run_id))


if __name__ == "__main__":
    asyncio.run(main())
