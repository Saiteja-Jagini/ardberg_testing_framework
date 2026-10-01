import asyncio
import json
import os
import re
import secrets
from pathlib import Path
import shlex
import shutil
import subprocess
from urllib.parse import quote
from uuid import uuid4

from .artifacts import run_dir, write_artifact
from .config import settings
from .events import emit


OUTPUT_DIRECTORIES = ("test-results", "playwright-report", "coverage", "reports", ".vitest", ".ardberg-results")


def _collect_output_artifacts(run_id: str, workspace: Path) -> list[str]:
    saved = []
    total = 0
    for directory in OUTPUT_DIRECTORIES:
        root = workspace / directory
        if not root.is_dir() or root.is_symlink():
            continue
        for item in root.rglob("*"):
            if not item.is_file() or item.is_symlink():
                continue
            size = item.stat().st_size
            if size > 20_000_000 or total + size > 100_000_000:
                continue
            relative = "evidence/" + item.relative_to(workspace).as_posix()
            saved.append(write_artifact(run_id, relative, item.read_bytes()))
            total += size
    return saved


async def _command(*args: str, timeout: int = 120) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise TimeoutError(f"Runner command exceeded its {timeout}s limit") from exc
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, output.decode("utf-8", errors="replace")[-1_000_000:]


def _apply_patches(workspace: Path, results: list[dict]):
    seen = set()
    for result in results:
        generated = result.get("patch") or {}
        patch = generated.get("patch", "")
        if not patch:
            continue
        paths = {path.casefold() for path in generated["test_files"]}
        if seen & paths:
            raise ValueError(f"Generated test patches overlap: {', '.join(sorted(seen & paths))}")
        seen.update(paths)
        applied = subprocess.run(["git", "apply", "-"], cwd=workspace, input=patch.encode("utf-8"),
                                 capture_output=True, timeout=30)
        if applied.returncode:
            raise ValueError(f"Cannot apply generated patch: {applied.stderr.decode(errors='replace').strip()[:1000]}")


def _uses_vitest(command: str, workspace: Path, depth: int = 0,
                 visited: set[tuple[str, str]] | None = None) -> bool:
    if re.search(r"\bvitest\s+run\b", command):
        return True
    if depth >= 5:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if tokens and tokens[0] == "corepack":
        tokens = tokens[1:]
    if not tokens or tokens[0] not in {"npm", "pnpm", "yarn", "bun"}:
        return False
    tool = tokens.pop(0)
    directory = workspace
    for flag in ("--dir", "--cwd", "-C", "--prefix"):
        if flag in tokens:
            position = tokens.index(flag)
            if position + 1 >= len(tokens):
                return False
            directory = (workspace / tokens[position + 1]).resolve()
            del tokens[position:position + 2]
    if workspace.resolve() not in (directory, *directory.parents):
        return False
    if not tokens:
        return False
    if tokens[0] == "run":
        tokens = tokens[1:]
    elif tool == "npm" and tokens[0] not in {"test", "t"}:
        return False
    script_name = tokens[0] if tokens else ""
    if script_name == "t" and tool == "npm":
        script_name = "test"
    if not script_name:
        return False
    key = (str(directory), script_name)
    visited = visited or set()
    if key in visited:
        return False
    visited.add(key)
    manifest = directory / "package.json"
    if not manifest.is_file() or manifest.is_symlink():
        return False
    try:
        script = json.loads(manifest.read_text(encoding="utf-8")).get("scripts", {}).get(script_name, "")
    except (ValueError, OSError):
        return False
    return bool(script) and _uses_vitest(script, directory, depth + 1, visited)


def _suite_commands(results: list[dict], workspace: Path) -> list[tuple[str, str]]:
    suites = []
    seen = set()
    for result in results:
        for index, command in enumerate(result.get("commands", [])):
            if command in seen:
                continue
            seen.add(command)
            name = f"{result['agent']}-{index + 1}"
            if result["agent"] == "playwright":
                if "playwright" not in command:
                    raise ValueError("Playwright agent command does not invoke Playwright")
                command = (
                    f"PLAYWRIGHT_JSON_OUTPUT_FILE=/workspace/.ardberg-results/{name}.json "
                    + command + f" --trace on --reporter=json --output=/workspace/test-results/{name}"
                )
            elif result["agent"] == "vitest" or _uses_vitest(command, workspace):
                if result["agent"] == "vitest" and "vitest" not in command:
                    raise ValueError("Vitest agent command does not invoke Vitest")
                forwarding = " --" if re.match(r"^(?:corepack\s+)?npm\s+(?:run\s+)?", command) else ""
                command += f"{forwarding} --reporter=json --outputFile=/workspace/.ardberg-results/{name}.json"
            suites.append((name, command))
    return suites


