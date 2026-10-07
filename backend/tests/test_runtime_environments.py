import asyncio
from uuid import uuid4

import pytest

from app.review import (BehaviorReview, ExpectedBehavior, RuntimeEnvironment,
                        RuntimePlan, RuntimeProbe, RuntimeProcess, _validate_probes)


def test_service_failure_does_not_block_independent_code(monkeypatch):
    from app.review_runner import run_runtime_probes
    calls = []
    async def revision(_run, context, revision, probes):
        if not probes:
            return []
        calls.append(context["analysis"])
        return [{"probe_id": p.id, "expectation_id": p.expectation_id,
                 "revision": revision, "kind": p.kind,
                 "status": "blocked" if context["analysis"]["services"] else "observed"}
                for p in probes]
    monkeypatch.setattr("app.review_runner._run_revision", revision)
    monkeypatch.setattr("app.review_runner.emit", lambda *args, **kwargs: None)
    plan = RuntimePlan(probes=[
        RuntimeProbe(id="api", expectation_id="access", kind="http", path="/", purpose="Check access", environment_id="api"),
        RuntimeProbe(id="logic", expectation_id="access", kind="script", script="print(1)", purpose="Check logic", environment_id="code"),
    ], environments=[
        RuntimeEnvironment(id="api", evidence_paths=["package.json"], rationale="API needs storage", services=["postgres"],
                           processes=[RuntimeProcess(command="npm run api", ready_url="http://localhost:8000/health")], target_url="http://localhost:8000"),
        RuntimeEnvironment(id="code", evidence_paths=["package.json"], rationale="Pure logic"),
    ])
    result = asyncio.run(run_runtime_probes(str(uuid4()), {"analysis": {
        "services": [{"kind": "postgres", "connection_environment_keys": ["DATABASE_URL"]}],
        "test_environment": [{"key": "DATABASE_URL", "value": "unavailable"}],
    }}, plan))
    assert result["probes_blocked"] == 1 and result["probes_observed"] == 1
    assert calls[1]["services"] == [] and calls[1]["test_environment"] == []
    assert calls[1]["runtime_processes"] == []


def test_environment_rejects_remote_readiness_and_unknown_services():
    behavior = BehaviorReview(expectations=[ExpectedBehavior(id="a", action="Open", expected="Visible", origin="user")])
    environment = RuntimeEnvironment(id="ui", evidence_paths=["package.json"], rationale="UI",
        processes=[RuntimeProcess(command="npm start", ready_url="http://example.com:80")])
    with pytest.raises(ValueError, match="local readiness"):
        _validate_probes(RuntimePlan(environments=[environment]), behavior, {"files": ["package.json"]})
    environment.processes = []
    environment.services = ["postgres"]
    with pytest.raises(ValueError, match="not discovered"):
        _validate_probes(RuntimePlan(environments=[environment]), behavior, {"files": ["package.json"]})


def test_setup_installs_once_before_isolation_and_retains_fixtures(tmp_path):
    from app.review_runner import _setup_phases
    installs, fixtures = _setup_phases({
        "install_commands": ["pip install -r requirements.txt", "python -m build --wheel"],
        "runtime_setup_commands": ["timeout 300s python -m pip install -r requirements.txt",
                                   "python -m build --wheel", "python -m playwright install chromium",
                                   "python manage.py migrate", "python fixtures.py"],
    }, tmp_path)
    assert installs == ["pip install -r requirements.txt", "python -m build --wheel",
                        "python -m playwright install chromium"]
    assert fixtures == ["python manage.py migrate", "python fixtures.py"]


def test_environment_and_requirement_ids_are_normalized_without_model_retry():
    behavior = BehaviorReview(expectations=[ExpectedBehavior(id="aria_tree", action="Inspect", expected="Tree", origin="user")])
    plan = RuntimePlan(environments=[RuntimeEnvironment(id="Python Library", evidence_paths=["module.py"], rationale="Code")],
        probes=[RuntimeProbe(id="probe", expectation_id="Aria Tree", kind="script", script="print(1)", purpose="Inspect", environment_id="Python Library")])
    _validate_probes(plan, behavior, {"files": ["module.py"]})
    assert plan.environments[0].id == "python_library"
    assert plan.probes[0].environment_id == "python_library"
    assert plan.probes[0].expectation_id == "aria_tree"


