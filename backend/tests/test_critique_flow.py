import asyncio
from pathlib import Path
from uuid import uuid4

from app.db import init_db, session_scope
from app.events import get_run
from app.models import Run
from app.review import (BehaviorAssessment, BehaviorReview, CodeAssessment, CodeConcern, CodeCritique, ExpectedBehavior,
                        ReviewFinding, ReviewJudgment, RuntimePlan, RuntimeProbe,
                        _pinned_changes, _source_evidence, _trace_paths, run_review)


def test_new_api_runs_start_critique_workflow(monkeypatch):
    from app.api import _start_run
    from app.workflow import ReviewWorkflow

    init_db()
    started = []
    class FakeGitHub:
        async def installation_token(self, _installation):
            return "token"
        async def pull_request(self, _repository, _number, _token):
            return {"title": "Review change", "head": {"sha": "a" * 40},
                    "base": {"sha": "b" * 40}}
        async def create_check(self, _repository, _sha, _token, _title):
            return 123
    class FakeClient:
        async def start_workflow(self, workflow, *args, **kwargs):
            started.append((workflow, args, kwargs))
    async def connect(_address):
        return FakeClient()

    monkeypatch.setattr("app.api.GitHubApp", FakeGitHub)
    monkeypatch.setattr("app.api.Client.connect", connect)
    run_id = asyncio.run(_start_run("local/critique-workflow", 1,
                                    "Find missing behavior", [], 1))
    assert started[0][0] == ReviewWorkflow.run
    assert get_run(run_id)["context"]["mode"] == "critique"


def test_source_review_includes_likely_caller_and_relevant_documentation(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "access.py").write_text("def approver_access():\n    return True\n")
    (tmp_path / "src" / "route.py").write_text("from .access import approver_access\n")
    (tmp_path / "docs" / "permissions.md").write_text("Approver access requires membership.\n")
    files = ["src/access.py", "src/route.py", "docs/permissions.md"]
    selected, gaps = _source_evidence(
        {"source_path": str(tmp_path), "files": files, "snippets": {}},
        [{"filename": "src/access.py"}],
    )
    assert set(files) <= set(selected)
    assert not gaps
    assert _trace_paths("src/access.py", selected, []) == ["src/access.py", "src/route.py"]


def test_large_diff_prioritizes_paths_matching_review_intent():
    changed = [{"filename": f"src/misc_{index}.ts", "patch": "x" * 8000}
               for index in range(30)]
    changed.append({"filename": "src/approver-project-access.ts", "patch": "specific behavior"})
    excerpts, gaps = _pinned_changes("unused", {
        "title": "Approver project access", "changed_files": changed,
    })
    assert excerpts[0]["filename"] == "src/approver-project-access.ts"
    assert excerpts[0]["patch"] == "specific behavior"
    assert gaps


def test_node_runtime_probe_uses_commonjs_extension(monkeypatch):
    from app.review_runner import _observe

    calls = []
    async def fake_command(*args, **_kwargs):
        calls.append(args)
        return 0, "observed"
    monkeypatch.setattr("app.review_runner._command", fake_command)
    monkeypatch.setattr("app.review_runner.emit", lambda *args, **kwargs: None)
    probe = RuntimeProbe(id="module_probe", expectation_id="expected", kind="script",
                         interpreter="node", script="console.log('ok')", purpose="Exercise module")
    result = asyncio.run(_observe(str(uuid4()), "container", "head", probe, "", {}))
    assert result["status"] == "observed"
    assert any(str(arg).endswith("module_probe.cjs") for call in calls for arg in call)


def test_structured_report_fallback_leads_with_critique():
    from app.report import _critique_fallback

    report = _critique_fallback(
        {"repository": "owner/repo", "pr_number": 4, "head_sha": "a" * 40},
        {"verdict": "needs_review", "expectations": {"expectations": [{
            "id": "access", "expected": "Approvers can see their projects"}]},
         "code_critique": {"assessments": [{"expectation_id": "access",
             "status": "incomplete", "trace": ["src/access.ts"]}]},
         "runtime": {"observations": []},
         "judgment": {"summary": "Project access needs review.",
             "assessments": [{"expectation_id": "access", "status": "unverified",
                              "rationale": "App could not start"}],
             "findings": [{"expectation_id": "access", "title": "Project access gap",
                           "status": "potential", "confidence": "low",
                           "expected": "Approvers can see their projects",
                           "code_evidence": "return allProjects", "source_paths": ["src/access.ts"],
                           "observed": "App could not start", "impact": "Some projects may be hidden",
                           "reproduction": "Open the project list", "artifact_paths": []}],
             "unverified": []}},
    )
    assert report.index("Project access gap") < report.index("Requirement-by-requirement")
    assert "**confidence:** low" in report
    assert "Code: **incomplete**; runtime: **unverified**" in report


