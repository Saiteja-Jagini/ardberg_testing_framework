"""Exercise the Temporal failure path without GitHub or model credentials."""

import asyncio
from uuid import uuid4

from temporalio.client import Client

from app.config import settings
from app.db import init_db, session_scope
from app.events import get_events, get_run
from app.models import Run
from app.workflow import AgentWorkflow, RunWorkflow


async def main():
    init_db()
    run_id = str(uuid4())
    with session_scope() as session:
        session.add(Run(
            id=run_id, repository="smoke/example", pr_number=1, installation_id=1,
            instruction="Wrong credentials must show an error and prevent a session.",
        ))
    client = await Client.connect(settings.temporal_address)
    result = await client.execute_workflow(
        RunWorkflow.run, run_id, id="ardberg-smoke-" + run_id,
        task_queue=settings.temporal_task_queue,
    )
    print({"outcome": result, "run": get_run(run_id)["status"],
           "events": [(item["stage"], item["node"], item["status"]) for item in get_events(run_id)]})
    agent_run = str(uuid4())
    with session_scope() as session:
        session.add(Run(
            id=agent_run, repository="smoke/example", pr_number=1, installation_id=1,
            instruction="Wrong credentials must show an error and prevent a session.",
        ))
    context = {"instruction": "Wrong credentials must show an error and prevent a session.",
               "analysis": {"native_test_framework": "none"}, "changed_files": []}
    agent_result = await client.execute_workflow(
        AgentWorkflow.run, args=[agent_run, "builtin", context],
        id="ardberg-agent-smoke-" + agent_run, task_queue=settings.temporal_task_queue,
    )
    print({"agent_result": agent_result,
           "agent_events": [(item["node"], item["status"]) for item in get_events(agent_run)]})


if __name__ == "__main__":
    asyncio.run(main())