def test_processes_wait_for_readiness_before_returning_origin(monkeypatch):
    from app.review_runner import _start_processes
    calls = []
    statuses = iter(["503", "200", "200"])
    async def command(*args, **kwargs):
        calls.append(args)
        return (0, next(statuses)) if "curl" in args else (0, "")
    async def sleep(_seconds):
        pass
    monkeypatch.setattr("app.review_runner._command", command)
    monkeypatch.setattr("app.review_runner.asyncio.sleep", sleep)
    monkeypatch.setattr("app.review_runner.emit", lambda *args, **kwargs: None)
    result = asyncio.run(_start_processes("run", "container", "head", [
        {"command": "npm run api", "ready_url": "http://localhost:8000/health"},
        {"command": "npm run ui", "ready_url": "http://localhost:3000/"},
    ], "http://localhost:3000", {}, "review/setup"))
    assert result == ("http://127.0.0.1:3000", "")
    assert len([call for call in calls if "curl" in call]) == 3
    assert "npm run ui" in str(calls[3])


def test_artifact_download_recovers_deleted_local_cache():
    from app.api import download_artifact
    from app.artifacts import artifact_path, write_artifact
    from app.db import session_scope
    from app.models import Run
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(id=run_id, repository="local/artifacts", pr_number=1, installation_id=1, instruction="Inspect"))
    write_artifact(run_id, "review/probe.log", "actual runtime evidence")
    path = artifact_path(run_id, "review/probe.log")
    path.unlink()
    response = download_artifact(run_id, "review/probe.log")
    assert response.path.read_text(encoding="utf-8") == "actual runtime evidence"


def test_execution_record_uses_observations_not_planned_counts():
    from app.report import _execution_record
    result = _execution_record({"runtime": {"probes_observed": 99, "observations": [
        {"revision": "head", "status": "observed"}, {"revision": "head", "status": "blocked"},
    ]}}, [{"id": 7, "stage": "runtime", "status": "passed", "detail": {"command": "pnpm db:push"}}])
    assert "| head | 1 | 0 | 1 |" in result
    assert "`pnpm db:push`: **passed** [event:7]" in result
    assert "99" not in result


def test_local_review_never_publishes_to_github(monkeypatch):
    from app.report import publish_report
    from app.db import session_scope
    from app.models import Run
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(id=run_id, repository="local/report", pr_number=1, installation_id=1,
                        instruction="Inspect", context={"publish_to_github": False}))
    def forbidden():
        pytest.fail("A local review must not create a GitHub publishing client")
    monkeypatch.setattr("app.report.GitHubApp", forbidden)
    asyncio.run(publish_report(run_id, "Local findings", "neutral"))


def test_retry_report_does_not_repeat_superseded_setup_failure():
    from app.report import _execution_record, classify_failure
    events = [
        {"id": 1, "stage": "runtime", "node": "head_setup", "status": "failed",
         "detail": {"command": "old setup", "error": "Unavailable dependency"}},
        {"id": 2, "stage": "preflight", "node": "reuse_pinned_context", "status": "passed", "detail": {}},
        {"id": 3, "stage": "runtime", "node": "head_install_1", "status": "passed",
         "detail": {"command": "fixed setup"}},
    ]
    record = _execution_record({"runtime": {"observations": []}}, events)
    assert "old setup" not in record and "fixed setup" in record
    assert classify_failure(events) == []


def test_retry_report_replaces_previous_automated_outcome(monkeypatch):
    from app.artifacts import artifact_path, write_artifact
    from app.db import session_scope
    from app.models import Run
    from app.workflow import report_activity
    import json
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(id=run_id, repository="local/retry", pr_number=1,
                        installation_id=1, instruction="Inspect"))
    write_artifact(run_id, "outcome.json", json.dumps({"runtime": "old blocked attempt"}))
    async def generate(*_args):
        return "Updated report"
    monkeypatch.setattr("app.workflow.generate_report", generate)
    outcome = {"mode": "critique", "runtime": "new observed attempt"}
    asyncio.run(report_activity(run_id, outcome))
    assert json.loads(artifact_path(run_id, "outcome.json").read_text()) == outcome
    asyncio.run(report_activity(run_id, {**outcome, "manual_review": {}}))
    assert json.loads(artifact_path(run_id, "outcome.json").read_text()) == outcome


def test_setup_aliases_share_one_environment(monkeypatch):
    from app.review_runner import run_runtime_probes
    calls = []
    async def revision(_run, _context, revision, probes):
        if probes:
            calls.append((revision, len(probes)))
        return []
    monkeypatch.setattr("app.review_runner._run_revision", revision)
    monkeypatch.setattr("app.review_runner.emit", lambda *args, **kwargs: None)
    plan = RuntimePlan(environments=[
        RuntimeEnvironment(id="a", evidence_paths=["requirements.txt"], rationale="Install",
                           setup_commands=["python -m pip install -r requirements.txt"]),
        RuntimeEnvironment(id="b", evidence_paths=["requirements.txt"], rationale="Install",
                           setup_commands=["timeout 300s pip install -r requirements.txt"]),
    ], probes=[RuntimeProbe(id=name, expectation_id=name, kind="script", script="print(1)",
                           purpose="Inspect", environment_id=name) for name in ("a", "b")])
    asyncio.run(run_runtime_probes(str(uuid4()), {"analysis": {
        "install_commands": ["pip install -r requirements.txt"], "services": []}}, plan))
    assert calls == [("head", 2)]