def test_review_classifies_confirmed_only_with_head_observation(monkeypatch, tmp_path: Path):
    init_db()
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(id=run_id, repository="owner/repo", pr_number=1,
                        installation_id=1, instruction="Reject empty names",
                        context={"mode": "critique"}))
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "name.py").write_text("def valid(name):\n    return True\n")
    context = {
        "source_path": str(source), "base_source_path": "",
        "user_instruction": "Reject empty names", "instruction_source": "user",
        "changed_files": [{"filename": "src/name.py", "patch": "+    return True"}],
        "files": ["src/name.py"], "snippets": {}, "impact_map": {}, "analysis": {},
    }
    response = [
        BehaviorReview(expectations=[ExpectedBehavior(
            id="empty_name", action="Submit an empty name", expected="Reject it",
            origin="user", evidence_paths=["src/name.py"])]),
        CodeCritique(assessments=[CodeAssessment(
            expectation_id="empty_name", status="contradictory",
            trace=["src/name.py"], evidence_path="src/name.py",
            evidence_quote="return True", rationale="All names are accepted")],
            concerns=[CodeConcern(
            expectation_id="empty_name", title="Missing empty-name check",
            explanation="The function always accepts", impact="Invalid names can be stored",
            path="src/name.py", evidence_quote="return True")]),
        RuntimePlan(probes=[RuntimeProbe(
            id="empty_probe", expectation_id="empty_name", kind="script",
            script="from src.name import valid\nprint(valid(''))", purpose="Observe empty input")]),
        ReviewJudgment(summary="Empty names are accepted", assessments=[BehaviorAssessment(
            expectation_id="empty_name", status="missing", probe_ids=["empty_probe"],
            rationale="The observed result was True")], findings=[ReviewFinding(
            expectation_id="empty_name", title="Empty names accepted",
            status="confirmed_missing", expected="Reject it", observed="True",
            impact="Invalid names can be stored", reproduction="Call valid('')",
            source_paths=["src/name.py"],
            artifact_paths=["review/runtime/head/empty_probe.log"])]),
    ]
    async def fake_parse(_prompt, _payload, _schema):
        return response.pop(0)
    async def observed(_run_id, _context, _plan):
        return {"observations": [{"probe_id": "empty_probe", "expectation_id": "empty_name",
                                  "revision": "head", "kind": "script", "status": "observed",
                                  "exit_code": 0, "output_excerpt": "True",
                                  "log": "review/runtime/head/empty_probe.log"}],
                "probes_planned": 1, "probes_observed": 1, "probes_blocked": 0,
                "artifact": "review/runtime-observations.json"}
    monkeypatch.setattr("app.review.parse", fake_parse)
    monkeypatch.setattr("app.review.run_runtime_probes", observed)
    outcome = asyncio.run(run_review(run_id, context))
    assert outcome["verdict"] == "findings"
    assert outcome["check_conclusion"] == "failure"
    assert outcome["judgment"]["findings"][0]["status"] == "confirmed_missing"
    assert outcome["judgment"]["findings"][0]["code_evidence"] == "return True"
    assert outcome["code_critique"]["assessments"][0]["status"] == "contradictory"


def test_review_downgrades_missing_behavior_when_runtime_is_blocked(monkeypatch, tmp_path: Path):
    init_db()
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(id=run_id, repository="owner/repo", pr_number=1,
                        installation_id=1, instruction="Reject empty names",
                        context={"mode": "critique"}))
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "name.py").write_text("def valid(name):\n    return True\n")
    context = {"source_path": str(source), "user_instruction": "Reject empty names",
               "changed_files": [{"filename": "src/name.py", "patch": "+    return True"}],
               "files": ["src/name.py"], "snippets": {}, "impact_map": {}, "analysis": {}}
    response = [
        BehaviorReview(expectations=[ExpectedBehavior(
            id="empty_name", action="Submit an empty name", expected="Reject it",
            origin="user", evidence_paths=["src/name.py"])]),
        CodeCritique(concerns=[]),
        RuntimePlan(probes=[RuntimeProbe(
            id="empty_probe", expectation_id="empty_name", kind="script",
            script="print(True)", purpose="Observe empty input")]),
        ReviewJudgment(summary="One confirmed missing behavior", assessments=[BehaviorAssessment(
            expectation_id="empty_name", status="met", probe_ids=["empty_probe"],
            rationale="Claimed to pass without evidence")], findings=[ReviewFinding(
            expectation_id="empty_name", title="Possibly missing validation",
            status="confirmed_missing", expected="Reject it", observed="Unavailable",
            impact="Invalid names might be stored", reproduction="Call valid('')")]),
    ]
    async def fake_parse(_prompt, _payload, _schema):
        return response.pop(0)
    async def blocked(_run_id, _context, _plan):
        return {"observations": [{"probe_id": "empty_probe", "expectation_id": "empty_name",
                                  "revision": "head", "kind": "script", "status": "blocked",
                                  "error": "Dependency unavailable"}],
                "probes_planned": 1, "probes_observed": 0, "probes_blocked": 1,
                "artifact": "review/runtime-observations.json"}
    monkeypatch.setattr("app.review.parse", fake_parse)
    monkeypatch.setattr("app.review.run_runtime_probes", blocked)
    outcome = asyncio.run(run_review(run_id, context))
    assert outcome["verdict"] == "needs_review"
    assert outcome["check_conclusion"] == "neutral"
    assert outcome["judgment"]["findings"][0]["status"] == "unverified"
    assert outcome["judgment"]["assessments"][0]["status"] == "unverified"
    assert outcome["code_critique"]["assessments"][0]["status"] == "unknown"
    assert outcome["judgment"]["summary"].startswith("No confirmed defect")
