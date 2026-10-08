"""Local Docker smoke: AI failure still yields native evidence and resource cleanup.

Set DATA_DIR and DATABASE_URL to disposable locations before running this module.
No GitHub or model requests are made.
"""

import asyncio
import json
from uuid import uuid4

from app.artifacts import run_dir
from app.baseline import run_independent_review
from app.db import init_db, session_scope
from app.events import get_events
from app.models import Run
from app.runner import _command
import app.inspection as inspection
import app.review as review


async def main():
    init_db()
    run_id = str(uuid4())
    source = run_dir(run_id) / "source"
    (source / ".ardberg").mkdir(parents=True)
    (source / "check.py").write_text("assert 6 * 7 == 42\nprint('native check executed')\n")
    (source / ".ardberg/setup.json").write_text(json.dumps({
        "processes": [{"command": "python -m http.server 8000", "ready_url": "http://localhost:8000"}],
        "checks": [{"name": "native", "command": "python check.py"},
                   {"name": "intentional-failure", "command": "python -c 'raise SystemExit(7)'"},
                   {"name": "after-failure", "command": "python check.py"}]}))
    with session_scope() as session:
        session.add(Run(id=run_id, repository="local/independent-setup-smoke", pr_number=1,
                        installation_id=1, instruction="", context={"mode": "critique"}))
    original = inspection.finish_preflight
    async def unavailable(*args):
        raise TimeoutError("Simulated model timeout; no model request was made")
    inspection.finish_preflight = unavailable
    try:
        result = await run_independent_review(run_id, {"source_path": str(source),
                                                    "needs_preflight_analysis": True})
    finally:
        inspection.finish_preflight = original
    baseline = result["runtime"]["baseline"]
    assert baseline["status"] == "passed", baseline
    assert baseline["application"] == "passed", baseline
    assert [c["status"] for c in baseline["checks"]] == ["passed", "failed", "passed"], baseline
    assert baseline["checks"][1]["exit_code"] == 7
    assert result["runtime"]["probes_planned"] == 0 and result["verdict"] == "needs_review"
    assert any(e["node"] == "baseline_head_setup" and e["status"] == "passed" for e in get_events(run_id))
    # Use the same configured process for a focused HTTP check without a second head container.
    original_review = review.run_review
    async def focused(run, context, prepared):
        ready = await prepared
        process = review.RuntimeProcess(command="python -m http.server 8000", ready_url="http://localhost:8000")
        plan = review.RuntimePlan(environments=[review.RuntimeEnvironment(
            id="web", evidence_paths=[".ardberg/setup.json"], rationale="Configured application",
            processes=[process], target_url=process.ready_url)], probes=[review.RuntimeProbe(
            id="http", expectation_id="page", kind="http", path="/", purpose="Read local page", environment_id="web")])
        result = await review.run_runtime_probes(run, {**context, "analysis": {}}, plan, prepared=ready)
        assert result["probes_observed"] == 1, result
        return {"runtime": result}
    review.run_review = focused
    try:
        await run_independent_review(run_id, {"source_path": str(source)})
    finally:
        review.run_review = original_review
    assert any(e["node"] == "head_setup" and e["detail"].get("reused_baseline") for e in get_events(run_id))
    _, remaining = await _command("docker", "ps", "-aq", "--filter", "name=ardberg-review-baseline_head-")
    assert not remaining.strip(), f"Baseline container leaked: {remaining}"
    print(json.dumps({"run_id": run_id, "setup": baseline["status"], "readiness": baseline["application"],
                      "native_checks": [c["status"] for c in baseline["checks"]],
                      "focused_runtime_reuse": "passed", "cleanup": "passed"}))


if __name__ == "__main__":
    asyncio.run(main())
