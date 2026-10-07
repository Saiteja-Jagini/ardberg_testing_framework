import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from temporalio.client import Client

from .artifacts import artifact_path
from .config import settings
from .db import init_db, session_scope
from .events import get_events, get_run
from .flow import definition
from .github import GitHubApp, GitHubError, parse_github_url, verify_webhook
from .models import GitHubInstallation, GitHubUser, PullRequestSettings, Run, WebhookDelivery
from .schemas import CreateRunRequest, PreviewRequest, ResolveRequest
from .inspection import preview_repository
from .interactive_preview import PreviewWorkflow, preview_defaults, preview_for_run, preview_record
from .models import InteractivePreview, InteractivePreviewOptions, ManualObservation
from .runner import _command
from .schemas import ManualObservationRequest, StartInteractivePreviewRequest
from .workflow import ManualReportWorkflow, ReviewWorkflow, RunWorkflow


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Ardberg PR testing", lifespan=lifespan)


def frontend_origins() -> list[str]:
    configured = settings.frontend_origin.rstrip("/")
    origins = {configured}
    parsed = urlsplit(configured)
    if parsed.hostname in {"localhost", "127.0.0.1"} and parsed.port is not None:
        origins.update({f"{parsed.scheme}://localhost:{parsed.port}",
                        f"{parsed.scheme}://127.0.0.1:{parsed.port}"})
    return sorted(origins)


app.add_middleware(CORSMiddleware, allow_origins=frontend_origins(),
                   allow_methods=["GET", "POST"], allow_headers=["*"])


def record_installation(details: dict, active: bool = True) -> None:
    account = details.get("account") or {}
    if not details.get("id") or not account.get("id"):
        return
    with session_scope() as session:
        item = session.get(GitHubInstallation, int(details["id"]))
        if item is None:
            item = GitHubInstallation(id=int(details["id"]), account_id=int(account["id"]),
                                      account_login=account.get("login", ""),
                                      target_type=details.get("target_type", "unknown"))
            session.add(item)
        item.account_id = int(account["id"])
        item.account_login = account.get("login", "")
        item.target_type = details.get("target_type", "unknown")
        item.active = active


def record_user(sender: dict | None) -> None:
    if not sender or not sender.get("id"):
        return
    with session_scope() as session:
        item = session.get(GitHubUser, int(sender["id"]))
        if item is None:
            item = GitHubUser(id=int(sender["id"]), login=sender.get("login", ""))
            session.add(item)
        item.login = sender.get("login", "")


@app.get("/health")
def health():
    return {"status": "ok", "github_configured": bool(settings.github_app_id and settings.github_private_key),
            "model_configured": bool(settings.openai_api_key)}


@app.get("/api/connections")
async def connections():
    async def github_status():
        if not settings.github_app_id or not settings.github_private_key:
            return {"connected": False, "error": "GitHub App ID or private key is missing"}
        try:
            details = await GitHubApp().app_details()
            matches = str(details.get("id")) == settings.github_app_id
            return {"connected": matches, "slug": details.get("slug", ""),
                    "error": "" if matches else "GitHub App ID does not match the private key"}
        except Exception as exc:
            return {"connected": False, "error": f"GitHub authentication failed: {type(exc).__name__}"}

    async def model_status():
        if not settings.openai_api_key:
            return {"connected": False, "model": settings.openai_model,
                    "error": "OPENAI_API_KEY is missing"}
        try:
            def retrieve_model():
                with OpenAI(api_key=settings.openai_api_key, timeout=20) as client:
                    return client.models.retrieve(settings.openai_model)

            model = await asyncio.to_thread(retrieve_model)
            return {"connected": model.id == settings.openai_model,
                    "model": settings.openai_model,
                    "error": "" if model.id == settings.openai_model else "Configured model is unavailable"}
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            message = f"OpenAI model access failed (HTTP {status})" if status else "OpenAI model access failed"
            return {"connected": False, "model": settings.openai_model, "error": message}

    github, model = await asyncio.gather(github_status(), model_status())
    return {"github": github, "model": model}


