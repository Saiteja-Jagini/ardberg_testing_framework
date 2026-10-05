import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import json
import zipfile
from urllib.error import HTTPError

import pytest
from sqlalchemy import delete
from fastapi.testclient import TestClient

from app import github
from app.agents import _patch_paths, _test_path, build_agent_graph, generate_patch, select_commands
from app.api import app
from app.artifacts import write_artifact
from app.db import init_db, session_scope
from app.events import emit, get_events, get_run, set_run
from app.flow import definition
from app.models import InteractivePreview, InteractivePreviewOptions, ManualObservation, PullRequestSettings, Run, RunEvent
from app.publication import publish_generated_tests
from app.report import _check_claim_links, evidence_artifact_excerpts, generate_report, publish_report
from app.runner import _apply_patches, _postgres_image, _structured_result, _suite_commands, _test_environment
from app.inspection import analyze_repository, hydrate_evidence, prefer_fresh_database_schema_push, selected_file_context, select_agents, specialist_feasibility, validate_services
from app.interactive_preview import _responds, preview_defaults
from app.schemas import CommandSelection, EvidenceClaim, FrameworkAnalysis, GeneratedPatch, ReportDraft, ReportVerification
from app.schemas import TestCase as CaseSchema, TestPlan as PlanSchema, TestService as ServiceSchema


def new_run() -> str:
    init_db()
    with session_scope() as session:
        record = Run(
            id=str(uuid4()), repository="owner/repo", pr_number=7, installation_id=11,
            instruction="Submitting a wrong password must show an error without creating a session.",
        )
        session.add(record)
        return record.id


def test_github_urls_and_webhook_signature(monkeypatch):
    assert github.parse_github_url("https://github.com/owner/repo/pull/42") == ("owner/repo", 42)
    assert github.parse_github_url("https://github.com/owner/repo") == ("owner/repo", None)
    with pytest.raises(ValueError):
        github.parse_github_url("https://evil.example/owner/repo")
    with pytest.raises(ValueError):
        github.parse_github_url("https://github.com/owner/repo/tree/main")
    monkeypatch.setattr(github, "settings", replace(github.settings, github_webhook_secret="secret"))
    body = b'{"action":"opened"}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert github.verify_webhook(body, signature)
    assert not github.verify_webhook(body + b"x", signature)


def test_repository_archive_uses_separate_download_and_extracted_limits(monkeypatch, tmp_path):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("owner-repo-commit/src/data.txt", "x" * 2048)
    assert len(buffer.getvalue()) < 500

    async def fake_request(_self, _method, _path, *, token):
        return SimpleNamespace(content=buffer.getvalue())

    monkeypatch.setattr(github.GitHubApp, "_request", fake_request)
    monkeypatch.setattr(github, "settings", replace(
        github.settings, max_archive_bytes=500,
        max_extracted_bytes=4096, max_archive_member_bytes=4096,
    ))
    source = tmp_path / "source"
    asyncio.run(github.GitHubApp().source_archive("owner/repo", "a" * 40, "token", source))
    assert (source / "src/data.txt").read_text() == "x" * 2048


def test_github_test_commit_fast_forwards_only_declared_files(monkeypatch):
    calls = []

    async def fake_pr(_self, _repository, _number, _token):
        return {"head": {"sha": "a" * 40, "ref": "feature/tests",
                         "repo": {"full_name": "owner/repo"}}}

    async def fake_request(_self, method, path, *, token, json_body=None):
        calls.append((method, path, json_body))
        if method == "GET" and "/git/refs/" in path:
            return SimpleNamespace(json=lambda: {"object": {"sha": "a" * 40}})
        if method == "GET" and "/git/commits/" in path:
            return SimpleNamespace(json=lambda: {"tree": {"sha": "b" * 40}})
        if method == "POST" and path.endswith("/git/trees"):
            return SimpleNamespace(json=lambda: {"sha": "c" * 40})
        if method == "POST" and path.endswith("/git/commits"):
            return SimpleNamespace(json=lambda: {"sha": "d" * 40})
        if method == "PATCH":
            return SimpleNamespace(json=lambda: {"object": {"sha": "d" * 40}})
        raise AssertionError((method, path))

    monkeypatch.setattr(github.GitHubApp, "pull_request", fake_pr)
    monkeypatch.setattr(github.GitHubApp, "_request", fake_request)
    sha = asyncio.run(github.GitHubApp().commit_test_files(
        "owner/repo", 7, "a" * 40, {"tests/new.test.py": "assert True\n"},
        "token", "run-id",
    ))
    assert sha == "d" * 40
    tree = next(body for method, path, body in calls if path.endswith("/git/trees"))
    assert tree["base_tree"] == "b" * 40
    assert tree["tree"] == [{"path": "tests/new.test.py", "mode": "100644",
                             "type": "blob", "content": "assert True\n"}]
    commit = next(body for method, path, body in calls if path.endswith("/git/commits"))
    assert commit["parents"] == ["a" * 40]
    assert calls[-1][2] == {"sha": "d" * 40, "force": False}


