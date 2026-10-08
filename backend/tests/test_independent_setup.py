import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from app.setup_recipe import resolve_recipe, SetupRecipe, NativeCheck
from app.baseline import run_independent_review, run_native_checks
from app.review import RuntimePlan, RuntimeProbe
from app.review_runner import prepare_runtime, can_reuse


def configured(tmp_path, **values):
    (tmp_path / ".ardberg").mkdir(exist_ok=True)
    (tmp_path / ".ardberg/setup.json").write_text(json.dumps(values))
    return {"source_path": str(tmp_path), "analysis": {}, "mode": "critique"}


def state(**values):
    return {"container": "isolated", "origin": "", "environment": {}, "analysis": {},
            "setup_error": "", "app_error": "", "browser_error": "",
            "dependencies_ready": True, "services_ready": True, **values}


def test_manifest_fallback_discovers_checks_without_intent(tmp_path):
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (tmp_path / "package.json").write_text(json.dumps({"packageManager": "pnpm@11.9.0", "scripts": {
        "test": "vitest run", "test:watch": "vitest --watch", "type-check": "tsc --noEmit",
        "build": "vite build", "build:alias": "vite build", "deploy": "aws deploy"}}))
    recipe, evidence, gaps = resolve_recipe(tmp_path)
    assert recipe.install_commands == ["pnpm install --frozen-lockfile"]
    assert [c.kind for c in recipe.checks] == ["test", "typecheck", "build"]
    assert evidence == "package.json" and gaps
    assert not recipe.processes and not recipe.services


def test_invalid_explicit_recipe_does_not_fall_back(tmp_path):
    configured(tmp_path, services=[{"kind": "redis", "connection_environment_keys": ["URL"], "evidence_files": []}])
    (tmp_path / "package.json").write_text("{}")
    with pytest.raises(ValueError):
        resolve_recipe(tmp_path)


@pytest.mark.parametrize("value", [
    {"processes": [{"command": "npm start", "ready_url": "https://production.example:443"}]},
    {"install_commands": ["npm ci\nother"]},
    {"test_environment": {"DATABASE_URL": "postgresql://prod/database"}},
    {"test_environment": {"NODE_OPTIONS": "--require=evil"}},
    {"runtime_versions": {"node": ">=22"}},
])
def test_invalid_recipe_is_blocked(value):
    with pytest.raises(ValueError):
        SetupRecipe.model_validate(value)


def test_native_failure_does_not_stop_other_checks(monkeypatch):
    calls = []
    async def command(*args, **kwargs):
        calls.append(args[-1])
        return (1, "assertion failed") if args[-1] == "npm test" else (0, "compiled")
    monkeypatch.setattr("app.baseline._command", command)
    recipe = SetupRecipe(checks=[NativeCheck(name="tests", command="npm test"),
                                NativeCheck(name="build", kind="build", command="npm run build")])
    results = asyncio.run(run_native_checks(str(uuid4()), state(), recipe))
    assert calls == ["npm test", "npm run build"]
    assert [r["status"] for r in results] == ["failed", "passed"]


def test_readiness_failure_blocks_browser_check_but_not_typecheck(monkeypatch):
    calls = []
    async def command(*args, **kwargs):
        calls.append(args[-1])
        return 0, "ok"
    monkeypatch.setattr("app.baseline._command", command)
    recipe = SetupRecipe(checks=[NativeCheck(name="e2e", command="npm run e2e", requires_application=True),
                                NativeCheck(name="types", command="npm run typecheck", kind="typecheck")])
    results = asyncio.run(run_native_checks(str(uuid4()), state(app_error="API not ready"), recipe))
    assert [r["status"] for r in results] == ["blocked", "passed"]
    assert calls == ["npm run typecheck"]


@pytest.mark.parametrize("instruction", ["", "Verify setup and build"])
def test_model_failure_still_runs_setup_and_native_checks(monkeypatch, tmp_path, instruction):
    context = configured(tmp_path, checks=[{"name": "unit", "command": "npm test"}])
    context.update(needs_preflight_analysis=True, user_instruction=instruction)
    observed = []
    @asynccontextmanager
    async def runtime(*args):
        observed.append("setup")
        try:
            yield state()
        finally:
            observed.append("cleanup")
    async def analyze(*args):
        assert "setup" in observed
        observed.append("model failed")
        raise TimeoutError("analysis timed out")
    async def command(*args, **kwargs):
        observed.append("native")
        return 0, "tests passed"
    monkeypatch.setattr("app.baseline.prepare_runtime", runtime)
    monkeypatch.setattr("app.baseline._command", command)
    monkeypatch.setattr("app.inspection.finish_preflight", analyze)
    result = asyncio.run(run_independent_review(str(uuid4()), context))
    assert result["runtime"]["baseline"]["status"] == "passed"
    assert result["runtime"]["baseline"]["checks"][0]["status"] == "passed"
    assert "model failed" in observed and observed[-1] == "cleanup"
    assert result["verdict"] == "needs_review" and not result["success"]