@app.post("/api/resolve")
async def resolve(request: ResolveRequest):
    try:
        repository, number = parse_github_url(request.url)
        github = GitHubApp()
        installation = await github.installation_details_for_repo(repository)
        record_installation(installation)
        installation_id = int(installation["id"])
        token = await github.installation_token(installation_id)
        prs = [await github.pull_request(repository, number, token)] if number else await github.pull_requests(repository, token)
        return {"repository": repository, "installation_id": installation_id,
                "pull_requests": [{"number": pr["number"], "title": pr["title"],
                                   "head_sha": pr["head"]["sha"], "url": pr["html_url"]} for pr in prs]}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except GitHubError as exc:
        install = f"https://github.com/apps/{settings.github_app_slug}/installations/new" if settings.github_app_slug else None
        raise HTTPException(409, {"message": str(exc), "install_url": install}) from exc


async def _start_run(repository: str, number: int, instruction: str,
                     selected_frameworks: list[str], installation_id: int,
                     expected_head: str | None = None,
                     mode: str = "critique", publish_to_github: bool = True) -> str:
    github = GitHubApp()
    token = await github.installation_token(installation_id)
    pr = await github.pull_request(repository, number, token)
    if expected_head and pr["head"]["sha"] != expected_head:
        raise ValueError("Webhook head was superseded before the run started")
    with session_scope() as session:
        run = Run(repository=repository, pr_number=number, installation_id=installation_id,
                  title=pr["title"], head_sha=pr["head"]["sha"], base_sha=pr["base"]["sha"],
                  instruction=instruction, selected_frameworks=selected_frameworks,
                  context={"mode": mode, "publish_to_github": publish_to_github})
        session.add(run)
        session.flush()
        run_id = run.id
        setting = session.scalar(select(PullRequestSettings).where(
            PullRequestSettings.repository == repository,
            PullRequestSettings.pr_number == number,
        ))
        if setting is None:
            session.add(PullRequestSettings(repository=repository, pr_number=number,
                                            instruction=instruction, selected_frameworks=selected_frameworks))
        else:
            setting.instruction = instruction
            setting.selected_frameworks = selected_frameworks
    check_id = None
    try:
        if publish_to_github:
            check_id = await github.create_check(repository, pr["head"]["sha"], token, "Ardberg PR review")
        with session_scope() as session:
            session.get(Run, run_id).check_run_id = check_id
        client = await Client.connect(settings.temporal_address)
        await client.start_workflow(ReviewWorkflow.run if mode == "critique" else RunWorkflow.run, run_id,
                                    id=f"ardberg-{run_id}", task_queue=settings.temporal_task_queue)
    except Exception as exc:
        if check_id is not None:
            try:
                await github.complete_check(
                    repository, check_id, token, "failure",
                    "Ardberg could not start the local run; see the dashboard for the setup error.",
                )
            except Exception:
                pass
        with session_scope() as session:
            db_run = session.get(Run, run_id)
            db_run.status = "failed"
            db_run.error = f"Could not start workflow: {exc}"
        raise
    return run_id


@app.post("/api/runs")
async def create_run(request: CreateRunRequest):
    try:
        repository, _ = parse_github_url(f"https://github.com/{request.repository}")
        github = GitHubApp()
        installation = await github.installation_details_for_repo(repository)
        record_installation(installation)
        installation_id = int(installation["id"])
        run_id = await _start_run(repository, request.pr_number, request.instruction,
                                  request.selected_frameworks, installation_id,
                                  mode=request.mode, publish_to_github=request.publish_to_github)
        return {"run_id": run_id}
    except (ValueError, GitHubError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, f"Run service unavailable: {exc}") from exc


@app.get("/api/runs")
def list_runs():
    with session_scope() as session:
        records = session.scalars(
            select(Run).join(GitHubInstallation,
                             GitHubInstallation.id == Run.installation_id)
            .order_by(Run.created_at.desc()).limit(25)
        ).all()
        return [{
            "id": item.id, "repository": item.repository,
            "pr_number": item.pr_number, "title": item.title,
            "head_sha": item.head_sha, "status": item.status,
            "stage": item.stage, "created_at": item.created_at.isoformat(),
        } for item in records]


@app.post("/api/preview")
async def preview(request: PreviewRequest):
    try:
        repository, _ = parse_github_url(f"https://github.com/{request.repository}")
        github = GitHubApp()
        installation_id = await github.installation_for_repo(repository)
        return await preview_repository(repository, request.pr_number, installation_id)
    except (ValueError, GitHubError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, f"Could not analyze repository: {exc}") from exc