def test_agent_stops_after_inapplicable_node(monkeypatch):
    run_id = new_run()

    async def no_skills(_agent, _context):
        return [], ""

    monkeypatch.setattr("app.agents.choose", no_skills)
    context = {
        "instruction": "Wrong passwords must show an error.",
        "analysis": {"playwright_applicable": False, "playwright_adapter": "",
                     "explanation": "No browser or HTTP service"},
    }
    result = asyncio.run(build_agent_graph("playwright").compile().ainvoke(
        {"run_id": run_id, "agent": "playwright", "context": context, "failed": False}
    ))
    assert result["failed"] is True
    events = get_events(run_id)
    assert any(item["node"] == "applicability" and item["success"] is False for item in events)
    assert not any(item["node"] == "plan_cases" for item in events)


def test_patch_paths_and_conflict(tmp_path: Path):
    assert _test_path("tests/login.test.ts")
    assert not _test_path("src/login.ts")
    assert not _test_path("../tests/login.test.ts")
    assert not _test_path(".github/workflows/test.yml")
    assert not _test_path(".GitHub/workflows/check.test.ts")
    patch = (
        "diff --git a/tests/login.test.ts b/tests/login.test.ts\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "--- /dev/null\n"
        "+++ b/tests/login.test.ts\n"
        "@@ -0,0 +1 @@\n"
        "+export const expected = true;\n"
    )
    results = [{"agent": "vitest", "patch": {"patch": patch, "test_files": ["tests/login.test.ts"]}}]
    assert _patch_paths(patch) == {"tests/login.test.ts"}
    with pytest.raises(ValueError, match="undeclared file path"):
        _patch_paths(patch.replace("--- /dev/null", "--- a/src/login.ts"))
    with pytest.raises(ValueError, match="regular, non-executable"):
        _patch_paths(patch.replace("new file mode 100644", "new file mode 120000"))
    accepted, rejected = _apply_patches(tmp_path, results)
    assert accepted == results and not rejected
    assert (tmp_path / "tests" / "login.test.ts").read_text() == "export const expected = true;\n"
    second_workspace = tmp_path / "second"
    second_workspace.mkdir()
    accepted, rejected = _apply_patches(second_workspace, results + results)
    assert accepted == results
    assert len(rejected) == 1 and "overlap" in rejected[0]["error"]


def test_interactive_preview_api_records_human_evidence(tmp_path: Path, monkeypatch):
    init_db()
    queued = []

    class FakeHandle:
        async def signal(self, _signal):
            queued.append("stop")

    class FakeClient:
        async def start_workflow(self, _workflow, *args, **kwargs):
            queued.append((args, kwargs))

        def get_workflow_handle(self, _workflow_id):
            return FakeHandle()

    async def connect(_address):
        return FakeClient()

    monkeypatch.setattr("app.api.Client.connect", connect)
    source = tmp_path / "source"
    source.mkdir()
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(id=run_id, repository="local/preview-api", pr_number=1,
                        installation_id=0, instruction="Test the preview route and human verdict.",
                        status="completed", context={
                            "source_path": str(source), "analysis": {
                                "app_start_command": "python3 -m http.server 8765 --bind 0.0.0.0",
                                "app_ready_url": "http://127.0.0.1:8765/health",
                            },
                        }))
    write_artifact(run_id, "outcome.json", json.dumps({"success": True, "agents": []}))
    preview = None
    try:
        with TestClient(app) as client:
            response = client.post(f"/api/runs/{run_id}/interactive-preview",
                                   json={"environment": {"APPLICATION_MODE": "local"},
                                         "setup_commands": ["echo prepared"]})
            assert response.status_code == 200, response.text
            preview = response.json()
            assert preview["port"] == 8765 and preview["ready_path"] == "/health"
            with session_scope() as session:
                options = session.get(InteractivePreviewOptions, preview["id"])
                assert options.environment == {"APPLICATION_MODE": "local"}
                assert options.setup_commands == ["echo prepared"]
            assert client.post(f"/api/runs/{run_id}/interactive-preview",
                               json={"environment": {"DATABASE_URL": "postgres://elsewhere"}}).status_code == 422
            response = client.post(f"/api/runs/{run_id}/interactive-preview/observations",
                                   json={"verdict": "failed", "steps": "Open the feature page",
                                         "expected": "The page loads", "actual": "The page is blank"})
            assert response.status_code == 200, response.text
            assert response.json()["report"]["queued"] is True
            response = client.get(f"/api/runs/{run_id}/interactive-preview")
            assert response.status_code == 200
            assert response.json()["observations"][0]["verdict"] == "failed"
            assert len(queued) == 2
            assert queued[1][1]["args"][1]["success"] is False
            response = client.post(f"/api/runs/{run_id}/interactive-preview/stop")
            assert response.status_code == 200
            assert queued[-1] == "stop"
    finally:
        with session_scope() as session:
            session.execute(delete(ManualObservation).where(ManualObservation.run_id == run_id))
            if preview:
                session.execute(delete(InteractivePreviewOptions).where(
                    InteractivePreviewOptions.preview_id == preview["id"]))
            session.execute(delete(InteractivePreview).where(InteractivePreview.run_id == run_id))
            session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
            session.execute(delete(Run).where(Run.id == run_id))