def test_empty_focused_plan_keeps_baseline_and_releases_environment(monkeypatch, tmp_path):
    context = configured(tmp_path)
    observed = []
    @asynccontextmanager
    async def runtime(*args):
        observed.append("setup")
        try:
            yield state()
        finally:
            observed.append("cleanup")
    async def review(_run, _context, prepared):
        ready = await prepared
        assert observed == ["setup"]
        assert ready["baseline"]["status"] == "passed"
        return {"runtime": {"baseline": ready["baseline"], "probes_planned": 0}, "verdict": "needs_review"}
    monkeypatch.setattr("app.baseline.prepare_runtime", runtime)
    monkeypatch.setattr("app.review.run_review", review)
    result = asyncio.run(run_independent_review(str(uuid4()), context))
    assert result["runtime"]["probes_planned"] == 0
    assert observed == ["setup", "cleanup"]


def test_setup_failure_does_not_prevent_source_review(monkeypatch, tmp_path):
    context = configured(tmp_path)
    @asynccontextmanager
    async def runtime(*args):
        yield state(setup_error="Docker unavailable", dependencies_ready=False, services_ready=False)
    async def review(_run, _context, prepared):
        ready = await prepared
        assert ready["baseline"]["status"] == "failed"
        return {"source_review": "completed"}
    monkeypatch.setattr("app.baseline.prepare_runtime", runtime)
    monkeypatch.setattr("app.review.run_review", review)
    assert asyncio.run(run_independent_review(str(uuid4()), context))["source_review"] == "completed"


def test_cancellation_cleans_prepared_environment(monkeypatch, tmp_path):
    context = configured(tmp_path)
    cleaned = []
    @asynccontextmanager
    async def runtime(*args):
        try:
            yield state()
        finally:
            cleaned.append(True)
    async def scenario():
        reviewing = asyncio.Event()
        async def review(_run, _context, prepared):
            await prepared
            reviewing.set()
            await asyncio.Event().wait()
        monkeypatch.setattr("app.review.run_review", review)
        task = asyncio.create_task(run_independent_review(str(uuid4()), context))
        await reviewing.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    monkeypatch.setattr("app.baseline.prepare_runtime", runtime)
    asyncio.run(scenario())
    assert cleaned == [True]


def test_runtime_setup_runs_without_probes_and_cleans_on_consumer_error(monkeypatch, tmp_path):
    calls = []
    async def command(*args, **kwargs):
        calls.append(args)
        return 0, "ok"
    monkeypatch.setattr("app.review_runner._command", command)
    async def scenario():
        with pytest.raises(RuntimeError, match="consumer"):
            async with prepare_runtime(str(uuid4()), {"source_path": str(tmp_path), "analysis": {}},
                                       "baseline_head", []) as ready:
                assert ready["dependencies_ready"]
                raise RuntimeError("consumer")
    asyncio.run(scenario())
    assert any(c[:2] == ("docker", "run") for c in calls)
    assert any(c[:3] == ("docker", "rm", "-f") for c in calls)
    assert any(c[:3] == ("docker", "network", "rm") for c in calls)


def test_setup_event_reports_readiness_error_as_failure(monkeypatch, tmp_path):
    events = []
    async def command(*args, **kwargs):
        return 0, "ok"
    async def processes(*args):
        return "", "API not ready"
    monkeypatch.setattr("app.review_runner._command", command)
    monkeypatch.setattr("app.review_runner._start_processes", processes)
    monkeypatch.setattr("app.review_runner.emit", lambda *a, **kw: events.append((a, kw)))
    async def scenario():
        async with prepare_runtime(str(uuid4()), {"source_path": str(tmp_path), "analysis": {
            "runtime_processes": [{"command": "npm start", "ready_url": "http://localhost:3000"}]}},
                                   "baseline_head", []) as ready:
            assert ready["app_error"] == "API not ready"
            assert ready["dependencies_ready"]
    asyncio.run(scenario())
    setup = [a[2] for a, kw in events if kw.get("node") == "baseline_head_setup" and a[2] != "artifact"]
    assert setup == ["running", "failed"]


def test_equivalent_runtime_is_reused_but_incompatible_services_are_not():
    probe = RuntimeProbe(id="code", expectation_id="e", kind="script", script="print(1)", purpose="check")
    prepared = state(analysis={"install_commands": ["npm ci"], "services": []})
    assert can_reuse(prepared, {"install_commands": ["npm ci"], "services": []}, [probe])
    assert not can_reuse(prepared, {"install_commands": ["npm ci"], "services": [{"kind": "postgres"}]}, [probe])


def test_report_separates_native_results_from_focused_coverage():
    from app.report import _execution_record
    record = _execution_record({"runtime": {"observations": [], "baseline": {
        "status": "passed", "configuration": ".ardberg/setup.json", "dependencies": "passed",
        "services": "passed", "application": "not_configured", "checks": [
            {"name": "unit", "kind": "test", "status": "failed", "log": "review/baseline/unit.log"}]}}}, [])
    assert "| head | 0 | 0 | 0 |" in record
    assert "| unit | test | failed |" in record
    assert "separate from focused PR behavior coverage" in record


