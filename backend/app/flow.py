from .events import get_events, get_run
from .agents import STAGE_NAMES


AGENT_LABELS = {"builtin": "Built-in Change Agent", "playwright": "Playwright Agent", "vitest": "Vitest Agent"}
AGENT_STEPS = STAGE_NAMES


def review_definition(run: dict | None, events: list[dict]) -> dict:
    """The critique workflow shown for new and review-mode runs."""
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
                      "status": event["status"] if event else "not_started", "event": event})
    def link(source: str, target: str, label: str = ""):
        edges.append({"id": f"{source}->{target}", "source": source,
                      "target": target, "label": label})
    add("input", "PR + review intent", 0, 250, "input",
        "The PR link and optional review intent define the critique.")
    if run:
        nodes[-1]["status"] = "passed"
    previous = "input"
    preflight = [
        ("fetch_repository", "Pin diff and source", "Fetch the exact base and PR revisions."),
        ("validate_instruction", "Interpret intent", "Keep supplied intent or infer expectations from PR evidence."),
        ("analyze_framework", "Discover runtime", "Find application, install, start, and service commands."),
        ("map_impact", "Map change impact", "Trace changed code and related surfaces."),
        ("compare_api_contract", "Compare API contract", "Compare checked-in contracts where available."),
    ]
    for index, (name, label, description) in enumerate(preflight):
        node_id = f"preflight.{name}"
        add(node_id, label, (index + 1) * 280, 250, "preflight", description)
        link(previous, node_id)
        previous = node_id
    stages = [
        ("behavior.analyze", "Behavior agent", "Extract expected behavior and ambiguity from intent and PR evidence.", "review"),
        ("code_critic.analyze", "Code critic", "Trace changed implementation and identify evidence-backed concerns.", "review"),
        ("runtime_planner.analyze", "Runtime planner", "Plan focused browser, HTTP, or script probes.", "review"),
        ("runner.base_setup", "Base runtime", "Run comparable probes on the pinned base when useful.", "executor"),
        ("runner.head_setup", "PR runtime", "Run the changed code in a disposable container.", "executor"),
        ("evidence_critic.classify", "Evidence critic", "Separate confirmed findings from potential and unverified concerns.", "review"),
        ("report.classify_failures", "Classify blockers", "Read setup and runtime failure evidence.", "investigation"),
        ("report.write_analysis", "Write critique", "Report missing behavior, impact, reproduction, and limits.", "report"),
        ("report.verify_evidence", "Verify claims", "Reject unsupported report claims.", "report"),
        ("report.publish", "Publish report", "Save to dashboard, GitHub check, and PR comment.", "report"),
    ]
    for index, (node_id, label, description, group) in enumerate(stages):
        add(node_id, label, 1680 + index * 300, 250, group, description)
        if (run and run["status"] in {"completed", "failed"} and
                node_id in {"runner.base_setup", "runner.head_setup"} and
                nodes[-1]["event"] is None):
            nodes[-1]["status"] = "not_selected"
        link(previous, node_id)
        previous = node_id
    probe_events = [event for event in events if event["stage"] == "runtime"
                    and event["agent"] == "runner" and event["node"]
                    and event["node"] not in {"head_setup", "base_setup"}
                    and not event["node"].startswith(("head_install_", "base_install_",
                                                       "head_service_", "base_service_"))]
    for index, name in enumerate(dict.fromkeys(event["node"] for event in probe_events)):
        node_id = f"runner.{name}"
        add(node_id, f"Probe {name}", 2850, 570 + index * 135, "runtime",
            "Focused observation of changed behavior; see its event and saved artifacts.")
        link("runner.head_setup", node_id, "execute")
        link(node_id, "evidence_critic.classify", "observation")
    return {"nodes": nodes, "edges": edges,
            "run": {"id": run["id"], "repository": run["repository"],
                    "pr_number": run["pr_number"], "head_sha": run["head_sha"],
                    "status": run["status"], "stage": run["stage"],
                    "instruction": (run["context"] or {}).get("instruction", run["instruction"]),
                    "context": run["context"]} if run else None}