def test_postgres_image_follows_repository_extension_evidence(tmp_path: Path):
    from app.config import settings
    service = {"kind": "postgres", "required_extensions": []}
    assert _postgres_image(tmp_path, service) == settings.postgres_test_image
    migration = tmp_path / "backend" / "scripts" / "db" / "setup.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text("CREATE EXTENSION IF NOT EXISTS vector;", encoding="utf-8")
    assert _postgres_image(tmp_path, service) == settings.postgres_vector_image


def test_preview_readiness_rejects_missing_page(monkeypatch):
    def response(status):
        raise HTTPError("http://127.0.0.1:1234/", status, "status", {}, None)

    monkeypatch.setattr("app.interactive_preview.urlopen", lambda *_args, **_kwargs: response(404))
    assert _responds("http://127.0.0.1:1234/") is False
    monkeypatch.setattr("app.interactive_preview.urlopen", lambda *_args, **_kwargs: response(401))
    assert _responds("http://127.0.0.1:1234/") is True
    assert preview_defaults({"analysis": {"interactive_preview_command": "pnpm dev:all",
                                          "interactive_preview_ready_url": "http://localhost:4200/app",
                                          "interactive_preview_setup_commands": ["pnpm db:setup"]}}) == {
        "command": "pnpm dev:all", "port": 4200, "ready_path": "/app",
        "setup_commands": ["pnpm db:setup"],
    }


def test_context_includes_unchanged_importers_of_changed_module(tmp_path: Path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "sql_tool.py").write_text("", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "from tools.sql_tool import sql_db_query\n", encoding="utf-8",
    )
    files, snippets = selected_file_context(
        tmp_path, ["tools/sql_tool.py"], "Check that sql_db_query is removed",
    )
    assert "main.py" in files
    assert "sql_db_query" in snippets["main.py"]


def test_preflight_hydrates_cited_repository_evidence(tmp_path: Path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "database.ts").write_text(
        'export const databaseUrl = process.env.DATABASE_URL; // postgres\n',
        encoding="utf-8",
    )
    snippets = {}
    hydrate_evidence(tmp_path, ["backend/database.ts"], snippets,
                     ["backend/database.ts", "missing.ts"])
    assert "postgres" in snippets["backend/database.ts"]
    assert "missing.ts" not in snippets


def test_ci_postgres_url_requires_disposable_service(monkeypatch, tmp_path: Path):
    (tmp_path / "package.json").write_text(
        '{"scripts":{"db:push":"pnpm --dir backend db:push"}}', encoding="utf-8",
    )
    (tmp_path / "src.ts").write_text("export const value = 1;", encoding="utf-8")
    calls = []

    async def fake_parse(_prompt, _payload, schema):
        calls.append(schema)
        if schema is ServiceSchema:
            return ServiceSchema(kind="postgres", evidence_files=["package.json"],
                               connection_environment_keys=["DATABASE_URL"],
                               setup_commands=["pnpm db:push"])
        return FrameworkAnalysis.model_validate({
            "application_framework": "Node", "language": "TypeScript",
            "package_manager": "pnpm", "native_test_framework": "Vitest",
            "has_native_test_framework": True, "install_commands": ["pnpm install"],
            "existing_test_commands": ["pnpm test"], "app_start_command": "",
            "app_ready_url": "", "explanation": "CI uses PostgreSQL",
            "test_environment": [{"key": "DATABASE_URL",
                                  "value": "postgres://tester:test@localhost:5432/app",
                                  "evidence_file": ".github/workflows/ci.yml"}],
            "services": [],
            "playwright": {"mode": "unsupported", "target": "none",
                           "evidence_files": [], "reason": "No browser target",
                           "covered_behaviors": []},
            "vitest": {"mode": "direct", "target": "javascript_module",
                       "evidence_files": ["src.ts"], "reason": "Module target",
                       "covered_behaviors": ["Module value"]},
        })

    monkeypatch.setattr("app.inspection.parse", fake_parse)
    analysis, _, _ = asyncio.run(analyze_repository(
        "owner/repo", [{"filename": "src.ts"}], tmp_path,
        "The module must return its value",
    ))
    assert calls == [FrameworkAnalysis, ServiceSchema]
    assert analysis.services[0].connection_environment_keys == ["DATABASE_URL"]


