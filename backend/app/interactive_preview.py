"""Disposable, loopback-only application previews for hands-on PR review."""

import asyncio
import json
import os
import re
import secrets
import shutil
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from sqlalchemy import select
    from .artifacts import run_dir, write_artifact
    from .config import settings
    from .db import session_scope
    from .events import emit, get_run
    from .models import InteractivePreview, InteractivePreviewOptions, ManualObservation
    from .runner import _command, _environment_args, _postgres_image, _start_postgres, _test_environment


PREVIEW_LIFETIME = timedelta(hours=2)


def preview_defaults(context: dict) -> dict:
    analysis = context.get("analysis") or {}
    command = (analysis.get("interactive_preview_command") or
               analysis.get("app_start_command") or "").strip()
    ready_url = (analysis.get("interactive_preview_ready_url") or
                 analysis.get("app_ready_url") or "").strip()
    parsed = urlparse(ready_url)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 3000)
    except ValueError:
        port = 3000
    return {
        "command": command,
        "port": port,
        "ready_path": (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else ""),
        "setup_commands": (analysis.get("interactive_preview_setup_commands") or
                           [command for service in analysis.get("services", [])
                            for command in service.get("setup_commands", [])]),
    }


def preview_record(preview_id: str) -> dict:
    with session_scope() as session:
        item = session.get(InteractivePreview, preview_id)
        if item is None:
            raise LookupError("Unknown interactive preview")
        return {
            "id": item.id, "run_id": item.run_id, "status": item.status,
            "command": item.command, "port": item.port, "ready_path": item.ready_path,
            "url": item.url, "error": item.error,
            "created_at": item.created_at.isoformat(),
        }


def preview_for_run(run_id: str) -> dict:
    with session_scope() as session:
        latest = session.scalar(select(InteractivePreview).where(
            InteractivePreview.run_id == run_id,
        ).order_by(InteractivePreview.created_at.desc()))
        observations = session.scalars(select(ManualObservation).where(
            ManualObservation.run_id == run_id,
        ).order_by(ManualObservation.created_at)).all()
        items = [{"id": item.id, "preview_id": item.preview_id,
                  "verdict": item.verdict, "steps": item.steps,
                  "expected": item.expected, "actual": item.actual,
                  "created_at": item.created_at.isoformat()} for item in observations]
    return {"defaults": preview_defaults(get_run(run_id)["context"] or {}),
            "session": preview_record(latest.id) if latest else None,
            "observations": items}


def _update(preview_id: str, **values) -> None:
    with session_scope() as session:
        item = session.get(InteractivePreview, preview_id)
        if item is None:
            raise LookupError("Unknown interactive preview")
        for key, value in values.items():
            setattr(item, key, value)


def _responds(url: str) -> bool:
    try:
        with urlopen(url, timeout=2) as response:
            return 200 <= response.status < 400
    except HTTPError as exc:
        return exc.code in {401, 403}
    except (URLError, TimeoutError, OSError):
        return False


