"""Disposable Docker smoke for independent code and browser/API environments."""
import asyncio
import json
from uuid import uuid4

from app.artifacts import run_dir, restore_artifact
from app.db import init_db, session_scope
from app.events import set_run
from app.models import Run
from app.review import RuntimeEnvironment, RuntimePlan, RuntimeProbe, RuntimeProcess, BrowserAction
from app.review_runner import run_runtime_probes


async def main():
    init_db()
    run_id = str(uuid4())
    root = run_dir(run_id)
    source = root / "smoke-source"
    source.mkdir()
    (source / "value.py").write_text("value = 42\n", encoding="utf-8")
    (source / "setup_database.py").write_text(
        "import os, psycopg\n"
        "with psycopg.connect(os.environ['DATABASE_URL']) as c:\n"
        "    c.execute('CREATE TABLE fixture (value integer)')\n"
        "    c.execute('INSERT INTO fixture VALUES (42)')\n", encoding="utf-8")
    (source / "index.html").write_text(
        '<button onclick="document.querySelector(\'output\').textContent=\'Saved\'">Save</button><output>Waiting</output>',
        encoding="utf-8")
    with session_scope() as session:
        session.add(Run(id=run_id, repository="local/runtime-smoke", pr_number=1,
                        installation_id=1, instruction="Click Save and inspect library value"))
    plan = RuntimePlan(environments=[
        RuntimeEnvironment(id="code", evidence_paths=["value.py"], rationale="Isolated function"),
        RuntimeEnvironment(id="ui", evidence_paths=["index.html"], rationale="UI plus API readiness",
            processes=[RuntimeProcess(command="python -m http.server 8000", ready_url="http://localhost:8000"),
                       RuntimeProcess(command="python -m http.server 9000", ready_url="http://localhost:9000")],
            target_url="http://localhost:8000"),
        RuntimeEnvironment(id="database", evidence_paths=["setup_database.py"],
            rationale="Disposable database with declared fixture", services=["postgres"],
            setup_commands=["python -m pip install 'psycopg[binary]'"]),
    ], probes=[
        RuntimeProbe(id="value", expectation_id="logic", kind="script", script="from value import value\nprint(value)",
                     purpose="Observe value", environment_id="code", compare_base=True),
        RuntimeProbe(id="click", expectation_id="save", kind="browser", path="/", environment_id="ui",
                     actions=[BrowserAction(action="click", selector="button")], purpose="Observe Save"),
        RuntimeProbe(id="http", expectation_id="page", kind="http", path="/", environment_id="ui", purpose="Read page"),
        RuntimeProbe(id="database", expectation_id="fixture", kind="script", environment_id="database",
            purpose="Read seeded database row", script="import os, psycopg\n"
                "with psycopg.connect(os.environ['DATABASE_URL']) as c:\n"
                "    value = c.execute('SELECT value FROM fixture').fetchone()[0]\n"
                "    assert value == 42\n    print('seeded row', value)\n"),
    ])
    result = await run_runtime_probes(run_id, {"source_path": str(source), "base_source_path": str(source),
        "analysis": {"install_commands": [], "test_environment": [], "services": [{
            "kind": "postgres", "evidence_files": ["setup_database.py"],
            "connection_environment_keys": ["DATABASE_URL"], "setup_commands": ["python setup_database.py"],
        }]}}, plan)
    set_run(run_id, status="completed" if result["probes_observed"] == 4 else "failed", stage="done")
    assert result["probes_observed"] == 4, result
    assert result["base_probes_unavailable"] == 0, result
    browser = next(o for o in result["observations"] if o["probe_id"] == "click")
    assert 'Saved' in browser["output_excerpt"]
    assert 'completed_actions' in browser["output_excerpt"]
    database = next(o for o in result["observations"] if o["probe_id"] == "database")
    assert 'seeded row 42' in database["output_excerpt"]
    screenshot = restore_artifact(run_id, browser["screenshot"])
    screenshot.unlink()
    assert restore_artifact(run_id, browser["screenshot"]).stat().st_size > 0
    print(json.dumps({"run_id": run_id, "observed": result["probes_observed"], "screenshot_recovered": True}))


if __name__ == "__main__":
    asyncio.run(main())