def test_report_receives_actual_failure_log(tmp_path: Path):
    run_id = new_run()
    relative = write_artifact(run_id, "logs/builtin-1.log",
                              "AssertionError: expected project denial\n")
    excerpts = evidence_artifact_excerpts(run_id, [
        {"detail": {"log": relative}},
    ])
    assert "AssertionError" in excerpts[relative]
    from_error = evidence_artifact_excerpts(run_id, [
        {"detail": {"error": f"Test setup failed; see {relative}"}},
    ])
    assert "AssertionError" in from_error[relative]


def test_builtin_patch_separates_modified_tests_from_existing_suite(monkeypatch):
    run_id = new_run()
    patch = (
        "diff --git a/tests/test_new.py b/tests/test_new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/test_new.py\n"
        "@@ -0,0 +1 @@\n"
        "+assert True\n"
    )

    async def fake_parse(_prompt, _payload, _schema):
        return GeneratedPatch(
            patch=patch, explanation="Runs both tests",
            test_files=["tests/test_new.py", "tests/test_existing.py"],
            test_command="python -m unittest discover -s tests",
        )

    monkeypatch.setattr("app.agents.parse", fake_parse)
    state = {
        "run_id": run_id, "agent": "builtin",
        "plan": {"cases": [
            {"title": "New case", "patch_location": "tests/test_new.py"},
            {"title": "Existing suite", "patch_location": "tests/test_existing.py"},
        ]},
        "context": {"instruction": "Run new and existing tests",
                    "files": ["tests/test_existing.py"], "snippets": {}, "analysis": {}},
    }
    result = asyncio.run(generate_patch(state))
    assert result["patch"]["test_files"] == ["tests/test_new.py"]
    assert result["plan"]["cases"][1]["patch_location"] == "tests/test_existing.py"


def test_patch_records_unimplementable_case_as_uncovered(monkeypatch):
    run_id = new_run()
    patch = (
        "diff --git a/tests/test_new.py b/tests/test_new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/test_new.py\n"
        "@@ -0,0 +1 @@\n"
        "+assert True\n"
    )

    async def fake_parse(_prompt, _payload, _schema):
        return GeneratedPatch(
            patch=patch, explanation="The request case lacks fixtures",
            test_files=["tests/test_new.py"], test_command="python -m unittest",
            uncovered_cases=[{"title": "Authenticated request", "reason": "No login fixture exists"}],
        )

    monkeypatch.setattr("app.agents.parse", fake_parse)
    state = {
        "run_id": run_id, "agent": "builtin",
        "plan": {"cases": [
            {"title": "Pure function", "patch_location": "tests/test_new.py"},
            {"title": "Authenticated request", "patch_location": "tests/test_http.py"},
        ], "uncovered": []},
        "context": {"instruction": "Check pure and request paths", "files": [],
                    "snippets": {}, "analysis": {}},
    }
    result = asyncio.run(generate_patch(state))
    assert [case["title"] for case in result["plan"]["cases"]] == ["Pure function"]
    assert "No login fixture exists" in result["plan"]["uncovered"][0]


def test_patch_generator_repairs_rejected_unified_diff(monkeypatch):
    run_id = new_run()
    valid = (
        "diff --git a/tests/test_new.py b/tests/test_new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/test_new.py\n"
        "@@ -0,0 +1 @@\n"
        "+assert True\n"
    )
    attempts = []

    async def fake_parse(_prompt, payload, _schema):
        attempts.append(payload.get("correction"))
        return GeneratedPatch(
            patch=valid if len(attempts) > 1 else valid.replace("--- /dev/null\n", ""),
            explanation="Syntax repaired", test_files=["tests/test_new.py"],
            test_command="python -m unittest",
        )

    monkeypatch.setattr("app.agents.parse", fake_parse)
    result = asyncio.run(generate_patch({
        "run_id": run_id, "agent": "builtin",
        "plan": {"cases": [{"title": "New case", "patch_location": "tests/test_new.py"}],
                 "uncovered": []},
        "context": {"instruction": "Check the new case", "files": [],
                    "snippets": {}, "analysis": {}},
    }))
    assert result["patch"]["patch"] == valid
    assert len(attempts) == 2
    assert "header" in attempts[1]


