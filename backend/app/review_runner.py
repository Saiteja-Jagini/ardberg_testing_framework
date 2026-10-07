"""Run focused behavior probes on isolated pinned PR and base checkouts."""

import asyncio
import json
import os
import secrets
import shlex
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .artifacts import artifact_path, write_artifact
from .config import settings
from .events import emit
from .runner import (_command, _environment_args, _postgres_image,
                     _repair_command_allowed, _run_setup_with_repair, _start_postgres, _test_environment)


def _setup_key(command: str) -> tuple[str, ...]:
    """Recognize equivalent install spellings without rewriting the executed command."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return (command,)
    if len(parts) > 2 and parts[0] == "timeout" and parts[1].rstrip("smhd").isdigit():
        parts = parts[2:]
    if parts[:3] in (["python", "-m", "pip"], ["python3", "-m", "pip"]):
        parts = ["pip", *parts[3:]]
    if parts[:3] in (["python", "-m", "playwright"], ["python3", "-m", "playwright"]):
        parts = ["playwright", *parts[3:]]
    return tuple(parts)


def _setup_phases(analysis: dict, source: Path) -> tuple[list[str], list[str]]:
    """Install dependencies before isolation and retain fixture setup for service startup."""
    installs = list(analysis.get("install_commands", []))
    seen = {_setup_key(command) for command in installs}
    fixtures = []
    for command in analysis.get("runtime_setup_commands", []):
        key = _setup_key(command)
        if key in seen:
            continue
        seen.add(key)
        dependency = shlex.join(key)
        if (key[:2] == ("playwright", "install") or
                _repair_command_allowed(dependency, "", source)):
            installs.append(command)
        else:
            fixtures.append(command)
    return installs, fixtures


async def _start_processes(run_id: str, container: str, revision: str,
                           processes: list[dict], target_url: str,
                           environment: dict[str, str], prefix: str) -> tuple[str, str]:
    """Start only required processes and verify each endpoint before dependent probes."""
    for index, process in enumerate(processes):
        parsed = urlsplit(process["ready_url"])
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in
                {"localhost", "127.0.0.1", "0.0.0.0"} or not parsed.port):
            return "", "No evidenced local start command and readiness URL"
        log_file = f"/tmp/ardberg-app-{index}.log"
        code, output = await _command(
            "docker", "exec", "-d", *_environment_args(environment), container,
            "sh", "-lc", f"({process['command']}) > {log_file} 2>&1", timeout=30)
        if code:
            return "", f"Application startup failed: {output}"
        ready = f"{parsed.scheme}://127.0.0.1:{parsed.port}{parsed.path or '/'}"
        if parsed.query:
            ready += "?" + parsed.query
        for _ in range(60):
            code, status = await _command(
                "docker", "exec", container, "curl", "--max-time", "2", "-sS",
                "-o", "/dev/null", "-w", "%{http_code}", ready, timeout=5)
            response = int(status.strip()) if status.strip().isdigit() else 0
            if code == 0 and (200 <= response < 400 or response in {401, 403}):
                emit(run_id, "runtime", "passed", agent="runner",
                     node=f"{revision}_ready_{index}", success=True,
                     detail={"command": process["command"], "ready_url": ready,
                             "response_status": response})
                break
            await asyncio.sleep(1)
        else:
            _, log = await _command("docker", "exec", container, "cat", log_file, timeout=20)
            path = write_artifact(run_id, f"{prefix}/application-{index}.log", log)
            return "", f"Application did not become ready; see {path}"
    parsed = urlsplit(target_url)
    if not processes or not parsed.port or parsed.hostname not in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return "", "No evidenced application process and target URL"
    return f"{parsed.scheme}://127.0.0.1:{parsed.port}", ""


async def _observe(run_id: str, container: str, revision: str, probe,
                   origin: str, environment: dict[str, str]) -> dict:
    name = probe.id
    detail = {"probe_id": name, "expectation_id": probe.expectation_id,
              "revision": revision, "kind": probe.kind}
    emit(run_id, "runtime", "running", agent="runner", node=name, detail=detail)
    prefix = f"review/runtime/{revision}/{name}"
    try:
        if probe.kind == "script":
            extension = {"python": "py", "node": "cjs", "sh": "sh"}[probe.interpreter]
            local = artifact_path(run_id, f"{prefix}.{extension}")
            write_artifact(run_id, f"{prefix}.{extension}", probe.script)
            remote = f"/workspace/.ardberg-review/{name}.{extension}"
            created, output = await _command("docker", "exec", container, "mkdir", "-p",
                                             "/workspace/.ardberg-review", timeout=20)
            if created:
                raise RuntimeError(f"Could not prepare probe directory: {output}")
            copied, output = await _command("docker", "cp", str(local),
                                            f"{container}:{remote}", timeout=20)
            if copied:
                raise RuntimeError(f"Could not copy probe: {output}")
            command = {"python": "python", "node": "node", "sh": "sh"}[probe.interpreter]
            script_environment = {**environment, "PYTHONPATH": "/workspace:/workspace/src",
                                  "NODE_PATH": "/workspace/node_modules"}
            code, output = await _command(
                "docker", "exec", *_environment_args(script_environment), container,
                command, *(["-P"] if probe.interpreter == "python" else []), remote,
                timeout=min(settings.test_timeout_seconds, 120),
            )
        elif probe.kind == "http":
            if not origin:
                raise RuntimeError("No evidenced application start command and readiness URL")
            url = origin + probe.path
            args = ["docker", "exec", *_environment_args(environment), container,
                    "curl", "-sS", "-i", "--max-time", "25", "-X", probe.method]
            if probe.method == "POST":
                args.extend(["-H", "Content-Type: application/json", "--data", probe.body])
            code, output = await _command(*args, url, timeout=35)
        else:
            if not origin:
                raise RuntimeError("No evidenced application start command and readiness URL")
            screenshot = f"/tmp/ardberg-{name}.png"
            data = {"url": origin + probe.path, "actions": [item.model_dump() for item in probe.actions],
                    "screenshot": screenshot}
            relative = f"{prefix}.json"
            local = artifact_path(run_id, relative)
            write_artifact(run_id, relative, json.dumps(data))
            copied, output = await _command("docker", "cp", str(local),
                                            f"{container}:/tmp/ardberg-{name}.json", timeout=20)
            if copied:
                raise RuntimeError(f"Could not copy browser probe: {output}")
            code, output = await _command("docker", "exec", container, "node",
                                          "/tmp/ardberg-runtime-probe.cjs",
                                          f"/tmp/ardberg-{name}.json", timeout=90)
            # A failed interaction can still produce useful visual evidence.
            path = artifact_path(run_id, f"{prefix}.png")
            copied, _ = await _command("docker", "cp", f"{container}:{screenshot}",
                                       str(path), timeout=30)
            if copied == 0:
                write_artifact(run_id, f"{prefix}.png", path.read_bytes())
                detail["screenshot"] = f"{prefix}.png"
        log = write_artifact(run_id, f"{prefix}.log", output)
        detail.update({"status": "observed" if code == 0 else "failed",
                       "exit_code": code, "log": log,
                       "output_excerpt": output[-12000:]})
        emit(run_id, "runtime", "passed" if code == 0 else "failed",
             agent="runner", node=name, success=code == 0, detail=detail)
        return detail
    except Exception as exc:
        detail.update({"status": "blocked", "error": str(exc)})
        emit(run_id, "runtime", "not_selected", agent="runner", node=name, detail=detail)
        return detail


async def _run_revision(run_id: str, context: dict, revision: str, probes: list) -> list[dict]:
    if not probes:
        return []
    source_key = "source_path" if revision == "head" else "base_source_path"
    source_text = context.get(source_key)
    source = Path(source_text).resolve() if source_text else None
    if not source or not source.is_dir():
        return [{"probe_id": item.id, "expectation_id": item.expectation_id,
                 "revision": revision, "kind": item.kind, "status": "blocked",
                 "error": "Pinned source snapshot unavailable"} for item in probes]
    analysis = context["analysis"]
    setup_prefix = f"review/runtime/{revision}/setup-{context.get('runtime_environment_id', 'default')}"
    suffix = uuid4().hex[:8]
    container = f"ardberg-review-{revision}-{suffix}"
    network = f"{container}-net"
    database = f"{container}-postgres"
    started = False
    network_started = False
    database_started = False
    observations = []
    processes = []
    try:
        emit(run_id, "runtime", "running", agent="runner", node=f"{revision}_setup")
        code, output = await _command("docker", "run", "-d", "--name", container,
                                      "--network", "bridge", "--workdir", "/workspace",
                                      "--memory", settings.runner_memory,
                                      "--cpus", settings.runner_cpus,
                                      "--pids-limit", str(settings.runner_pids_limit),
                                      "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                                      settings.docker_image, "sleep", "infinity", timeout=60)
        if code:
            raise RuntimeError(f"Could not create runtime container: {output}")
        started = True
        code, output = await _command("docker", "cp", str(source) + os.sep + ".",
                                      f"{container}:/workspace", timeout=180)
        if code:
            raise RuntimeError(f"Could not copy pinned source: {output}")
        install_commands, runtime_fixtures = _setup_phases(analysis, source)
        for index, command in enumerate(install_commands):
            await _run_setup_with_repair(
                run_id, stage="runtime", agent="runner", node=f"{revision}_install_{index + 1}",
                container=container, command=command,
                log_prefix=f"{setup_prefix}/install-{index + 1}",
                workspace=source,
                environment={"NPM_CONFIG_REGISTRY": settings.npm_registry_url,
                             "PIP_INDEX_URL": settings.pip_index_url},
                instruction=context.get("instruction", ""),
            )
        browser_probes = [item for item in probes if item.kind == "browser"]
        browser_error = ""
        if browser_probes:
            code, output = await _command("docker", "exec", "-e",
                                          f"NPM_CONFIG_REGISTRY={settings.npm_registry_url}",
                                          container, "sh", "-lc",
                                          "npm install --no-save --prefix /tmp/ardberg-probe @playwright/test@1.63.0",
                                          timeout=180)
            if code:
                browser_error = f"Browser probe dependency unavailable: {output[-1000:]}"
            else:
                # Repository installers can garbage-collect the image's browser cache.
                # Verify/install the helper's own exact browser before network isolation.
                code, output = await _command("docker", "exec", container, "node",
                    "/tmp/ardberg-probe/node_modules/@playwright/test/cli.js", "install", "chromium", timeout=180)
                path = write_artifact(run_id, f"{setup_prefix}/browser-install.log", output)
                emit(run_id, "runtime", "artifact", agent="runner", node=f"{revision}_browser",
                     detail={"path": path, "exit_code": code})
                if code:
                    browser_error = f"Browser helper executable unavailable; see {path}"
                resource = Path(__file__).parent / "resources" / "runtime_probe.cjs"
                code, output = await _command("docker", "cp", str(resource),
                                              f"{container}:/tmp/ardberg-runtime-probe.cjs",
                                              timeout=30)
                if code:
                    browser_error = f"Could not install browser probe: {output}"
        code, output = await _command("docker", "network", "create", "--internal", network,
                                      timeout=30)
        if code:
            raise RuntimeError(f"Could not create private runtime network: {output}")
        network_started = True
        code, output = await _command("docker", "network", "connect", network, container,
                                      timeout=30)
        if code:
            raise RuntimeError(f"Could not connect runtime container: {output}")
        code, output = await _command("docker", "network", "disconnect", "bridge", container,
                                      timeout=30)
        if code:
            raise RuntimeError(f"Could not isolate runtime container: {output}")
        services = analysis.get("services", [])
        if services:
            if len(services) != 1 or services[0]["kind"] != "postgres":
                raise RuntimeError("Runtime review supports one disposable PostgreSQL service")
            password = secrets.token_urlsafe(24)
            database_started = True
            await _start_postgres(run_id, network, database, password,
                                  stage="runtime", agent="runner",
                                  image=_postgres_image(source, services[0]),
                                  node=f"{revision}_postgres")
        else:
            password = None
        environment = _test_environment(analysis, password)
        setup_commands = list(dict.fromkeys([
            *(command for service in services for command in service.get("setup_commands", [])),
            *runtime_fixtures,
        ]))
        for index, command in enumerate(setup_commands):
            await _run_setup_with_repair(
                run_id, stage="runtime", agent="runner", node=f"{revision}_service_{index + 1}",
                container=container, command=command,
                log_prefix=f"{setup_prefix}/service-{index + 1}",
                workspace=source, environment=environment, network_isolated=True,
                instruction=context.get("instruction", ""),
            )
        needs_app = any(item.kind in {"http", "browser"} for item in probes)
        start = (analysis.get("interactive_preview_command") or
                 analysis.get("app_start_command") or "").strip()
        ready = (analysis.get("interactive_preview_ready_url") or
                 analysis.get("app_ready_url") or "").strip()
        origin = ""
        app_error = ""
        processes = analysis.get("runtime_processes", [])
        if needs_app or processes:
            if "runtime_processes" not in analysis and start and ready:
                processes = [{"command": start, "ready_url": ready}]
            environment.update({"HOST": "0.0.0.0", "HOSTNAME": "0.0.0.0"})
            origin, app_error = await _start_processes(
                run_id, container, revision, processes,
                analysis.get("runtime_target_url") or ready, environment, setup_prefix)
        emit(run_id, "runtime", "failed" if app_error else "passed", agent="runner", node=f"{revision}_setup",
             success=not bool(app_error), detail={"origin": origin, "app_error": app_error,
                                   "browser_error": browser_error})
        for probe in probes:
            blocker = (browser_error if probe.kind == "browser" else "") or (
                app_error if probe.kind in {"http", "browser"} or processes else ""
            )
            if blocker:
                detail = {"probe_id": probe.id, "expectation_id": probe.expectation_id,
                          "revision": revision, "kind": probe.kind,
                          "status": "blocked", "error": blocker}
                emit(run_id, "runtime", "not_selected", agent="runner", node=probe.id,
                     detail=detail)
                observations.append(detail)
            else:
                observations.append(await _observe(run_id, container, revision, probe,
                                                   origin, environment))
        return observations
    except Exception as exc:
        emit(run_id, "runtime", "failed", agent="runner", node=f"{revision}_setup",
             success=False, detail={"error": str(exc)})
        return [{"probe_id": item.id, "expectation_id": item.expectation_id,
                 "revision": revision, "kind": item.kind, "status": "blocked",
                 "error": str(exc)} for item in probes]
    finally:
        if started:
            try:
                for index in range(len(processes)):
                    code, log = await _command("docker", "exec", container, "cat",
                                               f"/tmp/ardberg-app-{index}.log", timeout=20)
                    if code == 0 and log.strip():
                        path = write_artifact(run_id, f"{setup_prefix}/application-{index}.log", log)
                        emit(run_id, "runtime", "artifact", agent="runner", node=f"{revision}_setup",
                             detail={"path": path})
            except (OSError, TimeoutError):
                pass
        if database_started:
            try:
                await _command("docker", "rm", "-f", database, timeout=20)
            except (OSError, TimeoutError):
                pass
        if started:
            try:
                await _command("docker", "rm", "-f", container, timeout=20)
            except (OSError, TimeoutError):
                pass
        if network_started:
            try:
                await _command("docker", "network", "rm", network, timeout=20)
            except (OSError, TimeoutError):
                pass


async def run_runtime_probes(run_id: str, context: dict, plan) -> dict:
    base, head = [], []
    environments = {item.id: item for item in plan.environments}
    inherited = {_setup_key(command) for command in context["analysis"].get("install_commands", [])}
    equivalents, aliases = {}, {}
    for item in plan.environments:
        definition = item.model_dump(exclude={"id", "rationale", "evidence_paths"})
        definition["setup_commands"] = [key for command in item.setup_commands
                                        if (key := _setup_key(command)) not in inherited]
        signature = json.dumps(definition, sort_keys=True)
        aliases[item.id] = equivalents.setdefault(signature, item.id)
    groups = {}
    for probe in plan.probes:
        # Older plans still isolate direct code calls from application setup.
        key = probe.environment_id or ("code" if probe.kind == "script" else "application")
        key = aliases.get(key, key)
        groups.setdefault(key, []).append(probe)
    for key, probes in groups.items():
        analysis = dict(context["analysis"])
        environment = environments.get(key)
        if environment:
            unused_keys = {k for s in analysis.get("services", []) if s["kind"] not in environment.services
                           for k in s.get("connection_environment_keys", [])}
            analysis.update({
                "services": [s for s in analysis.get("services", []) if s["kind"] in environment.services],
                "runtime_setup_commands": environment.setup_commands,
                "runtime_processes": [p.model_dump() for p in environment.processes],
                "runtime_target_url": environment.target_url,
                "test_environment": [entry for entry in analysis.get("test_environment", [])
                                     if entry["key"] not in unused_keys],
            })
        elif key == "code":
            unused_keys = {k for s in analysis.get("services", []) for k in s.get("connection_environment_keys", [])}
            analysis.update(services=[], runtime_processes=[], runtime_setup_commands=[],
                            test_environment=[e for e in analysis.get("test_environment", []) if e["key"] not in unused_keys])
        else:
            analysis["runtime_setup_commands"] = analysis.get("interactive_preview_setup_commands", [])
        scoped = {**context, "analysis": analysis, "runtime_environment_id": key}
        head_result, base_result = await asyncio.gather(
            _run_revision(run_id, scoped, "head", probes),
            _run_revision(run_id, scoped, "base", [p for p in probes if p.compare_base]),
        )
        head.extend(head_result)
        base.extend(base_result)
    result = {"revision_pins": {"head_sha": context.get("head_sha"), "base_sha": context.get("base_sha")},
              "observations": base + head,
              "probes_planned": len(plan.probes),
              "probes_observed": sum(item["status"] == "observed" for item in head),
              "probes_blocked": sum(item["status"] == "blocked" for item in head),
              "probes_failed": sum(item["status"] == "failed" for item in head),
              "base_probes_unavailable": sum(item["status"] != "observed" for item in base)}
    path = write_artifact(run_id, "review/runtime-observations.json",
                          json.dumps(result, indent=2))
    emit(run_id, "runtime", "artifact", agent="runner", node="collect",
         detail={"path": path})
    result["artifact"] = path
    return result
