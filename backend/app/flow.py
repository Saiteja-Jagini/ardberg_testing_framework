from .events import get_events, get_run
from .agents import STAGE_NAMES


AGENT_LABELS = {"builtin": "Built-in Change Agent", "playwright": "Playwright Agent", "vitest": "Vitest Agent"}
AGENT_STEPS = STAGE_NAMES


def definition(run_id: str | None = None) -> dict:
    run = get_run(run_id) if run_id else None
    events = get_events(run_id) if run_id else []
    enabled_agents = set(run["context"].get("enabled_agents", [])) if run and run["context"] else None
    latest = {}
    for event in events:
        if event["node"] and event["status"] != "artifact":
            key = f"{event['agent']}.{event['node']}" if event["agent"] else f"preflight.{event['node']}"
            latest[key] = event
    nodes = []
    edges = []

    def add(node_id: str, label: str, x: int, y: int, group: str, description: str):
        event = latest.get(node_id)
        nodes.append({"id": node_id, "label": label, "x": x, "y": y,
                      "group": group, "description": description,
                      "status": event["status"] if event else "not_started",
                      "event": event})

    def link(source: str, target: str, label: str = ""):
        edges.append({"id": f"{source}->{target}", "source": source,
                      "target": target, "label": label})

    add("input", "User instruction + PR", 0, 250, "input",
        "The exact free-text testing instruction and pinned PR identify the run.")
    if run:
        nodes[-1]["status"] = "passed"
    preflight = [
        ("fetch_repository", "Fetch repository", "GitHub App fetches the PR and pinned source snapshot."),
        ("validate_instruction", "Validate instruction", "The model checks for observable behavior and expected results."),
        ("analyze_framework", "Shared context", "Framework, files, commands, and applicability are shared with enabled agents."),
    ]
    previous = "input"
    for index, (name, label, description) in enumerate(preflight):
        node_id = f"preflight.{name}"
        add(node_id, label, 280 * (index + 1), 250, "preflight", description)
        link(previous, node_id, "instruction" if index == 0 else "context")
        previous = node_id
    y_positions = {"builtin": 0, "playwright": 250, "vitest": 500}
    for agent, y in y_positions.items():
        steps = AGENT_STEPS[agent]
        for index, name in enumerate(steps):
            node_id = f"{agent}.{name}"
            label = name.replace("_", " ").title()
            add(node_id, label, 1160 + index * 250, y, agent,
                f"{AGENT_LABELS[agent]}: {label.lower()}. Receives the shared PR context and user instruction.")
            if enabled_agents is not None and agent not in enabled_agents:
                nodes[-1]["status"] = "not_selected"
                reason = run["context"].get("inactive_agent_reasons", {}).get(agent)
                if reason:
                    nodes[-1]["description"] += f" Not selected: {reason}"
            link(previous if index == 0 else f"{agent}.{steps[index - 1]}",
                 node_id, "shared context + instruction" if index == 0 else "node result")
    add("executor.apply_patches", "Apply patches", 3140, 250, "executor",
        "Validate and apply agent-produced test patches in a disposable checkout.")
    for agent in AGENT_LABELS:
        link(f"{agent}.{AGENT_STEPS[agent][-1]}", "executor.apply_patches", "validated patch")
    add("executor.start_runner", "Start local runner", 3410, 250, "executor",
        "Start an isolated local container with the pinned source snapshot.")
    add("executor.setup", "Install + start app", 3680, 250, "executor",
        "Install dependencies, provision repository-required services, run schema setup, and start the app.")
    link("executor.apply_patches", "executor.start_runner")
    link("executor.start_runner", "executor.setup")
    database_required = bool(run and run["context"] and
                             run["context"].get("analysis", {}).get("services"))
    add("executor.postgres_ready", "Test database ready", 3830, 250, "executor",
        "Start a disposable PostgreSQL service on the runner's private network when repository evidence requires it.")
    if run and not database_required:
        nodes[-1]["status"] = "not_selected"
    link("executor.setup", "executor.postgres_ready", "required service")
    suite_events = [event for event in events if event["agent"] == "executor" and event["node"] and
                    event["node"] not in {"apply_patches", "start_runner", "setup", "postgres_ready", "collect", "commit_tests"}]
    suite_names = list(dict.fromkeys(event["node"] for event in suite_events)) or ["builtin suite", "playwright suite", "vitest suite"]
    for index, name in enumerate(suite_names):
        node_id = f"executor.{name}"
        add(node_id, name.replace("-", " ").title(), 3970, 60 + index * 190, "suite",
            "Runs a test command in parallel with other active suites. First failure cancels siblings.")
        link("executor.postgres_ready" if database_required else "executor.setup",
             node_id, "parallel suite")
    add("executor.collect", "Collect evidence", 4260, 250, "executor",
        "Save structured test results, logs, traces, and patch artifacts.")
    for name in suite_names:
        link(f"executor.{name}", "executor.collect", "suite result")
    add("executor.commit_tests", "Commit generated tests", 4530, 250, "publication",
        "After successful execution, fast-forward the PR head with validated generated test files only.")
    link("executor.collect", "executor.commit_tests", "validated tests")
    add("report.classify_failures", "Classify failures", 4810, 250, "investigation",
        "Rules-based classification reads real events, exit codes, and logs.")
    link("executor.commit_tests", "report.classify_failures", "evidence")
    add("report.write_analysis", "Write analysis", 5090, 250, "report",
        "Report agent receives instruction, test results, failures, and evidence IDs.")
    add("report.verify_evidence", "Verify claims", 5370, 250, "report",
        "Reject report claims that cite missing or unsupported evidence.")
    add("report.publish", "GitHub + dashboard", 5650, 250, "report",
        "Publish an agent-written report to the PR and dashboard.")
    link("report.classify_failures", "report.write_analysis")
    link("report.write_analysis", "report.verify_evidence")
    link("report.verify_evidence", "report.publish")
    return {"nodes": nodes, "edges": edges,
            "run": {"id": run["id"], "status": run["status"], "stage": run["stage"],
                    "instruction": run["instruction"], "context": run["context"]} if run else None}