def test_publication_commits_validated_checkout_files(monkeypatch):
    run_id = new_run()
    context = {"head_sha": "a" * 40}
    set_run(run_id, head_sha="a" * 40, context=context, check_run_id=123)
    from app.artifacts import run_dir
    workspace = run_dir(run_id) / "workspace" / "tests"
    workspace.mkdir(parents=True)
    (workspace / "review.test.py").write_text("assert 2 + 2 == 4\n", encoding="utf-8")
    patch = (
        "diff --git a/tests/review.test.py b/tests/review.test.py\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "--- /dev/null\n"
        "+++ b/tests/review.test.py\n"
        "@@ -0,0 +1 @@\n"
        "+assert 2 + 2 == 4\n"
    )
    committed = []

    class FakeGitHub:
        async def installation_token(self, _installation):
            return "token"

        async def commit_test_files(self, _repo, _number, expected, files, _token, _run):
            committed.append((expected, files))
            return "b" * 40

        async def complete_check(self, *_args):
            return None

    monkeypatch.setattr("app.publication.GitHubApp", FakeGitHub)
    result = asyncio.run(publish_generated_tests(run_id, context, [{
        "agent": "builtin", "patch": {"patch": patch,
                                      "test_files": ["tests/review.test.py"]},
    }]))
    assert result["success"] and result["committed"]
    assert committed == [("a" * 40, {"tests/review.test.py": "assert 2 + 2 == 4\n"})]
    assert get_run(run_id)["head_sha"] == "b" * 40
    assert get_run(run_id)["context"]["published_test_files"] == ["tests/review.test.py"]


def test_specialist_selection_respects_executable_targets():
    analysis = {
        "has_native_test_framework": True, "app_start_command": "",
        "app_ready_url": "", "playwright": {
            "mode": "unsupported", "target": "none", "evidence_files": [],
            "reason": "No isolated service",
        }, "vitest": {
            "mode": "direct", "target": "javascript_module",
            "evidence_files": ["src/example.ts"], "reason": "Importable module",
            "covered_behaviors": ["The module returns the requested value"],
        },
    }
    enabled, inactive = select_agents(analysis, ["src/example.ts"], [])
    assert enabled == ["builtin", "playwright", "vitest"]
    assert "playwright" in inactive
    run_id = new_run()
    context = {"analysis": analysis, "files": ["src/example.ts"],
               "selected_frameworks": [], "enabled_agents": enabled,
               "instruction": "A member sees only approved projects"}
    set_run(run_id, context=context)
    skipped = asyncio.run(build_agent_graph("playwright").compile().ainvoke(
        {"run_id": run_id, "agent": "playwright", "context": context, "failed": False},
    ))
    assert skipped["skipped"] is True and not skipped.get("failed")
    assert skipped["skip_reason"] == "No isolated service"
    nodes = {node["id"]: node for node in definition(run_id)["nodes"]}
    assert nodes["playwright.applicability"]["status"] == "not_selected"
    assert nodes["playwright.select_skills"]["status"] == "not_selected"
    analysis["has_native_test_framework"] = False
    enabled, inactive = select_agents(analysis, ["src/example.ts"], ["playwright"])
    assert enabled == ["builtin", "playwright", "vitest"]
    assert inactive["vitest"] == "Not selected by user"
    context["selected_frameworks"] = ["playwright"]
    failed = asyncio.run(build_agent_graph("playwright").compile().ainvoke(
        {"run_id": run_id, "agent": "playwright", "context": context, "failed": False},
    ))
    assert failed["failed"] is True
    assert "No isolated service" in failed["error"]