def _structured_result(workspace: Path, name: str) -> dict | None:
    path = workspace / ".ardberg-results" / f"{name}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if "numTotalTests" in data:
        failures = [
            {"title": case.get("fullName") or case.get("title"), "message": case.get("failureMessages", [])}
            for suite in data.get("testResults", [])
            for case in suite.get("assertionResults", [])
            if case.get("status") == "failed"
        ]
        return {
            "total": data.get("numTotalTests", 0),
            "passed": data.get("numPassedTests", 0),
            "failed": data.get("numFailedTests", 0),
            "pending": data.get("numPendingTests", 0),
            "failures": failures[:30],
            "path": f"evidence/.ardberg-results/{name}.json",
        }
    outcomes = []
    def walk(suites):
        for suite in suites:
            for spec in suite.get("specs", []):
                for test in spec.get("tests", []):
                    for result in test.get("results", []):
                        outcomes.append({"title": spec.get("title"), "status": result.get("status"),
                                         "error": result.get("error")})
            walk(suite.get("suites", []))
    walk(data.get("suites", []))
    return {
        "total": len(outcomes), "passed": sum(x["status"] == "passed" for x in outcomes),
        "failed": sum(x["status"] in {"failed", "timedOut"} for x in outcomes),
        "pending": sum(x["status"] == "skipped" for x in outcomes),
        "failures": [x for x in outcomes if x["status"] in {"failed", "timedOut"}][:30],
        "path": f"evidence/.ardberg-results/{name}.json",
    }


def _test_environment(analysis: dict, postgres_password: str | None = None) -> dict[str, str]:
    values = {entry["key"]: entry["value"] for entry in analysis.get("test_environment", [])}
    if postgres_password:
        url = "postgresql://ardberg:" + quote(postgres_password, safe="") + "@postgres:5432/ardberg_test"
        for service in analysis.get("services", []):
            if service["kind"] == "postgres":
                for key in service["connection_environment_keys"]:
                    values[key] = url
    return values


def _environment_args(values: dict[str, str]) -> list[str]:
    return [part for key, value in values.items() for part in ("-e", f"{key}={value}")]


async def _start_postgres(run_id: str, network: str, name: str,
                          password: str) -> None:
    code, output = await _command(
        "docker", "run", "-d", "--name", name, "--network", network,
        "--network-alias", "postgres", "--memory", "1g", "--cpus", "1",
        "--pids-limit", "128", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "postgres",
        "-e", "POSTGRES_USER=ardberg", "-e", "POSTGRES_DB=ardberg_test",
        "-e", f"POSTGRES_PASSWORD={password}",
        settings.postgres_test_image, timeout=60,
    )
    if code:
        raise RuntimeError(f"Could not start isolated PostgreSQL: {output}")
    for _ in range(40):
        code, _ = await _command(
            "docker", "exec", name, "pg_isready", "-U", "ardberg",
            "-d", "ardberg_test", timeout=5,
        )
        if code == 0:
            emit(run_id, "execution", "passed", agent="executor",
                 node="postgres_ready", success=True)
            return
        await asyncio.sleep(1)
    _, logs = await _command("docker", "logs", name, timeout=10)
    path = write_artifact(run_id, "logs/postgres-startup.log", logs)
    raise RuntimeError(f"Isolated PostgreSQL did not become ready; see {path}")


async def _copy_runner_outputs(container: str, output_root: Path) -> None:
    for directory in OUTPUT_DIRECTORIES:
        destination = output_root / directory
        destination.mkdir(parents=True, exist_ok=True)
        await _command(
            "docker", "cp", f"{container}:/workspace/{directory}/.",
            str(destination), timeout=120,
        )


