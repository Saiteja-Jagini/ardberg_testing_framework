"""Exercise an HTTP API Playwright adapter on the private runner network."""

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
            id=run_id, repository="local/playwright-adapter", pr_number=1,
            installation_id=0,
            instruction="The API must return the expected item and status.",
            selected_frameworks=["playwright"],
        ))
    source = run_dir(run_id) / "source"
    source.mkdir()
    (source / "package.json").write_text(
        '{"name":"ardberg-playwright-smoke","private":true,"type":"module"}\n',
        encoding="utf-8",
    )
    (source / "app.js").write_text(
        "import http from 'node:http';\n"
        "http.createServer((request, response) => {\n"
        "  response.setHeader('content-type', 'application/json');\n"
        "  response.end(JSON.stringify(request.url === '/health' "
        "? { ready: true } : { item: 'ready' }));\n"
        "}).listen(3000, '127.0.0.1');\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    patch = (
        "diff --git a/tests/ardberg/playwright/api.spec.ts b/tests/ardberg/playwright/api.spec.ts\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/ardberg/playwright/api.spec.ts\n"
        "@@ -0,0 +1,8 @@\n"
        "+import { test, expect } from '@playwright/test';\n"
        "+\n"
        "+test('API returns the item', async ({ request }) => {\n"
        "+  const response = await request.get('http://127.0.0.1:3000/item');\n"
        "+  expect(response.status()).toBe(200);\n"
        "+  const body = await response.json();\n"
        "+  expect(body.item).toBe('ready');\n"
        "+});\n"
    )
    context = {
        "source_path": str(source), "selected_frameworks": ["playwright"],
        "analysis": {"install_commands": [], "has_native_test_framework": False,
                     "app_start_command": "node app.js",
                     "app_ready_url": "http://127.0.0.1:3000/health",
                     "playwright": {"setup_command": "", "ready_url": ""},
                     "test_environment": []},
    }
    result = await execute_tests(run_id, context, [{
        "agent": "playwright",
        "patch": {"patch": patch, "test_files": ["tests/ardberg/playwright/api.spec.ts"]},
        "commands": ["npx playwright test tests/ardberg/playwright/api.spec.ts"],
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