def test_database_service_requires_repository_evidence_and_supplies_test_url():
    analysis = FrameworkAnalysis.model_validate({
        "application_framework": "Node", "language": "TypeScript", "package_manager": "npm",
        "native_test_framework": "Vitest", "has_native_test_framework": True,
        "install_commands": ["npm ci"], "existing_test_commands": ["npm test"],
        "app_start_command": "", "app_ready_url": "", "test_environment": [],
        "services": [{"kind": "postgres", "evidence_files": ["ci.yml"],
                      "connection_environment_keys": ["DATABASE_URL"],
                      "setup_commands": ["npm run db:push"]}],
        "playwright": {"mode": "unsupported", "target": "none", "evidence_files": [],
                       "reason": "No server", "covered_behaviors": []},
        "vitest": {"mode": "direct", "target": "javascript_module",
                   "evidence_files": ["src/access.ts"], "reason": "Importable module",
                   "covered_behaviors": ["Access decision is correct"],
                   "service_dependencies": ["postgres"]},
        "explanation": "CI starts PostgreSQL",
    })
    files = ["ci.yml", "src/access.ts"]
    validate_services(analysis, files, {"ci.yml": "services: postgres:16"})
    valid, _ = select_agents(analysis.model_dump(), files, [])
    assert valid == ["builtin", "playwright", "vitest"]
    url = _test_environment(analysis.model_dump(), "a test password")["DATABASE_URL"]
    assert url == "postgresql://ardberg:a%20test%20password@postgres:5432/ardberg_test"
    analysis.services[0].setup_commands = ["pnpm db:migrate"]
    prefer_fresh_database_schema_push(analysis, {
        "package.json": '{"scripts":{"db:push":"pnpm --dir backend db:push"}}',
    })
    assert analysis.services[0].setup_commands == ["pnpm db:push"]
    with pytest.raises(ValueError, match="evidence"):
        validate_services(analysis, files, {"ci.yml": "services: redis:7"})


def test_builtin_command_selection_keeps_generated_tests(monkeypatch):
    async def choose_only_existing(_prompt, _payload, _schema):
        return CommandSelection(commands=["pnpm test:backend"], rationale="Distinct suite")

    monkeypatch.setattr("app.agents.parse", choose_only_existing)
    state = {
        "agent": "builtin", "patch": {"test_command": "pnpm exec vitest run added.test.ts"},
        "context": {"analysis": {"existing_test_commands": ["pnpm test:backend",
                                                              "pnpm test:angular"]},
                    "instruction": "Run the backend tests for this change",
                    "snippets": {"package.json": "{}"}},
    }
    chosen = asyncio.run(select_commands(state))
    assert chosen["commands"] == ["pnpm exec vitest run added.test.ts", "pnpm test:backend"]


def test_command_selection_keeps_explicitly_requested_suites(monkeypatch):
    async def incomplete_choice(_prompt, _payload, _schema):
        return CommandSelection(commands=["pnpm test:backend"],
                                rationale="Backend tests selected")

    monkeypatch.setattr("app.agents.parse", incomplete_choice)
    state = {
        "agent": "builtin", "patch": {"test_command": "pnpm exec vitest run access.test.ts"},
        "context": {"analysis": {"existing_test_commands": [
            "pnpm test", "pnpm test:backend", "pnpm test:angular",
        ]}, "instruction": "Run backend and Angular test suites",
                    "snippets": {"package.json": "{}"}},
    }
    chosen = asyncio.run(select_commands(state))
    assert chosen["commands"] == [
        "pnpm exec vitest run access.test.ts", "pnpm test:backend", "pnpm test:angular",
    ]


def test_instruction_specific_feasibility_omits_unexecutable_specialist(monkeypatch):
    analysis = FrameworkAnalysis.model_validate({
        "application_framework": "TypeScript", "language": "TypeScript",
        "package_manager": "npm", "native_test_framework": "Vitest",
        "has_native_test_framework": True, "install_commands": ["npm ci"],
        "existing_test_commands": ["npm test"], "app_start_command": "",
        "app_ready_url": "", "test_environment": [], "explanation": "Unit-only",
        "playwright": {"mode": "unsupported", "target": "none", "evidence_files": [],
                       "reason": "No service", "covered_behaviors": []},
        "vitest": {"mode": "direct", "target": "javascript_module",
                   "evidence_files": ["src/access.ts"], "reason": "Importable",
                   "covered_behaviors": ["Access decision is correct"]},
    })

    async def no_cases(_prompt, _payload, _schema):
        return PlanSchema(cases=[], uncovered=["Needs an authenticated service"],
                        rationale="No runnable browser flow")

    monkeypatch.setattr("app.inspection.parse", no_cases)
    absent = asyncio.run(specialist_feasibility(
        "playwright", analysis, [], {}, "A member must see its organization",
    ))
    assert absent is None

    async def one_case(_prompt, _payload, _schema):
        return PlanSchema(cases=[CaseSchema(
            title="Member access", behavior="Member sees an organization",
            expected="Organization is returned", source="User instruction",
            framework="Vitest", patch_location="src/access.ardberg.vitest.test.ts",
        )], uncovered=[], rationale="Importable module")

    monkeypatch.setattr("app.inspection.parse", one_case)
    feasible = asyncio.run(specialist_feasibility(
        "vitest", analysis, [], {}, "A member must see its organization",
    ))
    assert feasible and feasible["cases"][0]["patch_location"].endswith(".test.ts")