def test_flow_setup_branch_does_not_depend_on_runtime_planner():
    from app.flow import review_definition
    graph = review_definition(None, [])
    edges = {(e["source"], e["target"]) for e in graph["edges"]}
    assert ("preflight.fetch_repository", "runner.baseline_config") in edges
    assert ("runtime_planner.analyze", "runner.baseline_head_setup") not in edges


def test_real_review_with_zero_expectations_still_retains_native_results(monkeypatch, tmp_path):
    from app.review import BehaviorReview, CodeCritique, ReviewJudgment
    context = configured(tmp_path, checks=[{"name": "unit", "command": "npm test"}])
    context.update(changed_files=[], files=[], snippets={}, impact_map={})
    responses = [BehaviorReview(expectations=[]), CodeCritique(),
                 ReviewJudgment(summary="No focused behavior could be established")]
    @asynccontextmanager
    async def runtime(*args):
        yield state()
    async def parse(*args):
        return responses.pop(0)
    async def command(*args, **kwargs):
        return 0, "unit tests executed"
    monkeypatch.setattr("app.baseline.prepare_runtime", runtime)
    monkeypatch.setattr("app.baseline._command", command)
    monkeypatch.setattr("app.review.parse", parse)
    result = asyncio.run(run_independent_review(str(uuid4()), context))
    assert not responses
    assert result["runtime"]["probes_planned"] == 0
    assert result["runtime"]["baseline"]["checks"][0]["status"] == "passed"
    assert result["verdict"] == "needs_review"


def test_pin_activity_never_calls_model_analysis(monkeypatch, tmp_path):
    from app.db import session_scope
    from app.models import Run
    from app.workflow import pin_review_activity
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(id=run_id, repository="local/pin", pr_number=1, installation_id=1,
                        instruction="", context={"mode": "critique"}))
    class GitHub:
        async def installation_token(self, *args):
            return "test"
        async def pull_request(self, *args):
            return {"title": "Pinned test", "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}}
        async def compare_files(self, *args):
            return []
        async def pull_request_commits(self, *args):
            return [], False
        async def source_archive(self, repository, sha, token, destination):
            destination.mkdir(parents=True)
            (destination / "package.json").write_text("{}")
    async def forbidden(*args, **kwargs):
        pytest.fail("Source retrieval must not wait for model analysis")
    monkeypatch.setattr("app.inspection.GitHubApp", GitHub)
    monkeypatch.setattr("app.inspection.parse", forbidden)
    monkeypatch.setattr("app.inspection.analyze_repository", forbidden)
    result = asyncio.run(pin_review_activity(run_id))
    assert result["needs_preflight_analysis"]
    assert result["head_sha"] == "a" * 40
    assert Path(result["source_path"]).is_dir()


def test_invalid_recipe_still_allows_source_review(monkeypatch, tmp_path):
    context = configured(tmp_path, processes=[{"command": "run", "ready_url": "https://external.example"}])
    async def review(_run, _context, prepared):
        baseline = (await prepared)["baseline"]
        assert baseline["status"] == "blocked" and "ready_url" in baseline["error"]
        return {"reviewed": True}
    monkeypatch.setattr("app.review.run_review", review)
    assert asyncio.run(run_independent_review(str(uuid4()), context))["reviewed"]


def test_retry_model_failure_does_not_reuse_old_observations(monkeypatch, tmp_path):
    from app.artifacts import write_artifact
    run_id = str(uuid4())
    write_artifact(run_id, "review/runtime-observations.json", json.dumps({
        "observations": [{"probe_id": "old", "revision": "head", "status": "observed"}]}))
    context = configured(tmp_path)
    @asynccontextmanager
    async def runtime(*args):
        yield state()
    async def review(*args, **kwargs):
        raise TimeoutError("Model unavailable on retry")
    monkeypatch.setattr("app.baseline.prepare_runtime", runtime)
    monkeypatch.setattr("app.review.run_review", review)
    result = asyncio.run(run_independent_review(run_id, context))
    assert result["runtime"]["observations"] == []


def test_runtime_evidence_survives_evidence_critic_failure(monkeypatch, tmp_path):
    from app.artifacts import write_artifact
    context = configured(tmp_path)
    @asynccontextmanager
    async def runtime(*args):
        yield state()
    async def review(run, context, prepared):
        await prepared
        write_artifact(run, "review/runtime-observations.json", json.dumps({
            "observations": [{"probe_id": "current", "revision": "head", "status": "observed"}],
            "probes_planned": 1, "probes_observed": 1}))
        raise TimeoutError("Evidence critic unavailable")
    monkeypatch.setattr("app.baseline.prepare_runtime", runtime)
    monkeypatch.setattr("app.review.run_review", review)
    result = asyncio.run(run_independent_review(str(uuid4()), context))
    assert result["runtime"]["observations"][0]["probe_id"] == "current"
    assert result["runtime"]["probes_observed"] == 1