def test_preflight_failure_retains_local_publication_policy(monkeypatch):
    from app.inspection import prepare_run
    from app.db import session_scope
    from app.events import get_run
    from app.models import Run
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(id=run_id, repository="local/preflight", pr_number=1, installation_id=1,
                        instruction="Check behavior", context={"mode": "critique", "publish_to_github": False}))
    class FakeGitHub:
        async def installation_token(self, *_args):
            return "local"
        async def pull_request(self, *_args):
            return {"title": "Change", "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}}
        async def compare_files(self, *_args):
            return [{"filename": "module.py", "patch": "+value = 1"}]
        async def pull_request_commits(self, *_args):
            return [], False
        async def source_archive(self, _repo, _sha, _token, source):
            source.mkdir(parents=True, exist_ok=True)
            (source / "module.py").write_text("value = 1")
    async def fail_analysis(*_args, **_kwargs):
        raise RuntimeError("Analysis unavailable")
    monkeypatch.setattr("app.inspection.GitHubApp", FakeGitHub)
    monkeypatch.setattr("app.inspection.analyze_repository", fail_analysis)
    with pytest.raises(RuntimeError, match="Analysis unavailable"):
        asyncio.run(prepare_run(run_id))
    assert get_run(run_id)["context"]["publish_to_github"] is False


def test_planner_batches_preserve_success_and_merge_equivalent_environments(monkeypatch):
    from app.review import CodeCritique, _plan_runtime
    calls = []
    async def parse(_prompt, payload, _schema):
        expectations = payload["expectations"]["expectations"]
        calls.append(len(expectations))
        first = expectations[0]["id"]
        if first == "e6":
            raise RuntimeError("One planning request failed")
        return RuntimePlan(environments=[RuntimeEnvironment(id="code", evidence_paths=["module.py"], rationale="Pure code")],
            probes=[RuntimeProbe(id="probe", expectation_id=first, kind="script", script="print(1)", purpose="Observe", environment_id="code")])
    monkeypatch.setattr("app.review.parse", parse)
    monkeypatch.setattr("app.review.emit", lambda *args, **kwargs: None)
    behaviors = BehaviorReview(expectations=[ExpectedBehavior(id=f"e{i}", action="Call", expected="Result", origin="user") for i in range(7)])
    plan = asyncio.run(_plan_runtime("run", "Plan", {}, behaviors, CodeCritique(), {"files": ["module.py"]}))
    assert calls == [3, 3, 1]
    assert len(plan.probes) == 2 and len(plan.environments) == 1
    assert len({p.id for p in plan.probes}) == 2
    assert all(p.environment_id == "env_1" for p in plan.probes)
    assert any("batch 3 unavailable" in reason for reason in plan.untestable)


def test_retry_preserves_pins_and_skips_completed_preflight(monkeypatch, tmp_path):
    from app.api import retry_pinned_review
    from app.artifacts import write_artifact
    from app.db import session_scope
    from app.events import get_run
    from app.models import Run
    from app.workflow import preflight_activity
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(id=run_id, repository="local/retry", pr_number=1, installation_id=1,
            instruction="Inspect", status="completed", head_sha="a" * 40, base_sha="b" * 40,
            context={"mode": "critique", "publish_to_github": False, "source_path": str(tmp_path)}))
    for name in ("expectations", "code-critique", "runtime-plan"):
        write_artifact(run_id, f"review/{name}.json", "{}")
    started = []
    class Client:
        async def start_workflow(self, *_args, **kwargs):
            started.append(kwargs)
    async def connect(*_args):
        return Client()
    async def forbidden(*_args):
        pytest.fail("Pinned retry must not fetch and analyze the repository again")
    monkeypatch.setattr("app.api.Client.connect", connect)
    monkeypatch.setattr("app.workflow.prepare_run", forbidden)
    asyncio.run(retry_pinned_review(run_id, replan_runtime=False))
    context = asyncio.run(preflight_activity(run_id))
    assert context["resume_source_review"] and context["publish_to_github"] is False
    assert context["resume_runtime_plan"] is True
    assert get_run(run_id)["head_sha"] == "a" * 40
    assert len(started) == 1