async def launch_preview(preview_id: str) -> dict:
    record = preview_record(preview_id)
    with session_scope() as session:
        options = session.get(InteractivePreviewOptions, preview_id)
        extra_environment = dict(options.environment) if options else {}
        custom_setup = list(options.setup_commands) if options and options.setup_commands is not None else None
    run_id = record["run_id"]
    source = Path(get_run(run_id)["context"]["source_path"]).resolve()
    if not source.is_dir():
        raise RuntimeError("The pinned PR source snapshot is missing")
    root = run_dir(run_id).resolve()
    workspace = (root / "interactive" / preview_id / "workspace").resolve()
    if root not in workspace.parents:
        raise ValueError("Preview workspace must stay inside the run directory")
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, workspace)
    suffix = preview_id.replace("-", "")[:12]
    container = f"ardberg-preview-{suffix}"
    network = f"{container}-net"
    database_container = f"{container}-postgres"
    _update(preview_id, status="starting", container=container, network=network,
            database_container=database_container)
    emit(run_id, "manual", "running", agent="preview", node="start",
         detail={"preview_id": preview_id, "command": record["command"]})
    try:
        code, output = await _command("docker", "network", "create", network, timeout=30)
        if code:
            raise RuntimeError(f"Could not create preview network: {output}")
        code, output = await _command(
            "docker", "run", "-d", "--name", container, "--network", network,
            "-p", f"127.0.0.1::{record['port']}", "--workdir", "/workspace",
            "--memory", settings.preview_memory, "--cpus", settings.runner_cpus,
            "--pids-limit", str(settings.runner_pids_limit), "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", settings.docker_image,
            "sleep", "infinity", timeout=60,
        )
        if code:
            raise RuntimeError(f"Could not start preview container: {output}")
        code, output = await _command("docker", "cp", str(workspace) + os.sep + ".",
                                      f"{container}:/workspace", timeout=180)
        if code:
            raise RuntimeError(f"Could not copy pinned PR source: {output}")
        analysis = get_run(run_id)["context"]["analysis"]
        for index, command in enumerate(analysis.get("install_commands", [])):
            code, output = await _command(
                "docker", "exec", "-e", f"NPM_CONFIG_REGISTRY={settings.npm_registry_url}",
                "-e", f"PIP_INDEX_URL={settings.pip_index_url}", container,
                "sh", "-lc", command, timeout=settings.test_timeout_seconds,
            )
            path = write_artifact(run_id, f"manual/{preview_id}/install-{index + 1}.log", output)
            if code:
                raise RuntimeError(f"Preview install failed (exit {code}); see {path}")
        postgres_password = None
        services = analysis.get("services") or []
        if services:
            if len(services) != 1 or services[0]["kind"] != "postgres":
                raise RuntimeError("Preview supports one isolated PostgreSQL service")
            postgres_password = secrets.token_urlsafe(24)
            await _start_postgres(run_id, network, database_container, postgres_password,
                                  stage="manual", agent="preview",
                                  image=_postgres_image(source, services[0]))
        environment = _test_environment(analysis, postgres_password)
        environment.update({"HOST": "0.0.0.0", "HOSTNAME": "0.0.0.0"})
        environment.update(extra_environment)
        setup_commands = custom_setup if custom_setup is not None else preview_defaults(
            {"analysis": analysis})["setup_commands"]
        for index, command in enumerate(setup_commands):
            code, output = await _command("docker", "exec", *_environment_args(environment),
                                          container, "sh", "-lc", command,
                                          timeout=settings.test_timeout_seconds)
            path = write_artifact(run_id, f"manual/{preview_id}/service-{index + 1}.log", output)
            if code:
                raise RuntimeError(f"Preview service setup failed (exit {code}); see {path}")
        code, output = await _command(
            "docker", "exec", "-d", *_environment_args(environment), container,
            "sh", "-lc", f"({record['command']}) > /tmp/ardberg-preview.log 2>&1; "
            "echo $? > /tmp/ardberg-preview.exit", timeout=30,
        )
        if code:
            raise RuntimeError(f"Could not launch application: {output}")
        code, output = await _command("docker", "port", container,
                                      f"{record['port']}/tcp", timeout=20)
        if code or not output.strip():
            raise RuntimeError(f"Could not resolve localhost preview port: {output}")
        host_port = int(output.splitlines()[0].rsplit(":", 1)[1])
        url = f"http://127.0.0.1:{host_port}"
        ready_url = url + record["ready_path"]
        for _ in range(60):
            if await asyncio.to_thread(_responds, ready_url):
                _update(preview_id, status="ready", url=url)
                emit(run_id, "manual", "passed", agent="preview", node="start",
                     success=True, detail={"preview_id": preview_id, "url": url})
                return {"success": True, "url": url}
            code, _ = await _command("docker", "exec", container, "test", "-f",
                                     "/tmp/ardberg-preview.exit", timeout=10)
            if code == 0:
                _, log = await _command("docker", "exec", container, "cat",
                                        "/tmp/ardberg-preview.log", timeout=20)
                path = write_artifact(run_id, f"manual/{preview_id}/application.log", log)
                raise RuntimeError(f"Application exited before it became ready; see {path}")
            _, memory_events = await _command("docker", "exec", container, "cat",
                                               "/sys/fs/cgroup/memory.events", timeout=10)
            if re.search(r"(?m)^oom_kill [1-9]\d*$", memory_events):
                _, log = await _command("docker", "exec", container, "cat",
                                        "/tmp/ardberg-preview.log", timeout=20)
                path = write_artifact(run_id, f"manual/{preview_id}/application.log", log)
                raise RuntimeError(
                    f"Application exceeded the preview memory limit ({settings.preview_memory}); "
                    f"see {path}. Set PREVIEW_MEMORY for a larger local runner."
                )
            await asyncio.sleep(2)
        raise RuntimeError(f"Application did not respond at {ready_url}; inspect preview logs")
    except Exception as exc:
        _update(preview_id, status="failed", error=str(exc))
        emit(run_id, "manual", "failed", agent="preview", node="start",
             success=False, detail={"preview_id": preview_id, "error": str(exc)})
        await stop_preview(preview_id, failed=True)
        return {"success": False, "error": str(exc)}