@app.get("/api/runs/{run_id}")
def read_run(run_id: str):
    try:
        return get_run(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/runs/{run_id}/events")
def read_events(run_id: str, after: int = 0):
    try:
        get_run(run_id)
        return get_events(run_id, after)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/runs/{run_id}/retry-review")
async def retry_pinned_review(run_id: str, replan_runtime: bool = True):
    """Retry runtime planning/execution after setup recovery, preserving pinned intent/source."""
    from .artifacts import restore_artifact, write_artifact
    from .events import set_run
    try:
        run = get_run(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    if run["status"] not in {"failed", "completed"} or run["context"].get("mode") != "critique":
        raise HTTPException(409, "Only a finished critique can retry its pinned runtime review")
    if not run["context"].get("source_path") or not Path(run["context"]["source_path"]).is_dir():
        raise HTTPException(409, "Pinned source is unavailable; start a new review")
    for name in ("expectations", "code-critique"):
        if not restore_artifact(run_id, f"review/{name}.json").is_file():
            raise HTTPException(409, "Source analysis is incomplete; start a new review")
    if not replan_runtime and not restore_artifact(run_id, "review/runtime-plan.json").is_file():
        raise HTTPException(409, "Runtime plan is unavailable; retry with planning enabled")
    if run["report"]:
        write_artifact(run_id, "review/previous-report.md", run["report"])
    set_run(run_id, context={**run["context"], "resume_source_review": True,
                            "resume_runtime_plan": not replan_runtime},
            status="queued", stage="review", error=None, report=None)
    try:
        client = await Client.connect(settings.temporal_address)
        await client.start_workflow(ReviewWorkflow.run, run_id,
            id=f"ardberg-{run_id}-retry-{uuid4().hex[:8]}", task_queue=settings.temporal_task_queue)
    except Exception as exc:
        set_run(run_id, status="failed", error=f"Could not retry runtime review: {exc}")
        raise HTTPException(503, "Could not start runtime review retry") from exc
    return {"run_id": run_id}


@app.get("/api/runs/{run_id}/interactive-preview")
def read_interactive_preview(run_id: str):
    try:
        return preview_for_run(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/runs/{run_id}/interactive-preview")
async def start_interactive_preview(run_id: str, request: StartInteractivePreviewRequest):
    try:
        run = get_run(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    context = run["context"] or {}
    source_path = context.get("source_path")
    if not context.get("analysis") or not source_path or not Path(source_path).is_dir():
        raise HTTPException(409, "Wait for PR preflight to produce a pinned source snapshot")
    defaults = preview_defaults(context)
    command = request.command or defaults["command"]
    if not command:
        raise HTTPException(422, "Enter an application start command for this repository")
    port = request.port or defaults["port"]
    ready_path = request.ready_path or defaults["ready_path"]
    if not ready_path.startswith("/") or ready_path.startswith("//"):
        raise HTTPException(422, "Ready path must start with a single slash")
    protected = {"DATABASE_URL", "POSTGRES_URL", "PGHOST", "PGPORT", "PGUSER",
                 "PGPASSWORD", "PGDATABASE"}
    for service in context["analysis"].get("services", []):
        protected.update(key.upper() for key in service.get("connection_environment_keys", []))
    if protected & {key.upper() for key in request.environment}:
        raise HTTPException(422, "Preview database connection is managed by the disposable service")
    with session_scope() as session:
        existing = session.scalar(select(InteractivePreview).where(
            InteractivePreview.run_id == run_id,
            InteractivePreview.status.in_(["queued", "starting", "ready", "stopping"]),
        ))
        if existing:
            raise HTTPException(409, "An interactive preview is already active for this run")
        item = InteractivePreview(run_id=run_id, command=command, port=port,
                                  ready_path=ready_path)
        session.add(item)
        session.flush()
        preview_id = item.id
        session.add(InteractivePreviewOptions(
            preview_id=preview_id, environment=request.environment,
            setup_commands=request.setup_commands,
        ))
    try:
        client = await Client.connect(settings.temporal_address)
        await client.start_workflow(PreviewWorkflow.run, preview_id,
                                    id=f"ardberg-preview-{preview_id}",
                                    task_queue=settings.temporal_task_queue)
    except Exception as exc:
        with session_scope() as session:
            item = session.get(InteractivePreview, preview_id)
            item.status = "failed"
            item.error = f"Could not queue preview: {exc}"
        raise HTTPException(503, f"Preview worker unavailable: {exc}") from exc
    return preview_record(preview_id)


@app.post("/api/runs/{run_id}/interactive-preview/stop")
async def stop_interactive_preview(run_id: str):
    data = preview_for_run(run_id)
    preview = data["session"]
    if not preview or preview["status"] not in {"queued", "starting", "ready", "stopping"}:
        raise HTTPException(409, "No active preview to stop")
    try:
        client = await Client.connect(settings.temporal_address)
        await client.get_workflow_handle(f"ardberg-preview-{preview['id']}").signal(PreviewWorkflow.stop)
        with session_scope() as session:
            session.get(InteractivePreview, preview["id"]).status = "stopping"
    except Exception as exc:
        raise HTTPException(503, f"Could not stop preview: {exc}") from exc
    return {"accepted": True}


@app.get("/api/runs/{run_id}/interactive-preview/logs")
async def interactive_preview_logs(run_id: str):
    data = preview_for_run(run_id)
    preview = data["session"]
    if not preview:
        raise HTTPException(404, "No preview exists for this run")
    if preview["status"] in {"stopped", "failed"}:
        relative = f"manual/{preview['id']}/application.log"
        path = artifact_path(run_id, relative)
        return {"text": path.read_text(encoding="utf-8", errors="replace")[-20000:]
                if path.is_file() else "", "artifact": relative if path.is_file() else None}
    with session_scope() as session:
        container = session.get(InteractivePreview, preview["id"]).container
    if not container:
        return {"text": "Preview has not started yet", "artifact": None}
    try:
        code, output = await _command("docker", "exec", container, "cat",
                                      "/tmp/ardberg-preview.log", timeout=15)
        return {"text": output[-20000:] if code == 0 else "Application log is not ready", "artifact": None}
    except (OSError, TimeoutError):
        return {"text": "Application log is unavailable", "artifact": None}


async def _queue_manual_report(run_id: str) -> dict:
    run = get_run(run_id)
    if run["status"] not in {"completed", "failed"}:
        return {"queued": False, "reason": "Automated review is still running"}
    saved = artifact_path(run_id, "outcome.json")
    if not saved.is_file():
        return {"queued": False, "reason": "Automated outcome is not available yet"}
    outcome = json.loads(saved.read_text(encoding="utf-8"))
    with session_scope() as session:
        observations = session.scalars(select(ManualObservation).where(
            ManualObservation.run_id == run_id,
        ).order_by(ManualObservation.created_at)).all()
        verdicts = [item.verdict for item in observations]
    outcome["manual_review"] = {"verdicts": verdicts,
                                "all_passed": all(verdict == "passed" for verdict in verdicts)}
    outcome["success"] = bool(outcome["success"] and outcome["manual_review"]["all_passed"])
    if not outcome["manual_review"]["all_passed"]:
        outcome["error"] = "; ".join(filter(None, [outcome.get("error"),
                                                    "Manual review did not pass"]))
    client = await Client.connect(settings.temporal_address)
    await client.start_workflow(ManualReportWorkflow.run, args=[run_id, outcome],
                                id=f"ardberg-manual-report-{uuid4()}",
                                task_queue=settings.temporal_task_queue)
    return {"queued": True}


@app.post("/api/runs/{run_id}/interactive-preview/observations")
async def record_manual_observation(run_id: str, request: ManualObservationRequest):
    data = preview_for_run(run_id)
    preview = data["session"]
    if not preview:
        raise HTTPException(409, "Start a preview before recording manual evidence")
    with session_scope() as session:
        observation = ManualObservation(
            run_id=run_id, preview_id=preview["id"], verdict=request.verdict,
            steps=request.steps.strip(), expected=request.expected.strip(),
            actual=request.actual.strip(),
        )
        session.add(observation)
        session.flush()
        observation_id = observation.id
    from .events import emit
    emit(run_id, "manual", "passed" if request.verdict == "passed" else "failed",
         agent="reviewer", node="observation", success=request.verdict == "passed",
         detail={"observation_id": observation_id, "preview_id": preview["id"],
                 "verdict": request.verdict, "steps": request.steps,
                 "expected": request.expected, "actual": request.actual})
    try:
        report = await _queue_manual_report(run_id)
    except Exception as exc:
        report = {"queued": False, "reason": f"Could not queue report update: {exc}"}
    return {"observation_id": observation_id, "report": report}


@app.post("/api/runs/{run_id}/interactive-preview/refresh-report")
async def refresh_manual_report(run_id: str):
    try:
        return await _queue_manual_report(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, f"Could not queue report update: {exc}") from exc


@app.get("/api/runs/{run_id}/stream")
async def stream_events(run_id: str, request: Request, after: int = 0):
    try:
        get_run(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    last_id = max(after, int(request.headers.get("last-event-id", "0")))

    async def stream():
        cursor = last_id
        while not await request.is_disconnected():
            events = get_events(run_id, cursor)
            for event in events:
                cursor = event["id"]
                yield f"id: {cursor}\nevent: node\ndata: {json.dumps(event)}\n\n"
            if get_run(run_id)["status"] in {"completed", "failed"}:
                yield "event: done\ndata: {}\n\n"
                break
            yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/runs/{run_id}/artifacts/{relative:path}")
def download_artifact(run_id: str, relative: str):
    try:
        run = get_run(run_id)
        from .artifacts import restore_artifact, write_artifact
        path = restore_artifact(run_id, relative)
        if relative == "report.md" and not path.is_file() and run.get("report"):
            write_artifact(run_id, relative, run["report"])
    except (LookupError, ValueError) as exc:
        raise HTTPException(404, str(exc)) from exc
    if not path.is_file() or "source" in Path(relative).parts or "workspace" in Path(relative).parts:
        raise HTTPException(404, "Artifact not found")
    return FileResponse(path)


@app.get("/api/flow")
def flow_definition():
    return definition()


@app.get("/api/runs/{run_id}/flow")
def run_flow(run_id: str):
    try:
        return definition(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/webhooks/github")
async def github_webhook(request: Request):
    body = await request.body()
    if not verify_webhook(body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(401, "Invalid GitHub webhook signature")
    event_name = request.headers.get("x-github-event")
    payload = json.loads(body)
    record_user(payload.get("sender"))
    if event_name == "installation":
        action = payload.get("action")
        if action in {"created", "deleted", "suspend", "unsuspend"}:
            record_installation(payload.get("installation") or {},
                                active=action in {"created", "unsuspend"})
            return {"accepted": True, "action": action}
    if event_name != "pull_request":
        return {"accepted": False, "reason": "event not configured"}
    if payload.get("action") not in {"opened", "synchronize", "reopened"}:
        return {"accepted": False, "reason": "action not configured"}
    repository = payload["repository"]["full_name"]
    number = payload["number"]
    delivery = request.headers.get("x-github-delivery", "")
    if not delivery:
        raise HTTPException(422, "Missing GitHub delivery ID")
    try:
        with session_scope() as session:
            session.add(WebhookDelivery(id=delivery, repository=repository))
    except IntegrityError:
        return {"accepted": False, "reason": "duplicate webhook delivery"}
    with session_scope() as session:
        setting = session.scalar(select(PullRequestSettings).where(
            PullRequestSettings.repository == repository,
            PullRequestSettings.pr_number == number,
        ))
        if setting is None:
            return {"accepted": False, "reason": "PR has no saved test settings; start its first run in the dashboard"}
        instruction, selected = setting.instruction, list(setting.selected_frameworks)
        existing = session.scalar(select(Run).where(
            Run.repository == repository, Run.pr_number == number,
            Run.head_sha == payload["pull_request"]["head"]["sha"],
        ).order_by(Run.created_at.desc()))
        if existing:
            return {"accepted": False, "reason": "head commit already has a run", "run_id": existing.id}
    if payload["action"] == "synchronize":
        github = GitHubApp()
        token = await github.installation_token(int(payload["installation"]["id"]))
        commit = await github.git_commit(repository, payload["pull_request"]["head"]["sha"], token)
        marker = re.search(r"^Ardberg-Run: ([0-9a-f-]{36})$",
                           commit.get("message", ""), flags=re.MULTILINE)
        if marker:
            with session_scope() as session:
                published_by = session.get(Run, marker.group(1))
                if (published_by and published_by.repository == repository and
                        published_by.pr_number == number):
                    return {"accepted": False, "reason": "generated-test commit for an existing run",
                            "run_id": published_by.id}
    try:
        run_id = await _start_run(repository, number, instruction, selected,
                                  int(payload["installation"]["id"]),
                                  expected_head=payload["pull_request"]["head"]["sha"])
        return {"accepted": True, "run_id": run_id, "delivery": delivery}
    except ValueError as exc:
        return {"accepted": False, "reason": str(exc), "delivery": delivery}
    except Exception as exc:
        raise HTTPException(503, f"Could not queue webhook run: {exc}") from exc
