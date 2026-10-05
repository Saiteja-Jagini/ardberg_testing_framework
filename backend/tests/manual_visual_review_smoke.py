"""Exercise PR/base browser screenshots and viewport checks in local Docker."""

import asyncio
import json
import subprocess
from uuid import uuid4

from sqlalchemy import delete

from app.artifacts import artifact_path, run_dir
from app.db import init_db, session_scope
from app.events import get_events
from app.models import Run, RunEvent
from app.runner import execute_tests


async def main() -> None:
    init_db()
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(id=run_id, repository="local/visual-smoke", pr_number=1,
                        installation_id=0, instruction="The updated heading must appear without overflow."))
    root = run_dir(run_id)
    for folder, heading in (("source", "Updated heading"), ("base-source", "Original heading")):
        source = root / folder
        source.mkdir()
        (source / "package.json").write_text(json.dumps({"name": "ardberg-visual-smoke",
                                                   "private": True, "type": "module"}))
        (source / "app.js").write_text(
            "import http from 'node:http';\n"
            "http.createServer((request, response) => {\n"
            " response.setHeader('content-type', 'text/html');\n"
            " response.end('<!doctype html><html><head><title>Review</title></head><body>"
            f"<h1>{heading}</h1></body></html>');\n"
            "}).listen(3000, '127.0.0.1');\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    patch = (
        "diff --git a/tests/ardberg/playwright/page.ardberg.playwright.spec.ts b/tests/ardberg/playwright/page.ardberg.playwright.spec.ts\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/ardberg/playwright/page.ardberg.playwright.spec.ts\n"
        "@@ -0,0 +1,6 @@\n"
        "+import { test, expect } from '@playwright/test';\n"
        "+\n"
        "+test('updated page heading appears', async ({ page }) => {\n"
        "+  await page.goto('http://127.0.0.1:3000/');\n"
        "+  await expect(page.locator('h1')).toHaveText('Updated heading');\n"
        "+});\n"
    )
    context = {"source_path": str(root / "source"), "base_source_path": str(root / "base-source"),
               "impact_map": {"browser_targets": [{"path": "/"}]},
               "analysis": {"install_commands": [], "has_native_test_framework": False,
                            "app_start_command": "node app.js",
                            "app_ready_url": "http://127.0.0.1:3000/",
                            "playwright": {"target": "browser", "setup_command": "", "ready_url": ""},
                            "test_environment": [], "services": [], "security_check_commands": []}}
    result = await execute_tests(run_id, context, [{
        "agent": "playwright",
        "patch": {"patch": patch, "test_files": ["tests/ardberg/playwright/page.ardberg.playwright.spec.ts"]},
        "commands": ["npx playwright test tests/ardberg/playwright/page.ardberg.playwright.spec.ts"],
    }])
    assert result["success"], result
    assert result["baseline"]["status"] == "captured", result
    comparison = json.loads(artifact_path(run_id, "evidence/visual-comparison.json").read_text())
    assert len(comparison["screenshots"]) == 3
    assert all(not item["same_bytes"] for item in comparison["screenshots"])
    assert any(item["node"] == "visual-review" and item["success"] is True
               for item in get_events(run_id))
    print({"run_id": run_id, "suites": result["suites"], "comparison": comparison["screenshots"]})
    with session_scope() as session:
        session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
        session.execute(delete(Run).where(Run.id == run_id))


if __name__ == "__main__":
    asyncio.run(main())
