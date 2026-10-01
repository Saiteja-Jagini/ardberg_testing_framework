"""Exercise the no-native-framework Vitest fallback in a disposable container."""

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
            id=run_id, repository="local/no-native", pr_number=1, installation_id=0,
            instruction="Adding two numbers must return their sum.",
            selected_frameworks=["vitest"],
        ))
    source = run_dir(run_id) / "source"
    source.mkdir()
    (source / "package.json").write_text(
        '{"name":"ardberg-fallback-smoke","private":true,"type":"module"}\n',
        encoding="utf-8",
    )
    (source / "sum.js").write_text(
        "export const sum = (a, b) => a + b;\n", encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    patch = (
        "diff --git a/tests/ardberg/vitest/sum.test.js b/tests/ardberg/vitest/sum.test.js\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/ardberg/vitest/sum.test.js\n"
        "@@ -0,0 +1,6 @@\n"
        "+import { expect, it } from 'vitest';\n"
        "+import { sum } from '../../../sum.js';\n"
        "+\n"
        "+it('adds two numbers', () => {\n"
        "+  expect(sum(2, 3)).toBe(5);\n"
        "+});\n"
    )
    context = {
        "source_path": str(source), "selected_frameworks": ["vitest"],
        "analysis": {"install_commands": [], "has_native_test_framework": False,
                     "app_start_command": "", "app_ready_url": "",
                     "test_environment": []},
    }
    result = await execute_tests(run_id, context, [{
        "agent": "vitest",
        "patch": {"patch": patch, "test_files": ["tests/ardberg/vitest/sum.test.js"]},
        "commands": ["npx vitest run tests/ardberg/vitest/sum.test.js"],
    }])
    assert result["success"], result
    assert result["suites"][0]["structured_result"]["passed"] == 1, result
    assert any(item["node"] == "collect" and item["success"] is True
               for item in get_events(run_id))
    print(result)
    with session_scope() as session:
        session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
        session.execute(delete(Run).where(Run.id == run_id))


if __name__ == "__main__":
    asyncio.run(main())
