"""Run focused behavior probes on isolated pinned PR and base checkouts."""

import asyncio
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .artifacts import artifact_path, write_artifact
from .config import settings
from .events import emit
from .runner import (_command, _environment_args, _postgres_image,
                     _run_setup_with_repair, _start_postgres, _test_environment)


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
                command, remote, timeout=min(settings.test_timeout_seconds, 120),
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
            if code == 0:
                path = artifact_path(run_id, f"{prefix}.png")
                copied, _ = await _command("docker", "cp", f"{container}:{screenshot}",
                                           str(path), timeout=30)
                if copied == 0:
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
    suffix = uuid4().hex[:8]
    container = f"ardberg-review-{revision}-{suffix}"
    network = f"{container}-net"
    database = f"{container}-postgres"
    started = False
    network_started = False
    database_started = False
    observations = []
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
        for index, command in enumerate(analysis.get("install_commands", [])):
            await _run_setup_with_repair(
                run_id, stage="runtime", agent="runner", node=f"{revision}_install_{index + 1}",
                container=container, command=command,
                log_prefix=f"review/runtime/{revision}/install-{index + 1}",
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
        for index, command in enumerate(
            command for service in services for command in service.get("setup_commands", [])
        ):
            await _run_setup_with_repair(
                run_id, stage="runtime", agent="runner", node=f"{revision}_service_{index + 1}",
                container=container, command=command,
                log_prefix=f"review/runtime/{revision}/service-{index + 1}",
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
        if needs_app:
            parsed = urlsplit(ready)
            if not start or parsed.scheme not in {"http", "https"} or parsed.hostname not in {
                "localhost", "127.0.0.1", "0.0.0.0"
            } or not parsed.port:
                app_error = "No evidenced local start command and readiness URL"
            else:
                environment.update({"HOST": "0.0.0.0", "HOSTNAME": "0.0.0.0"})
                code, output = await _command(
                    "docker", "exec", "-d", *_environment_args(environment), container,
                    "sh", "-lc", f"({start}) > /tmp/ardberg-app.log 2>&1", timeout=30,
                )
                if code:
                    app_error = f"Application startup failed: {output}"
                else:
                    origin = f"{parsed.scheme}://127.0.0.1:{parsed.port}"
                    ready_path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
                    for _ in range(40):
                        code, status = await _command("docker", "exec", container,
                                                       "curl", "-sS", "-o", "/dev/null",
                                                       "-w", "%{http_code}",
                                                       origin + ready_path, timeout=10)
                        response_code = int(status.strip()) if status.strip().isdigit() else 0
                        if code == 0 and (200 <= response_code < 400 or response_code in {401, 403}):
                            break
                        await asyncio.sleep(1)
                    else:
                        code, log = await _command("docker", "exec", container, "cat",
                                                   "/tmp/ardberg-app.log", timeout=20)
                        path = write_artifact(run_id, f"review/runtime/{revision}/application.log", log)
                        app_error = f"Application did not become ready; see {path}"
                        origin = ""
        emit(run_id, "runtime", "passed", agent="runner", node=f"{revision}_setup",
             success=True, detail={"origin": origin, "app_error": app_error,
                                   "browser_error": browser_error})
        for probe in probes:
            blocker = (browser_error if probe.kind == "browser" else "") or (
                app_error if probe.kind in {"http", "browser"} else ""
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
                code, log = await _command("docker", "exec", container, "cat",
                                           "/tmp/ardberg-app.log", timeout=20)
                if code == 0 and log.strip():
                    path = write_artifact(run_id, f"review/runtime/{revision}/application.log", log)
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
    base_probes = [item for item in plan.probes if item.compare_base]
    base = await _run_revision(run_id, context, "base", base_probes)
    head = await _run_revision(run_id, context, "head", plan.probes)
    result = {"observations": base + head,
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