def definition(run_id: str | None = None) -> dict:
    run = get_run(run_id) if run_id else None
    events = get_events(run_id) if run_id else []
    if run is None or (run.get("context") or {}).get("mode") == "critique":
        return review_definition(run, events)
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

    add("input", "Testing intent + PR", 0, 250, "input",
        "A user instruction or automatically inferred PR review goal and the pinned PR identify the run.")
    if run:
        nodes[-1]["status"] = "passed"
    preflight = [
        ("fetch_repository", "Fetch repository", "GitHub App fetches the PR and pinned source snapshot."),
        ("validate_instruction", "Define review goal", "Validate a supplied instruction or infer a review goal from pinned PR evidence."),
        ("analyze_framework", "Shared context", "Framework, files, commands, and applicability are shared with enabled agents."),
        ("map_impact", "Map PR impact", "Compare PR intent with changed code and related source; record evidence and uncertainty."),
        ("compare_api_contract", "Compare API contract", "Compare checked-in OpenAPI operations at pinned base and PR commits when available."),
        ("select_agents", "Prepare agent branches", "Start all three agent graphs; each specialist checks its own applicability independently."),
    ]
    shift = 280 * max(0, len(preflight) - 5)
    previous = "input"
    for index, (name, label, description) in enumerate(preflight):
        node_id = f"preflight.{name}"
        add(node_id, label, 280 * (index + 1), 250, "preflight", description)
        link(previous, node_id, "instruction" if index == 0 else "context")
        previous = node_id
    y_positions = {"builtin": 0, "playwright": 250, "vitest": 500}
    for agent, y in y_positions.items():
        steps = AGENT_STEPS[agent]
        applicability_event = latest.get(f"{agent}.applicability")
        stopped_at_applicability = bool(applicability_event and
                                        applicability_event["status"] == "not_selected")
        for index, name in enumerate(steps):
            node_id = f"{agent}.{name}"
            label = name.replace("_", " ").title()
            add(node_id, label, 1720 + shift + index * 250, y, agent,
                f"{AGENT_LABELS[agent]}: {label.lower()}. Receives the shared PR context and review goal.")
            if enabled_agents is not None and agent not in enabled_agents:
                nodes[-1]["status"] = "not_selected"
                reason = run["context"].get("inactive_agent_reasons", {}).get(agent)
                if reason:
                    nodes[-1]["description"] += f" Not selected: {reason}"
            elif stopped_at_applicability and index > 0 and nodes[-1]["event"] is None:
                nodes[-1]["status"] = "not_selected"
                reason = (applicability_event.get("detail") or {}).get("skip_reason")
                if reason:
                    nodes[-1]["description"] += f" Not applicable: {reason}"
            link(previous if index == 0 else f"{agent}.{steps[index - 1]}",
                 node_id, "shared context + instruction" if index == 0 else "node result")
    add("preview.start", "Start interactive preview", 2010 + shift, 750, "manual",
        "Optionally install and start the pinned PR application in a local disposable container.")
    link(previous, "preview.start", "pinned source + app command")
    add("preview.postgres_ready", "Preview database ready", 2390 + shift, 750, "manual",
        "Provision a disposable PostgreSQL service when the repository requires one.")
    if run and not (run["context"] or {}).get("analysis", {}).get("services"):
        nodes[-1]["status"] = "not_selected"
    link("preview.start", "preview.postgres_ready", "optional service")
    add("reviewer.observation", "Record human finding", 2890 + shift, 750, "manual",
        "A tester records steps, expected behavior, actual behavior, and a verdict.")
    link("preview.postgres_ready", "reviewer.observation", "hands-on evidence")
    add("preview.stop", "Stop preview", 3410 + shift, 750, "manual",
        "Save the application log and remove the disposable preview container.")
    link("preview.start", "preview.stop", "stop or two-hour limit")
    add("executor.apply_patches", "Apply patches", 4300 + shift, 250, "executor",
        "Validate and apply agent-produced test patches in a disposable checkout.")
    for agent in AGENT_LABELS:
        link(f"{agent}.{AGENT_STEPS[agent][-1]}", "executor.apply_patches", "validated patch")
    add("executor.start_runner", "Start local runner", 4570 + shift, 250, "executor",
        "Start an isolated local container with the pinned source snapshot.")
    add("executor.setup", "Prepare test environment", 4840 + shift, 250, "executor",
        "Install dependencies and prepare repository-required services. Start the app only when a runnable Playwright branch needs it.")
    link("executor.apply_patches", "executor.start_runner")
    link("executor.start_runner", "executor.setup")
    security_events = [event for event in events if event["stage"] == "security"
                       and event["agent"] == "executor" and event["node"]]
    for index, name in enumerate(dict.fromkeys(event["node"] for event in security_events)):
        node_id = f"executor.{name}"
        add(node_id, "Security " + name.replace("-", " ").title(),
            4900 + shift, -250 - index * 120, "security",
            "Run an evidenced, repository-declared security command in the disposable runner.")
        link("executor.setup", node_id, "security check")
    command_events = [event for event in events if event["agent"] == "executor"
                      and (event["node"] or "").endswith("-commands")]
    for index, name in enumerate(dict.fromkeys(event["node"] for event in command_events)):
        node_id = f"executor.{name}"
        add(node_id, name.replace("-", " ").title(), 4710 + shift, 590 + index * 120,
            "executor", "Validate this agent's suite commands. A rejected command does not prevent other agents' suites from running.")
        link("executor.apply_patches", node_id, "validate suite")
    database_required = bool(run and run["context"] and
                             run["context"].get("analysis", {}).get("services"))
    add("executor.postgres_ready", "Test database ready", 5050 + shift, 250, "executor",
        "Start a disposable PostgreSQL service on the runner's private network when repository evidence requires it.")
    if run and not database_required:
        nodes[-1]["status"] = "not_selected"
    link("executor.setup", "executor.postgres_ready", "required service")
    add("executor.database_snapshot", "Inspect database schema", 5260 + shift, 250, "executor",
        "Capture the disposable database schema after repository-declared setup.")
    if run and not database_required:
        nodes[-1]["status"] = "not_selected"
    link("executor.postgres_ready", "executor.database_snapshot", "schema evidence")
    suite_events = [event for event in events if event["stage"] == "execution" and
                    event["agent"] == "executor" and event["node"] and
                    event["node"] not in {"apply_patches", "start_runner", "setup", "postgres_ready", "database_snapshot", "baseline_visual", "compare_visual", "collect", "commit_tests"}
                    and not event["node"].endswith("-commands")]
    suite_names = list(dict.fromkeys(event["node"] for event in suite_events)) or ["builtin suite", "playwright suite", "vitest suite"]
    for index, name in enumerate(suite_names):
        node_id = f"executor.{name}"
        add(node_id, name.replace("-", " ").title(), 5490 + shift, 60 + index * 190, "suite",
            "Runs a test command in parallel with other active suites. Every suite completes and contributes evidence.")
        link("executor.database_snapshot" if database_required else "executor.setup",
             node_id, "parallel suite")
    add("executor.baseline_visual", "Review base revision", 5750 + shift, 750, "executor",
        "Capture the same browser routes on the pinned base revision when its test environment is available.")
    add("executor.compare_visual", "Compare screenshots", 6000 + shift, 750, "executor",
        "Pair base and PR screenshots; byte differences are observations for review, not automatic layout defects.")
    browser_applicable = bool(run and run["context"] and
                              "playwright" in run["context"].get("enabled_agents", []) and
                              run["context"].get("analysis", {}).get("playwright", {}).get("target") == "browser")
    if run and not browser_applicable:
        nodes[-2]["status"] = "not_selected"
        nodes[-1]["status"] = "not_selected"
    for name in suite_names:
        link(f"executor.{name}", "executor.baseline_visual", "after suites")
    link("executor.baseline_visual", "executor.compare_visual", "base + PR screenshots")
    add("executor.upgrade_postgres_ready", "Upgrade database ready", 5750 + shift, 1110, "executor",
        "Provision a second disposable PostgreSQL database for base-to-PR migration review.")
    add("executor.database_upgrade", "Review seeded migration", 6000 + shift, 1110, "executor",
        "Prepare the base schema, seed repository fixtures, run PR migrations, and compare surviving row counts.")
    upgrade_applicable = bool(run and database_required and any(
        service.get("baseline_setup_commands") and service.get("seed_commands") and
        service.get("upgrade_commands") for service in
        run["context"].get("analysis", {}).get("services", [])))
    if run and not upgrade_applicable:
        nodes[-2]["status"] = "not_selected"
        nodes[-1]["status"] = "not_selected"
    for name in suite_names:
        link(f"executor.{name}", "executor.upgrade_postgres_ready", "after suites")
    link("executor.upgrade_postgres_ready", "executor.database_upgrade", "seed + migrate")
    add("executor.collect", "Collect evidence", 6250 + shift, 250, "executor",
        "Save structured test results, logs, traces, and patch artifacts.")
    for name in suite_names:
        link(f"executor.{name}", "executor.collect", "suite result")
    link("executor.compare_visual", "executor.collect", "comparison evidence")
    link("executor.database_upgrade", "executor.collect", "migration evidence")
    add("executor.commit_tests", "Commit generated tests", 6520 + shift, 250, "publication",
        "After successful execution, fast-forward the PR head with validated generated test files only.")
    link("executor.collect", "executor.commit_tests", "validated tests")
    add("report.classify_failures", "Classify failures", 6800 + shift, 250, "investigation",
        "Rules-based classification reads real events, exit codes, and logs.")
    link("executor.commit_tests", "report.classify_failures", "evidence")
    link("executor.collect", "report.classify_failures", "partial evidence")
    link("reviewer.observation", "report.classify_failures", "report refresh")
    add("report.write_analysis", "Write analysis", 7080 + shift, 250, "report",
        "Report agent receives instruction, test results, failures, and evidence IDs.")
    add("report.verify_evidence", "Verify claims", 7360 + shift, 250, "report",
        "Reject report claims that cite missing or unsupported evidence.")
    add("report.publish", "GitHub + dashboard", 7640 + shift, 250, "report",
        "Publish an agent-written report to the PR and dashboard.")
    link("report.classify_failures", "report.write_analysis")
    link("report.write_analysis", "report.verify_evidence")
    link("report.verify_evidence", "report.publish")
    return {"nodes": nodes, "edges": edges,
            "run": {"id": run["id"], "repository": run["repository"],
                    "pr_number": run["pr_number"], "head_sha": run["head_sha"],
                    "status": run["status"], "stage": run["stage"],
                    "instruction": (run["context"] or {}).get("instruction", run["instruction"]),
                    "context": run["context"]} if run else None}