async def execute_tests(run_id: str, context: dict, results: list[dict]) -> dict:
    source = Path(context["source_path"])
    root = run_dir(run_id).resolve()
    workspace = (root / "workspace").resolve()
    output_root = (root / "runner-output").resolve()
    if root not in workspace.parents or root not in output_root.parents:
        raise ValueError("Runner paths must stay inside the run directory")
    if workspace.exists():
        shutil.rmtree(workspace)
    if output_root.exists():
        shutil.rmtree(output_root)
    shutil.copytree(source, workspace)
    output_root.mkdir()
    emit(run_id, "execution", "running", agent="executor", node="apply_patches")
    try:
        _apply_patches(workspace, results)
    except Exception as exc:
        emit(run_id, "execution", "failed", agent="executor", node="apply_patches",
             success=False, detail={"error": str(exc)})
        return {"success": False, "error": str(exc), "suites": []}
    emit(run_id, "execution", "passed", agent="executor", node="apply_patches", success=True)

    container = f"ardberg-{run_id[:8]}-{uuid4().hex[:6]}"
    network = f"{container}-private"
    database_container = f"{container}-postgres"
    database_started = False
    emit(run_id, "execution", "running", agent="executor", node="start_runner")
    try:
        code, output = await _command(
            "docker", "run", "-d", "--name", container, "--network", "bridge",
            "--workdir", "/workspace", "--memory", settings.runner_memory,
            "--cpus", settings.runner_cpus,
            "--pids-limit", str(settings.runner_pids_limit),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            settings.docker_image, "sleep", "infinity", timeout=60,
        )
        if code:
            raise RuntimeError(output)
        code, output = await _command(
            "docker", "cp", str(workspace.resolve()) + os.sep + ".",
            f"{container}:/workspace", timeout=180,
        )
        if code:
            raise RuntimeError(f"Could not copy the pinned checkout into the runner: {output}")
        emit(run_id, "execution", "passed", agent="executor", node="start_runner", success=True,
             detail={"container": container})
        analysis = context["analysis"]
        emit(run_id, "execution", "running", agent="executor", node="setup")
        install_commands = list(analysis["install_commands"])
        if not analysis["has_native_test_framework"]:
            selected = set(context["selected_frameworks"])
            packages = []
            if "playwright" in selected:
                packages.append("@playwright/test@1.63.0")
            if "vitest" in selected:
                packages.append("vitest")
            if packages:
                command = "npm install --no-save " + " ".join(packages)
                if "playwright" in selected:
                    command += " && npx playwright install"
                install_commands.append(command)
        for index, command in enumerate(install_commands):
            code, output = await _command(
                "docker", "exec", "-e", f"NPM_CONFIG_REGISTRY={settings.npm_registry_url}",
                "-e", f"PIP_INDEX_URL={settings.pip_index_url}",
                container, "sh", "-lc", command,
                                          timeout=settings.test_timeout_seconds)
            path = write_artifact(run_id, f"logs/setup-{index + 1}.log", output)
            emit(run_id, "execution", "artifact", agent="executor", node="setup",
                 detail={"path": path, "command": command})
            if code:
                raise RuntimeError(f"Install command failed (exit {code}); see {path}")
        code, output = await _command("docker", "network", "create", "--internal", network, timeout=30)
        if code:
            raise RuntimeError(f"Could not create private test network: {output}")
        code, output = await _command("docker", "network", "connect", network, container, timeout=30)
        if code:
            raise RuntimeError(f"Could not attach private test network: {output}")
        code, output = await _command("docker", "network", "disconnect", "bridge", container, timeout=30)
        if code:
            raise RuntimeError(f"Could not remove public network before tests: {output}")
        postgres_password = None
        if analysis.get("services"):
            if len(analysis["services"]) != 1 or analysis["services"][0]["kind"] != "postgres":
                raise ValueError("Runner supports one isolated PostgreSQL test service")
            postgres_password = secrets.token_urlsafe(24)
            database_started = True
            emit(run_id, "execution", "running", agent="executor", node="postgres_ready")
            await _start_postgres(run_id, network, database_container, postgres_password)
        runtime_environment = _test_environment(analysis, postgres_password)
        for index, command in enumerate(
            command for service in analysis.get("services", [])
            for command in service.get("setup_commands", [])
        ):
            code, output = await _command(
                "docker", "exec", *_environment_args(runtime_environment),
                container, "sh", "-lc", command, timeout=settings.test_timeout_seconds,
            )
            path = write_artifact(run_id, f"logs/service-setup-{index + 1}.log", output)
            emit(run_id, "execution", "artifact", agent="executor", node="setup",
                 detail={"path": path, "command": command})
            if code:
                raise RuntimeError(f"Test service setup command failed (exit {code}); see {path}")
        code, output = await _command(
            "docker", "exec", container, "mkdir", "-p", "/workspace/.ardberg-results", timeout=20,
        )
        if code:
            raise RuntimeError(f"Could not create report directory: {output}")
        playwright_enabled = any(
            result["agent"] == "playwright" for result in results
        )
        playwright_decision = analysis.get("playwright", {}) if playwright_enabled else {}
        start_command = (
            playwright_decision.get("setup_command") or analysis["app_start_command"]
        ).strip() if playwright_enabled else ""
        if start_command:
            app_env = _environment_args(runtime_environment)
            code, output = await _command(
                "docker", "exec", "-d", *app_env, container, "sh", "-lc",
                f"exec {start_command} > /tmp/ardberg-app.log 2>&1", timeout=30,
            )
            if code:
                raise RuntimeError(f"Application startup failed: {output}")
            ready_url = (
                playwright_decision.get("ready_url") or analysis.get("app_ready_url", "")
            ).strip()
            if ready_url:
                check = "for i in $(seq 1 40); do curl -fsS " + shlex.quote(ready_url) + " >/dev/null && exit 0; sleep 1; done; exit 1"
                code, output = await _command("docker", "exec", container, "sh", "-lc", check, timeout=50)
                if code:
                    raise RuntimeError(f"Application did not become ready at {ready_url}: {output}")
        emit(run_id, "execution", "passed", agent="executor", node="setup", success=True)

        suites = _suite_commands(results, workspace)
        if not suites:
            raise RuntimeError("No executable test commands were produced")

        async def run_suite(name: str, command: str) -> dict:
            emit(run_id, "execution", "running", agent="executor", node=name,
                 detail={"command": command})
            try:
                test_env = _environment_args(runtime_environment)
                code, output = await _command("docker", "exec", *test_env,
                                              container, "sh", "-lc", command,
                                              timeout=settings.test_timeout_seconds)
                path = write_artifact(run_id, f"logs/{name}.log", output)
                report_path = output_root / ".ardberg-results"
                report_path.mkdir(exist_ok=True)
                await _command(
                    "docker", "cp",
                    f"{container}:/workspace/.ardberg-results/{name}.json",
                    str(report_path / f"{name}.json"), timeout=30,
                )
                structured = _structured_result(output_root, name)
                passed = code == 0 and (structured is None or structured["failed"] == 0)
                emit(run_id, "execution", "passed" if passed else "failed", agent="executor",
                     node=name, success=passed,
                     detail={"exit_code": code, "command": command, "log": path,
                             "structured_result": structured})
                return {"name": name, "success": passed, "exit_code": code, "log": path,
                        "structured_result": structured}
            except asyncio.CancelledError:
                emit(run_id, "execution", "cancelled", agent="executor", node=name,
                     detail={"reason": "Global fail-fast"})
                raise
            except Exception as exc:
                emit(run_id, "execution", "failed", agent="executor", node=name,
                     success=False, detail={"error": str(exc)})
                return {"name": name, "success": False, "error": str(exc)}

        tasks = [asyncio.create_task(run_suite(name, command)) for name, command in suites]
        findings = []
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for item in done:
                result = item.result()
                findings.append(result)
                if not result["success"]:
                    for sibling in pending:
                        sibling.cancel()
                    await _command("docker", "stop", "-t", "2", container, timeout=15)
                    await asyncio.gather(*pending, return_exceptions=True)
                    emit(run_id, "execution", "running", agent="executor", node="collect")
                    await _copy_runner_outputs(container, output_root)
                    artifacts = _collect_output_artifacts(run_id, output_root)
                    emit(run_id, "execution", "artifact", agent="executor", node="collect",
                         detail={"paths": artifacts})
                    emit(run_id, "execution", "passed", agent="executor", node="collect",
                         success=True, detail={"paths": artifacts})
                    return {"success": False, "error": f"Suite {result['name']} failed",
                            "suites": findings, "artifacts": artifacts}
        emit(run_id, "execution", "running", agent="executor", node="collect")
        await _copy_runner_outputs(container, output_root)
        artifacts = _collect_output_artifacts(run_id, output_root)
        emit(run_id, "execution", "passed", agent="executor", node="collect", success=True,
             detail={"suites": findings, "artifacts": artifacts})
        return {"success": True, "suites": findings, "artifacts": artifacts}
    except Exception as exc:
        emit(run_id, "execution", "failed", agent="executor", node="setup", success=False,
             detail={"error": str(exc)})
        return {"success": False, "error": str(exc), "suites": []}
    finally:
        if database_started:
            try:
                await _command("docker", "rm", "-f", database_container, timeout=15)
            except Exception:
                pass
        try:
            await _command("docker", "rm", "-f", container, timeout=15)
        except Exception:
            pass
        try:
            await _command("docker", "network", "rm", network, timeout=15)
        except Exception:
            pass
