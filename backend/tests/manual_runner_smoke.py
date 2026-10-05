"""Run a local container smoke test without GitHub or a model key."""

import asyncio
import subprocess
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
            id=run_id, repository="local/smoke", pr_number=1, installation_id=0,
            instruction="The generated arithmetic test must execute inside the container.",
        ))
    source = run_dir(run_id) / "source"
    source.mkdir()
    (source / "README.md").write_text("Local runner smoke test\n", encoding="utf-8")
    (source / "security_check.py").write_text(
        "from pathlib import Path\nassert 'Local runner smoke test' in Path('README.md').read_text()\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    patch = (
        "diff --git a/tests/test_arithmetic.py b/tests/test_arithmetic.py\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "--- /dev/null\n"
        "+++ b/tests/test_arithmetic.py\n"
        "@@ -0,0 +1 @@\n"
        "+assert 2 + 2 == 4\n"
    )
    context = {
        "source_path": str(source), "selected_frameworks": [],
        "analysis": {"install_commands": [], "native_test_framework": "Python",
                     "has_native_test_framework": True,
                     "app_start_command": "", "app_ready_url": "",
                     "security_check_commands": ["python3 security_check.py"]},
    }
    result = await execute_tests(run_id, context, [{
        "agent": "builtin",
        "patch": {"patch": patch, "test_files": ["tests/test_arithmetic.py"]},
        "commands": ["python3 tests/test_arithmetic.py"],
    }])
    print({"result": result, "events": [(event["node"], event["status"])
                                       for event in get_events(run_id)]})
    if not result["success"]:
        raise SystemExit(1)
    assert result["security_scans"][0]["success"] is True
    with session_scope() as session:
        session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
        session.execute(delete(Run).where(Run.id == run_id))


if __name__ == "__main__":
    asyncio.run(main())
