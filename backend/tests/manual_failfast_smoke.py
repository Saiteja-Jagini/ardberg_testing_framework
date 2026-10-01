"""Verify Temporal cancels a sibling agent after the first failed node."""

import asyncio
from uuid import uuid4

from sqlalchemy import delete
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin
from temporalio.worker import Worker

import app.agents as agents
import app.workflow as orchestration
from app.db import init_db, session_scope
from app.events import get_events, get_run, set_run
from app.models import Run, RunEvent


async def main():
    init_db()
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(
            id=run_id, repository="local/failfast", pr_number=1, installation_id=0,
            instruction="A failed specialist must cancel the other active agent.",
        ))

    async def fake_prepare(_run_id):
        set_run(_run_id, status="running", stage="agents")
        return {
            "instruction": "A failed specialist must cancel the other active agent.",
            "enabled_agents": ["builtin", "playwright"],
            "changed_files": [{"filename": "src/example.ts", "status": "modified"}],
            "files": ["src/example.ts"],
            "behaviors": ["Failed specialist cancels a sibling"],
            "analysis": {
                "has_native_test_framework": False, "native_test_framework": "",
                "existing_test_commands": [], "app_start_command": "", "app_ready_url": "",
                "playwright": {
                    "mode": "unsupported", "target": "none", "evidence_files": [],
                    "reason": "No executable browser target",
                },
            },
        }

    async def fake_choose(agent, _context):
        if agent == "builtin":
            await asyncio.sleep(30)
        else:
            await asyncio.sleep(0.4)
        return [], ""

    async def fake_report(_run_id, _outcome):
        return "Local fail-fast smoke report."

    async def fake_publish(_run_id, _markdown, _success):
        return None

    orchestration.prepare_run = fake_prepare
    orchestration.generate_report = fake_report
    orchestration.publish_report = fake_publish
    agents.choose = fake_choose

    client = await Client.connect("localhost:7233")
    queue = "ardberg-failfast-" + run_id
    plugin = LangGraphPlugin(graphs={
        name: agents.build_agent_graph(name) for name in agents.AGENTS
    })
    worker = Worker(
        client, task_queue=queue,
        workflows=[orchestration.RunWorkflow, orchestration.AgentWorkflow],
        activities=[orchestration.preflight_activity, orchestration.execute_activity,
                    orchestration.report_activity, orchestration.publish_activity,
                    orchestration.status_activity],
        plugins=[plugin],
    )
    async with worker:
        outcome = await asyncio.wait_for(client.execute_workflow(
            orchestration.RunWorkflow.run, run_id,
            id="ardberg-failfast-" + run_id, task_queue=queue,
        ), timeout=20)
    events = get_events(run_id)
    print({"outcome": outcome, "status": get_run(run_id)["status"],
           "events": [(item["agent"], item["node"], item["status"])
                      for item in events if item["node"]]})
    assert outcome["stage"] == "agents"
    assert outcome["success"] is False
    assert any(item["agent"] == "playwright" and item["status"] == "failed" for item in events)
    assert any(item["agent"] == "builtin" and item["status"] == "cancelled" for item in events)
    assert not any(item["stage"] == "execution" for item in events)
    with session_scope() as session:
        session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
        session.execute(delete(Run).where(Run.id == run_id))


if __name__ == "__main__":
    asyncio.run(main())