async def stop_preview(preview_id: str, failed: bool = False, error: str = "") -> None:
    with session_scope() as session:
        item = session.get(InteractivePreview, preview_id)
        if item is None:
            return
        run_id, container, database, network = (
            item.run_id, item.container, item.database_container, item.network,
        )
    if container:
        try:
            code, output = await _command("docker", "exec", container, "cat",
                                          "/tmp/ardberg-preview.log", timeout=20)
            if code == 0:
                path = write_artifact(run_id, f"manual/{preview_id}/application.log", output)
                emit(run_id, "manual", "artifact", agent="preview", node="stop",
                     detail={"path": path})
        except (OSError, TimeoutError):
            pass
    for name in (container, database):
        if name:
            try:
                await _command("docker", "rm", "-f", name, timeout=30)
            except (OSError, TimeoutError):
                pass
    if network:
        try:
            await _command("docker", "network", "rm", network, timeout=30)
        except (OSError, TimeoutError):
            pass
    workspace_root = (run_dir(run_id) / "interactive" / preview_id).resolve()
    if run_dir(run_id).resolve() in workspace_root.parents and workspace_root.exists():
        shutil.rmtree(workspace_root)
    fields = {"status": "failed" if failed else "stopped", "url": None}
    if error:
        fields["error"] = error
    _update(preview_id, **fields)
    emit(run_id, "manual", "failed" if failed else "passed", agent="preview",
         node="stop", success=not failed, detail={"preview_id": preview_id})


@activity.defn
async def launch_preview_activity(preview_id: str) -> dict:
    return await launch_preview(preview_id)


@activity.defn
async def stop_preview_activity(preview_id: str, failed: bool = False,
                                error: str = "") -> None:
    await stop_preview(preview_id, failed=failed, error=error)


@activity.defn
async def preview_health_activity(preview_id: str) -> bool:
    record = preview_record(preview_id)
    if record["status"] != "ready":
        return False
    with session_scope() as session:
        container = session.get(InteractivePreview, preview_id).container
    if not container:
        await stop_preview(preview_id, failed=True,
                           error="Preview container is missing")
        return False
    exists, running = await _command("docker", "inspect", "--format",
                                     "{{.State.Running}}", container, timeout=10)
    if exists or running.strip() != "true":
        await stop_preview(preview_id, failed=True,
                           error="Preview container stopped unexpectedly")
        return False
    code, _ = await _command("docker", "exec", container, "test", "-f",
                             "/tmp/ardberg-preview.exit", timeout=10)
    if code == 0:
        _, exit_code = await _command("docker", "exec", container, "cat",
                                      "/tmp/ardberg-preview.exit", timeout=10)
        await stop_preview(preview_id, failed=True,
                           error=f"Application exited after readiness (exit {exit_code.strip()})")
        return False
    return True


@workflow.defn
class PreviewWorkflow:
    def __init__(self):
        self.stop_requested = False

    @workflow.signal
    def stop(self) -> None:
        self.stop_requested = True

    @workflow.run
    async def run(self, preview_id: str) -> None:
        try:
            launched = await workflow.execute_activity(
                launch_preview_activity, preview_id, start_to_close_timeout=timedelta(minutes=35),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception as exc:
            await workflow.execute_activity(
                stop_preview_activity, args=[preview_id, True, str(exc)],
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            return
        if not launched["success"]:
            return
        for _ in range(int(PREVIEW_LIFETIME.total_seconds() // 30)):
            try:
                await workflow.wait_condition(lambda: self.stop_requested,
                                              timeout=timedelta(seconds=30))
            except asyncio.TimeoutError:
                pass
            if self.stop_requested:
                break
            healthy = await workflow.execute_activity(
                preview_health_activity, preview_id,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            if not healthy:
                return
        await workflow.execute_activity(
            stop_preview_activity, preview_id, start_to_close_timeout=timedelta(minutes=3),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
