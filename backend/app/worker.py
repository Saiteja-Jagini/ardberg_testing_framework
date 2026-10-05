import asyncio
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin
from temporalio.worker import Worker

from .agents import AGENTS, build_agent_graph
from .config import settings
from .db import init_db
from .interactive_preview import (
    PreviewWorkflow, launch_preview_activity, preview_health_activity, stop_preview_activity,
)
from .workflow import (
    AgentWorkflow, ManualReportWorkflow, RunWorkflow, commit_tests_activity, execute_activity, preflight_activity,
    publish_activity, report_activity, status_activity,
)


async def main():
    init_db()
    client = await Client.connect(settings.temporal_address)
    plugin = LangGraphPlugin(graphs={name: build_agent_graph(name) for name in AGENTS})
    worker = Worker(
        client, task_queue=settings.temporal_task_queue,
        workflows=[RunWorkflow, AgentWorkflow, PreviewWorkflow, ManualReportWorkflow],
        activities=[preflight_activity, execute_activity, commit_tests_activity, report_activity,
                    publish_activity, status_activity, launch_preview_activity,
                    stop_preview_activity, preview_health_activity],
        plugins=[plugin],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
