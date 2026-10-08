"""Independent baseline setup/native validation and concurrent source review."""

import asyncio
import json
from pathlib import Path

from .artifacts import restore_artifact, write_artifact
from .config import settings
from .events import emit
from .review_runner import prepare_runtime, _command, _environment_args
from .setup_recipe import resolve_recipe


async def run_native_checks(run_id, state, recipe):
    results = []
    for check in recipe.checks:
        detail = {"name": check.name, "kind": check.kind, "command": check.command,
                  "revision": "head", "status": "blocked"}
        blocker = (state["setup_error"] if not state["dependencies_ready"] or
                   (check.requires_services and not state["services_ready"]) else "")
        if check.requires_application:
            blocker = blocker or state["app_error"] or ("No configured application process" if not state["origin"] else "")
        node = f"native_{check.name}"
        if blocker:
            detail["error"] = blocker
            emit(run_id, "runtime", "not_selected", agent="runner", node=node, detail=detail)
        else:
            emit(run_id, "runtime", "running", agent="runner", node=node, detail=detail)
            try:
                code, output = await _command(
                    "docker", "exec", *_environment_args({"CI": "true", **state["environment"]}),
                    state["container"], "sh", "-lc", check.command,
                    timeout=settings.test_timeout_seconds)
                detail.update(status="passed" if code == 0 else "failed", exit_code=code)
            except Exception as exc:
                output = str(exc)
                detail.update(status="failed", error=output)
            detail["log"] = write_artifact(run_id, f"review/baseline/{check.name}.log", output)
            detail["output_excerpt"] = output[-8000:]
            emit(run_id, "runtime", detail["status"], agent="runner", node=node,
                 success=detail["status"] == "passed", detail=detail)
        results.append(detail)
    emit(run_id, "runtime", ("failed" if any(r["status"] == "failed" for r in results) else
         "not_selected" if not results or any(r["status"] == "blocked" for r in results) else "passed"),
         agent="runner", node="native_checks", detail={"checks": results,
         "reason": "No repository-native checks declared" if not results else ""})
    return results


async def _baseline_branch(run_id, context, ready, release):
    result = {"status": "blocked", "configuration": "", "gaps": [], "checks": []}
    state = {"container": "", "setup_error": "", "app_error": "", "browser_error": "",
             "dependencies_ready": False, "services_ready": False, "analysis": {},
             "origin": "", "environment": {}, "baseline": result}

    def save_ready():
        result["artifact"] = write_artifact(run_id, "review/baseline/results.json", json.dumps(result, indent=2))
        if not ready.done():
            ready.set_result(state)

    try:
        emit(run_id, "runtime", "running", agent="runner", node="baseline_config")
        recipe, source, gaps = resolve_recipe(Path(context.get("source_path") or "__missing_source__"))
        result.update(configuration=source, gaps=gaps)
        result["recipe_artifact"] = write_artifact(run_id, "review/baseline/recipe.json", recipe.model_dump_json(indent=2))
        emit(run_id, "runtime", "passed", agent="runner", node="baseline_config", success=True,
             detail={"source": source, "checks": len(recipe.checks), "gaps": gaps})
        scoped = {**context, "analysis": recipe.analysis(), "runtime_environment_id": "baseline"}
        async with prepare_runtime(run_id, scoped, "baseline_head", []) as prepared:
            state.update(prepared)
            result.update(status="failed" if state["setup_error"] or state["app_error"] else "passed",
                          error=state["setup_error"] or state["app_error"],
                          dependencies=("passed" if state["dependencies_ready"] else "blocked")
                          if state.get("install_commands", recipe.install_commands) else "not_required",
                          services=("passed" if state["services_ready"] else "blocked") if recipe.services else "not_required",
                          application=("failed" if state["app_error"] else "passed" if state["origin"] else
                                       "blocked" if recipe.processes else "not_configured"))
            result["checks"] = await run_native_checks(run_id, state, recipe)
            save_ready()
            # The matching focused checks may reuse this runtime after native checks finish.
            await release.wait()
    except Exception as exc:
        state["setup_error"] = f"Baseline setup unavailable: {type(exc).__name__}: {exc}"
        result.update(status="blocked", error=state["setup_error"])
        emit(run_id, "runtime", "failed", agent="runner", node="baseline_config", success=False,
             detail={"error": state["setup_error"]})
        save_ready()


def empty_runtime(baseline):
    return {"observations": [], "probes_planned": 0, "probes_observed": 0,
            "probes_blocked": 0, "probes_failed": 0, "baseline": baseline}


async def run_independent_review(run_id: str, context: dict) -> dict:
    from .inspection import finish_preflight
    from .review import run_review

    ready = asyncio.get_running_loop().create_future()
    release = asyncio.Event()
    # A retry must never reuse superseded runtime observations after an early AI failure.
    write_artifact(run_id, "review/runtime-observations.json", json.dumps(empty_runtime({})))
    baseline_task = asyncio.create_task(_baseline_branch(run_id, context, ready, release))
    try:
        # Give setup its own branch before any model-dependent inspection starts.
        await asyncio.sleep(0)
        try:
            if context.get("needs_preflight_analysis"):
                context = await asyncio.wait_for(finish_preflight(run_id, context), timeout=600)
            return await run_review(run_id, context, prepared=ready)
        except Exception as exc:
            state = await ready
            gap = f"Source review or planning unavailable: {type(exc).__name__}: {exc}"
            emit(run_id, "review", "failed", agent="review", node="analysis", success=False,
                 detail={"error": gap, "baseline_retained": True})
            runtime = empty_runtime(state["baseline"])
            try:
                runtime.update(json.loads(restore_artifact(run_id, "review/runtime-observations.json").read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
            runtime["baseline"] = state["baseline"]
            runtime["artifact"] = write_artifact(run_id, "review/runtime-observations.json",
                                                 json.dumps(runtime, indent=2))
            return {"mode": "critique", "success": False, "stage": "review", "error": gap,
                    "verdict": "needs_review", "check_conclusion": "failure", "runtime": runtime,
                    "judgment": {"summary": "Baseline execution is recorded separately from unavailable AI review.",
                                 "findings": [], "assessments": [], "unverified": [gap]},
                    "artifacts": [runtime["artifact"], state["baseline"]["artifact"]]}
    finally:
        release.set()
        if not ready.done():
            baseline_task.cancel()
        # Await cleanup on success, model failure and cancellation alike.
        await asyncio.gather(baseline_task, return_exceptions=True)