def test_structured_vitest_failure_is_preserved(tmp_path: Path):
    report = tmp_path / ".ardberg-results"
    report.mkdir()
    (report / "vitest-1.json").write_text(
        '{"numTotalTests":2,"numPassedTests":1,"numFailedTests":1,'
        '"numPendingTests":0,"testResults":[{"assertionResults":['
        '{"fullName":"access denied","status":"failed","failureMessages":["expected 403"]}]}]}',
        encoding="utf-8",
    )
    parsed = _structured_result(tmp_path, "vitest-1")
    assert parsed["failed"] == 1
    assert parsed["failures"][0]["title"] == "access denied"


def test_native_package_script_vitest_gets_structured_reporter(tmp_path: Path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"test:backend": "pnpm --dir backend test", "test:angular": "pnpm --dir frontend test"},
    }), encoding="utf-8")
    (tmp_path / "backend" / "package.json").write_text(json.dumps({
        "scripts": {"test": "vitest run"},
    }), encoding="utf-8")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text(json.dumps({
        "scripts": {"test": "ng test --watch=false"},
    }), encoding="utf-8")
    commands = _suite_commands([{
        "agent": "builtin", "commands": ["pnpm test:backend", "pnpm test:angular"],
    }], tmp_path)
    assert "--reporter=json" in commands[0][1]
    assert "--outputFile=/workspace/.ardberg-results/builtin-1.json" in commands[0][1]
    assert commands[1][1] == "pnpm test:angular"


def test_report_rejects_status_mismatch():
    draft = ReportDraft(
        markdown="The suite passed [event:7].", evidence_event_ids=[7],
        uncovered_requests=[],
        claims=[EvidenceClaim(text="The suite passed", event_ids=[7],
                              expected_status="passed")],
    )
    errors = _check_claim_links("run-id", draft, [{
        "id": 7, "status": "failed", "stage": "execution", "node": "vitest-1",
    }], {})
    assert any("event 7 is failed" in error for error in errors)


def test_flow_uses_real_events():
    run_id = new_run()
    emit(run_id, "preflight", "running", node="fetch_repository")
    emit(run_id, "preflight", "passed", node="fetch_repository", success=True)
    emit(run_id, "agents", "running", agent="builtin", node="select_skills")
    flow = definition(run_id)
    by_id = {node["id"]: node for node in flow["nodes"]}
    assert by_id["preflight.fetch_repository"]["status"] == "passed"
    assert by_id["builtin.select_skills"]["status"] == "running"
    assert by_id["executor.postgres_ready"]["status"] == "not_selected"
    assert flow["run"]["instruction"].startswith("Submitting a wrong password")
    assert any(edge["label"] == "shared context + instruction" for edge in flow["edges"])


def test_api_health_and_reference_flow():
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        result = client.get("/api/flow")
        assert result.status_code == 200
        assert len(result.json()["nodes"]) > 20
        assert client.get("/api/runs").status_code == 200


def test_webhook_reuses_instruction_for_new_head_and_deduplicates_delivery(monkeypatch):
    init_db()
    repository = "owner/" + uuid4().hex[:12]
    instruction = "An approved project member must see only their organization."
    with session_scope() as session:
        session.add(PullRequestSettings(repository=repository, pr_number=1,
                                        instruction=instruction, selected_frameworks=["vitest"]))
    starts = []

    async def fake_start(repo, number, request, frameworks, installation, expected_head=None):
        starts.append((repo, request, frameworks, expected_head))
        return str(uuid4())

    monkeypatch.setattr("app.api.verify_webhook", lambda *_args: True)
    monkeypatch.setattr("app.api._start_run", fake_start)
    published = []
    class FakeGitHub:
        async def installation_token(self, _installation):
            return "token"

        async def git_commit(self, _repository, _sha, _token):
            if published and _sha == "c" * 40:
                return {"message": "test: add Ardberg generated cases\n\nArdberg-Run: " + published[0]}
            return {"message": "Ordinary contributor commit"}

    monkeypatch.setattr("app.api.GitHubApp", FakeGitHub)
    with TestClient(app) as client:
        for index, head in enumerate(("a" * 40, "b" * 40)):
            payload = {
                "action": "synchronize", "repository": {"full_name": repository},
                "number": 1, "pull_request": {"head": {"sha": head}},
                "installation": {"id": 900001, "account": {"id": 900002, "login": "owner"},
                                 "target_type": "User"},
                "sender": {"id": 900003, "login": "author"},
            }
            headers = {"x-github-event": "pull_request", "x-github-delivery": f"{repository}-{index}"}
            response = client.post("/webhooks/github", content=json.dumps(payload), headers=headers)
            assert response.status_code == 200 and response.json()["accepted"]
            duplicate = client.post("/webhooks/github", content=json.dumps(payload), headers=headers)
            assert duplicate.json()["reason"] == "duplicate webhook delivery"
        published_id = str(uuid4())
        with session_scope() as session:
            session.add(Run(id=published_id, repository=repository, pr_number=1,
                            installation_id=900001, head_sha="a" * 40,
                            instruction=instruction))
        published.append(published_id)
        payload["pull_request"]["head"]["sha"] = "c" * 40
        headers["x-github-delivery"] = f"{repository}-generated"
        generated = client.post("/webhooks/github", content=json.dumps(payload), headers=headers)
        assert generated.status_code == 200
        assert generated.json()["reason"] == "generated-test commit for an existing run"
    assert [item[-1] for item in starts] == ["a" * 40, "b" * 40]
    assert all(item[1] == instruction and item[2] == ["vitest"] for item in starts)


