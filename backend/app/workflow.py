import asyncio
import json
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph import graph

with workflow.unsafe.imports_passed_through():
    from .artifacts import artifact_path, write_artifact
    from .events import emit, get_events, set_run
    from .inspection import prepare_run
    from .publication import publish_generated_tests
    from .report import generate_report, publish_report
    from .runner import execute_tests


NO_RETRY = RetryPolicy(maximum_attempts=1)


def inferred_feature_unverified(context: dict, results: list[dict]) -> bool:
    return (context.get("instruction_source") == "inferred" and
            not any((result.get("plan") or {}).get("cases") for result in results))


@activity.defn
async def preflight_activity(run_id: str) -> dict:
    try:
        return await prepare_run(run_id)
    except Exception as exc:
        running = [item for item in get_events(run_id) if item["stage"] == "preflight"
                   and item["status"] == "running" and item["node"]]
        node = running[-1]["node"] if running else "preflight"
        emit(run_id, "preflight", "failed", node=node, success=False,
             detail={"error": str(exc)})
        return {"_failed": str(exc)}


@activity.defn
async def execute_activity(run_id: str, context: dict, agent_results: list[dict]) -> dict:
    set_run(run_id, stage="execution")
    return await execute_tests(run_id, context, agent_results)


@activity.defn
async def commit_tests_activity(run_id: str, context: dict,
                                agent_results: list[dict]) -> dict:
    set_run(run_id, stage="publication")
    return await publish_generated_tests(run_id, context, agent_results)


@activity.defn
async def report_activity(run_id: str, outcome: dict) -> str:
    set_run(run_id, stage="report")
    if not artifact_path(run_id, "outcome.json").is_file():
        write_artifact(run_id, "outcome.json", json.dumps(outcome, indent=2))
    try:
        return await generate_report(run_id, outcome)
    except Exception as exc:
        report_nodes = [item for item in get_events(run_id)
                        if item["stage"] == "report" and item["node"]]
        if report_nodes and report_nodes[-1]["status"] == "running":
            emit(run_id, "report", "failed", agent="report",
                 node=report_nodes[-1]["node"], success=False,
                 detail={"error": str(exc)})
        raise


@activity.defn
async def publish_activity(run_id: str, markdown: str, success: bool) -> None:
    try:
        await publish_report(run_id, markdown, success)
    except Exception as exc:
        emit(run_id, "report", "failed", agent="report", node="publish",
             success=False, detail={"error": str(exc)})
        raise


@activity.defn
async def status_activity(run_id: str, status: str, stage: str, error: str = "") -> None:
    set_run(run_id, status=status, stage=stage, error=error or None)
    emit(run_id, stage, status, detail={"error": error} if error else {})


@workflow.defn
class AgentWorkflow:
    @workflow.run
    async def run(self, run_id: str, agent: str, context: dict) -> dict:
        result = await graph(agent).compile().ainvoke({
            "run_id": run_id, "agent": agent, "context": context, "failed": False,
        })
        return {key: value for key, value in result.items() if key != "context"}


