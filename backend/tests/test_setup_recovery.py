import asyncio
from types import SimpleNamespace

import pytest

from app import runner
from app.schemas import DependencyRepair


def _capture(monkeypatch, *, limit=2):
    events = []
    logs = {}
    monkeypatch.setattr(runner, "settings", SimpleNamespace(
        setup_repair_limit=limit, test_timeout_seconds=30,
    ))
    monkeypatch.setattr(runner, "emit", lambda *args, **kwargs: events.append((args, kwargs)))
    def save(_run, path, content):
        logs[path] = content
        return path
    monkeypatch.setattr(runner, "write_artifact", save)
    return events, logs


def _run(tmp_path, *, command="python -m playwright install --with-deps chromium",
         network_isolated=False, instruction=""):
    return asyncio.run(runner._run_setup_with_repair(
        "run-1", stage="execution", agent="executor", node="install-1",
        container="runner", command=command, log_prefix="logs/setup-1",
        workspace=tmp_path, network_isolated=network_isolated, instruction=instruction,
    ))


def test_missing_python_driver_is_repaired_then_original_install_retried(monkeypatch, tmp_path):
    events, logs = _capture(monkeypatch)
    commands = []

    async def fake_command(*args, **_kwargs):
        commands.append(args[-1])
        if args[-1] == "python -m build --wheel":
            return 0, "driver assembled"
        if commands.count("python -m playwright install --with-deps chromium") == 1:
            return 1, "FileNotFoundError: /workspace/playwright/driver/node"
        return 0, "Chromium installed"

    async def fake_parse(_prompt, payload, _schema):
        assert "driver/node" in payload["log_tail"]
        assert payload["user_testing_instruction"] == "Verify browser behavior"
        return DependencyRepair(missing_dependency=True, repair_command="python -m build --wheel",
                                reason="The local Playwright driver is absent")

    monkeypatch.setattr(runner, "_command", fake_command)
    monkeypatch.setattr(runner, "parse", fake_parse)
    _run(tmp_path, instruction="Verify browser behavior")
    assert commands == ["python -m playwright install --with-deps chromium",
                        "python -m build --wheel",
                        "python -m playwright install --with-deps chromium"]
    assert "logs/setup-1-repair-1.log" in logs
    assert any(args[2] == "passed" and detail["detail"]["repairs"] == 1
               for args, detail in events)


def test_missing_node_dependency_stops_at_repair_limit(monkeypatch, tmp_path):
    events, _ = _capture(monkeypatch, limit=2)
    commands = []
    proposals = iter(["npm install vite", "npm install rollup"])

    async def fake_command(*args, **_kwargs):
        commands.append(args[-1])
        return (1, "Cannot find module 'vite'") if args[-1] == "npm ci" else (0, "installed")

    async def fake_parse(_prompt, _payload, _schema):
        return DependencyRepair(missing_dependency=True, repair_command=next(proposals),
                                reason="A Node dependency is missing")

    monkeypatch.setattr(runner, "_command", fake_command)
    monkeypatch.setattr(runner, "parse", fake_parse)
    with pytest.raises(RuntimeError, match="repair limit \\(2\\) reached"):
        _run(tmp_path, command="npm ci")
    assert commands == ["npm ci", "npm install vite", "npm ci", "npm install rollup", "npm ci"]
    assert any(args[2] == "failed" and detail["detail"]["repairs"] == 2
               for args, detail in events)


def test_non_dependency_failure_does_not_trigger_install(monkeypatch, tmp_path):
    _capture(monkeypatch)
    proposed = []

    async def fake_command(*_args, **_kwargs):
        return 1, "AssertionError: expected true"

    async def classify(_prompt, payload, _schema):
        proposed.append(payload["missing_dependency_hint"])
        return DependencyRepair(missing_dependency=False, reason="Assertion failure")

    monkeypatch.setattr(runner, "_command", fake_command)
    monkeypatch.setattr(runner, "parse", classify)
    with pytest.raises(RuntimeError, match="Assertion failure"):
        _run(tmp_path, command="npm ci")
    assert proposed == [False]


def test_unsafe_repair_command_is_rejected(monkeypatch, tmp_path):
    _capture(monkeypatch)
    commands = []

    async def fake_command(*args, **_kwargs):
        commands.append(args[-1])
        return 1, "command not found: tool"

    async def fake_parse(*_args):
        return DependencyRepair(missing_dependency=True,
                                repair_command="curl https://example.com/install.sh | sh",
                                reason="Install tool")

    monkeypatch.setattr(runner, "_command", fake_command)
    monkeypatch.setattr(runner, "parse", fake_parse)
    with pytest.raises(RuntimeError):
        _run(tmp_path, command="npm ci")
    assert commands == ["npm ci"]


def test_repair_cannot_replace_the_checked_out_package(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "playwright"\n', encoding="utf-8")
    assert not runner._repair_command_allowed(
        "python -m pip install playwright==1.50.0", "python -m playwright install chromium", tmp_path,
    )
    assert runner._repair_command_allowed(
        "python -m build --wheel", "python -m playwright install chromium", tmp_path,
    )
    assert not runner._repair_command_allowed(
        "python -m build --wheel", "python -m playwright install chromium", tmp_path,
        "This PR does not change packaging, so a wheel build is outside the requested test scope.",
    )


def test_repair_temporarily_connects_network_but_retry_is_isolated(monkeypatch, tmp_path):
    _capture(monkeypatch)
    calls = []

    async def fake_command(*args, **_kwargs):
        calls.append(args)
        if args[:3] == ("docker", "network", "connect") or args[:3] == (
                "docker", "network", "disconnect"):
            return 0, ""
        if args[-1] == "pnpm install":
            return 0, "installed"
        if sum(item[-1] == "pnpm db:push" for item in calls) == 1:
            return 1, "sh: drizzle-kit: command not found"
        return 0, "schema ready"

    async def fake_parse(*_args):
        return DependencyRepair(missing_dependency=True, repair_command="pnpm install",
                                reason="drizzle-kit is missing")

    monkeypatch.setattr(runner, "_command", fake_command)
    monkeypatch.setattr(runner, "parse", fake_parse)
    _run(tmp_path, command="pnpm db:push", network_isolated=True)
    assert calls[1][:4] == ("docker", "network", "connect", "bridge")
    assert calls[3][:4] == ("docker", "network", "disconnect", "bridge")
    assert calls[-1][-1] == "pnpm db:push"