def test_report_agent_persists_evidence_linked_analysis(monkeypatch):
    run_id = new_run()
    evidence_id = emit(run_id, "execution", "passed", agent="executor",
                       node="builtin-1", success=True)

    async def report_model(_prompt, _payload, _schema):
        if _schema is ReportVerification:
            return ReportVerification(supported=True, reason="The event records a passed suite")
        return ReportDraft(markdown=f"The test passed [event:{evidence_id}].",
                           evidence_event_ids=[evidence_id], uncovered_requests=[],
                           claims=[EvidenceClaim(text="The test passed", event_ids=[evidence_id],
                                                 expected_status="passed")])

    monkeypatch.setattr("app.report.parse", report_model)
    markdown = asyncio.run(generate_report(run_id, {"success": True}))
    assert markdown == f"The test passed [event:{evidence_id}]."
    events = get_events(run_id)
    assert any(item["node"] == "verify_evidence" and item["success"] is True
               for item in events)
    assert get_run(run_id)["report"] == markdown


def test_report_agent_revises_invalid_evidence_once(monkeypatch):
    run_id = new_run()
    evidence_id = emit(run_id, "execution", "failed", agent="executor",
                       node="builtin-1", success=False)
    attempts = []

    async def report_model(_prompt, payload, _schema):
        if _schema is ReportVerification:
            return ReportVerification(supported=True, reason="The event records a failed suite")
        attempts.append(payload.get("correction"))
        event_id = 999999 if len(attempts) == 1 else evidence_id
        return ReportDraft(markdown=f"Finding [event:{event_id}].",
                           evidence_event_ids=[event_id], uncovered_requests=[],
                           claims=[EvidenceClaim(text="Finding", event_ids=[event_id],
                                                 expected_status="failed")])

    monkeypatch.setattr("app.report.parse", report_model)
    markdown = asyncio.run(generate_report(run_id, {"success": False}))
    assert markdown.endswith(f"[event:{evidence_id}].")
    assert len(attempts) == 2 and attempts[1]
    events = get_events(run_id)
    assert any(item["node"] == "verify_evidence" and item["status"] == "retrying"
               for item in events)


def test_older_run_cannot_replace_newer_pr_comment(monkeypatch):
    init_db()
    repository = "owner/" + uuid4().hex[:10]
    old_id, new_id = str(uuid4()), str(uuid4())
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        session.add(PullRequestSettings(
            repository=repository, pr_number=42, instruction="Test the login behavior.",
            selected_frameworks=[], comment_id=123,
        ))
        for run_id, created in [(old_id, now - timedelta(minutes=1)), (new_id, now)]:
            session.add(Run(
                id=run_id, repository=repository, pr_number=42, installation_id=1,
                head_sha="a" * 40, check_run_id=101, created_at=created,
                instruction="Test the login behavior.",
            ))

    comments = []

    class FakeGitHub:
        async def installation_token(self, _id):
            return "test-token"

        async def complete_check(self, *_args):
            return None

        async def upsert_pr_comment(self, *_args):
            comments.append(_args)
            return 456

    monkeypatch.setattr("app.report.GitHubApp", FakeGitHub)
    asyncio.run(publish_report(old_id, "Old report [event:1].", False))
    assert comments == []
    assert get_events(old_id)[-1]["detail"]["superseded_by_newer_run"] is True
    asyncio.run(publish_report(new_id, "New report [event:2].", True))
    assert len(comments) == 1
    assert comments[0][-1] == 123