@workflow.defn
class RunWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> dict:
        context = None
        outcome = {"success": False, "stage": "preflight", "error": "Run did not start"}
        try:
            context = await workflow.execute_activity(
                preflight_activity, run_id, start_to_close_timeout=timedelta(minutes=10),
                retry_policy=NO_RETRY,
            )
            if context.get("_failed"):
                outcome = {"success": False, "stage": "preflight", "error": context["_failed"]}
                context = None
                raise RuntimeError(outcome["error"])
            outcome = {"success": False, "stage": "agents", "error": "Agent execution did not finish"}
            handles = []
            for agent in context["enabled_agents"]:
                handle = await workflow.start_child_workflow(
                    AgentWorkflow.run, args=[run_id, agent, context],
                    id=f"{run_id}-{agent}", task_queue=workflow.info().task_queue,
                )
                handles.append((agent, handle))
            pending = {handle: agent for agent, handle in handles}
            agent_results = []
            while pending:
                done, _ = await workflow.wait(
                    list(pending), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    agent = pending.pop(task)
                    try:
                        result = task.result()
                    except Exception as exc:
                        result = {"agent": agent, "failed": True, "error": str(exc)}
                    agent_results.append(result)
            agent_results.sort(key=lambda result: context["enabled_agents"].index(result["agent"]))
            failed_agents = [result for result in agent_results if result.get("failed")]
            usable_results = [result for result in agent_results
                              if not result.get("failed") and not result.get("skipped")]
            if usable_results:
                execution = await workflow.execute_activity(
                    execute_activity, args=[run_id, context, usable_results],
                    start_to_close_timeout=timedelta(hours=2), retry_policy=NO_RETRY,
                )
            else:
                execution = {"success": False, "error": "No agent produced executable tests", "suites": []}
            problems = [f"{item['agent']}: {item.get('error') or 'agent failed'}" for item in failed_agents]
            if execution.get("error"):
                problems.append(execution["error"])
            feature_unverified = inferred_feature_unverified(context, usable_results)
            if feature_unverified:
                problems.append("No executable feature-specific test case was planned; "
                                "existing suite results do not verify the PR feature")
            outcome = {"success": execution["success"] and not failed_agents
                       and not context.get("review_incomplete") and not feature_unverified,
                       "stage": "execution" if usable_results else "agents",
                       "execution": execution, "agents": agent_results,
                       "error": "; ".join(problems + context.get("review_incomplete_reasons", []))}
            if outcome["success"]:
                publication = await workflow.execute_activity(
                    commit_tests_activity, args=[run_id, context, agent_results],
                    start_to_close_timeout=timedelta(minutes=3), retry_policy=NO_RETRY,
                )
                outcome["publication"] = publication
                if not publication["success"]:
                    outcome["success"] = False
                    outcome["stage"] = "publication"
                    outcome["error"] = publication["error"]
        except Exception as exc:
            if outcome["error"] in {"Run did not start", "Agent execution did not finish"}:
                outcome = {"success": False, "stage": "preflight" if context is None else "agents",
                           "error": str(exc)}
        await workflow.execute_activity(
            status_activity, args=[run_id, "reporting", "report", outcome.get("error", "")],
            start_to_close_timeout=timedelta(seconds=30), retry_policy=NO_RETRY,
        )
        try:
            report = await workflow.execute_activity(
                report_activity, args=[run_id, outcome],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(
                    maximum_attempts=3, initial_interval=timedelta(seconds=10),
                    non_retryable_error_types=["RuntimeError", "ValidationError"],
                ),
            )
            await workflow.execute_activity(
                publish_activity, args=[run_id, report, outcome["success"]],
                start_to_close_timeout=timedelta(minutes=2), retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                status_activity,
                args=[run_id, "completed" if outcome["success"] else "failed", "done", outcome.get("error", "")],
                start_to_close_timeout=timedelta(seconds=30), retry_policy=NO_RETRY,
            )
        except Exception as exc:
            await workflow.execute_activity(
                status_activity, args=[run_id, "failed", "report", f"Report/publish failed: {exc}"],
                start_to_close_timeout=timedelta(seconds=30), retry_policy=NO_RETRY,
            )
        return outcome


@workflow.defn
class ManualReportWorkflow:
    @workflow.run
    async def run(self, run_id: str, outcome: dict) -> None:
        try:
            markdown = await workflow.execute_activity(
                report_activity, args=[run_id, outcome],
                start_to_close_timeout=timedelta(minutes=10), retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                publish_activity, args=[run_id, markdown, outcome["success"]],
                start_to_close_timeout=timedelta(minutes=2), retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                status_activity,
                args=[run_id, "completed" if outcome["success"] else "failed", "done",
                      outcome.get("error", "")],
                start_to_close_timeout=timedelta(seconds=30), retry_policy=NO_RETRY,
            )
        except Exception as exc:
            await workflow.execute_activity(
                status_activity,
                args=[run_id, "failed", "report", f"Manual report update failed: {exc}"],
                start_to_close_timeout=timedelta(seconds=30), retry_policy=NO_RETRY,
            )
