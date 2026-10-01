import asyncio
from copy import deepcopy
from pathlib import Path
import re
import subprocess
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from datetime import timedelta
from temporalio.common import RetryPolicy
from temporalio import activity

from .artifacts import write_artifact
from .events import emit
from .llm import parse
from .schemas import CommandSelection, GeneratedPatch, TestPlan
from .skills import choose
from .inspection import validate_specialist


PROMPT_DIR = Path(__file__).parent / "prompts"
AGENTS = ("builtin", "playwright", "vitest")


class AgentState(TypedDict, total=False):
    run_id: str
    agent: str
    context: dict[str, Any]
    skills: list[str]
    skill_text: str
    adapter: str
    plan: dict[str, Any]
    patch: dict[str, Any]
    commands: list[str]
    failed: bool
    error: str


def _prompt(agent: str) -> str:
    return (PROMPT_DIR / f"{agent}.txt").read_text(encoding="utf-8")


async def _run_stage(state: AgentState, name: str, function) -> dict:
    run_id, agent = state["run_id"], state["agent"]
    emit(run_id, "agents", "running", agent=agent, node=name,
         detail={"instruction_received": bool(state["context"].get("instruction"))})
    async def heartbeat():
        while True:
            activity.heartbeat()
            await asyncio.sleep(2)

    heartbeat_task = asyncio.create_task(heartbeat()) if activity.in_activity() else None
    try:
        update = await function(state)
        emit(run_id, "agents", "passed", agent=agent, node=name, success=True,
             detail={key: value for key, value in update.items() if key not in {"skill_text", "patch"}})
        return update
    except Exception as exc:
        emit(run_id, "agents", "failed", agent=agent, node=name, success=False,
             detail={"error": str(exc)})
        return {"failed": True, "error": f"{agent}.{name}: {exc}"}
    finally:
        if heartbeat_task:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)


async def select_skills(state: AgentState) -> dict:
    names, skill_text = await choose(state["agent"], state["context"])
    return {"skills": names, "skill_text": skill_text}


async def applicability(state: AgentState) -> dict:
    agent = state["agent"]
    if agent == "builtin":
        return {}
    analysis = state["context"]["analysis"]
    decision = analysis[agent]
    valid, reason = validate_specialist(agent, decision, state["context"]["files"], analysis)
    if not valid:
        raise ValueError(f"{agent} has no executable target or valid adapter: {reason}")
    return {"adapter": decision if decision["mode"] == "adapter" else {}}


async def inspect_change(state: AgentState) -> dict:
    changed = state["context"]["changed_files"]
    if not changed:
        raise ValueError("Pinned PR comparison contains no changed files")
    return {"change_summary": {
        "paths": [item["filename"] for item in changed],
        "statuses": {item["filename"]: item.get("status", "unknown") for item in changed},
    }}


async def confirm_framework(state: AgentState) -> dict:
    context = state["context"]
    analysis = context["analysis"]
    framework = analysis["native_test_framework"].strip()
    if analysis["has_native_test_framework"] and not framework:
        raise ValueError("Preflight marked a native framework without naming it")
    if analysis["has_native_test_framework"] and not analysis["existing_test_commands"]:
        raise ValueError("Native test framework has no repository test command")
    return {"confirmed_framework": framework,
            "existing_commands": analysis["existing_test_commands"]}


async def map_requested_behavior(state: AgentState) -> dict:
    behaviors = state["context"]["behaviors"]
    if not behaviors:
        raise ValueError("Instruction has no testable requested behaviors")
    return {"requested_behaviors": behaviors}


async def plan_cases(state: AgentState) -> dict:
    context = state["context"]
    agent = state["agent"]
    if agent == "builtin" and not context["analysis"]["has_native_test_framework"]:
        return {"plan": TestPlan(cases=[], uncovered=[], rationale="No native test framework").model_dump()}
    if agent in {"playwright", "vitest"} and agent in context.get("preflight_plans", {}):
        return {"plan": TestPlan.model_validate(context["preflight_plans"][agent]).model_dump()}
    plan = await parse(
        _prompt(agent) + "\n\nSelected skills:\n" + state.get("skill_text", "") +
        "\nProduce concrete cases with expected results, source requirement, and "
        "the intended repository-relative test-file patch_location. "
        "Plan only cases that can be implemented with the supplied repository source, "
        "module APIs, supported services, and existing fixtures. If a case requires "
        "unavailable authentication or integration fixtures, list it as uncovered "
        "instead of inventing a test. Existing repository suites can be listed for "
        "execution without changing their files.",
        {"instruction": context["instruction"], "analysis": context["analysis"],
         "changed_files": context["changed_files"], "snippets": context["snippets"],
         "adapter": state.get("adapter", "")}, TestPlan,
    )
    if not plan.cases and agent != "builtin":
        raise ValueError("No executable test cases were planned")
    for case in plan.cases:
        if not case.behavior.strip() or not case.expected.strip() or not case.source.strip():
            raise ValueError("Every test case needs behavior, expectation, and source")
    return {"plan": plan.model_dump()}


async def generate_patch(state: AgentState) -> dict:
    if not state["plan"]["cases"]:
        return {"patch": GeneratedPatch(patch="", explanation="No native test framework", test_files=[], test_command="").model_dump()}
    context = state["context"]
    prompt = (_prompt(state["agent"]) + "\n\nSelected skills:\n" + state.get("skill_text", "") +
        "\nReturn one complete unified git diff patch that only adds or updates test files. "
        "Use diff --git and ---/+++ headers, actual repository paths, and no prose inside the patch. "
        "For every planned case, use its patch_location as the test file. "
        "List in test_files only files changed by the patch. Existing test files that are only "
        "run by a suite command must not appear in test_files. "
        "If a planned case cannot be implemented from actual repository APIs and fixtures, "
        "put its exact title and a concrete reason in uncovered_cases. Do not invent fixtures. "
        "Also provide the exact command to run the generated tests. "
        "Specialist commands must invoke playwright or vitest by name and target the generated test files; "
        "the executor adds machine-readable reporter flags. "
        "The patch will be checked with git apply --check; an invalid patch fails the run.")
    payload = {"cases": state["plan"], "files": context["files"],
               "snippets": context["snippets"], "analysis": context["analysis"],
               "instruction": context["instruction"]}
    for attempt in range(2):
        generated = await parse(prompt, payload, GeneratedPatch)
        try:
            plan = _prepare_generated_patch(state, generated)
            break
        except ValueError as exc:
            if attempt:
                raise
            relative = write_artifact(
                state["run_id"], f"patches/{state['agent']}-rejected.diff", generated.patch,
            )
            emit(state["run_id"], "agents", "artifact", agent=state["agent"],
                 node="generate_patch", detail={"path": relative,
                                                "rejection": str(exc)})
            payload = {**payload, "previous_output": generated.model_dump(),
                       "correction": str(exc)}
            prompt += ("\nThe previous output was rejected. Correct the exact error in "
                       "the payload and return the complete patch and metadata again. "
                       "Do not weaken or invent assertions.")
    generated.test_files = sorted(_patch_paths(generated.patch))
    relative = write_artifact(state["run_id"], f"patches/{state['agent']}.diff", generated.patch)
    emit(state["run_id"], "agents", "artifact", agent=state["agent"], node="generate_patch",
         detail={"path": relative, "test_files": generated.test_files})
    return {"patch": generated.model_dump(), "plan": plan}


def _prepare_generated_patch(state: AgentState, generated: GeneratedPatch) -> dict:
    if not generated.patch.strip() or not generated.test_files or not generated.test_command.strip():
        raise ValueError("Model did not provide a test patch, files, and execution command")
    actual_paths = _patch_paths(generated.patch)
    declared_paths = set(generated.test_files)
    if not actual_paths <= declared_paths:
        raise ValueError("Generated patch modifies an undeclared test file")
    plan = deepcopy(state["plan"])
    cases = plan["cases"]
    existing_files = set(state["context"]["files"])
    existing_case_paths = {case["patch_location"] for case in cases
                           if case["patch_location"] in existing_files}
    extra_paths = declared_paths - actual_paths
    if (state["agent"] != "builtin" and extra_paths) or not extra_paths <= existing_case_paths:
        raise ValueError("Generated patch declares files it does not modify")
    omitted = {item.title: item.reason for item in generated.uncovered_cases}
    if (len(omitted) != len(generated.uncovered_cases) or
            not set(omitted) <= {case["title"] for case in cases} or
            any(not reason.strip() for reason in omitted.values())):
        raise ValueError("Generated patch has invalid uncovered case declarations")
    retained_cases = []
    for case in cases:
        if case["title"] in omitted:
            plan["uncovered"].append(
                f"{case['title']}: {omitted[case['title']]}",
            )
            continue
        location = case["patch_location"]
        if not location and len(actual_paths) == 1:
            location = next(iter(actual_paths))
        if location not in actual_paths and not (state["agent"] == "builtin" and
                                                  location in existing_files):
            raise ValueError(f"No generated test file maps to case {case['title']}")
        case["patch_location"] = location
        retained_cases.append(case)
    if not retained_cases:
        raise ValueError("Generated patch covers none of the planned cases")
    plan["cases"] = retained_cases
    generated.test_files = sorted(actual_paths)
    return plan


def _test_path(path: str) -> bool:
    parts = Path(path).parts
    name = Path(path).name.lower()
    lower_parts = {part.lower() for part in parts}
    if (Path(path).is_absolute() or Path(path).drive or "\\" in path or
            ".." in parts or ".github" in lower_parts or not parts):
        return False
    return ("test" in name or "spec" in name or
            any(part.lower() in {"tests", "__tests__", "test", "spec"} for part in parts))


def _patch_paths(patch: str) -> set[str]:
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    if sections[0].strip() or len(sections) < 2:
        raise ValueError("Patch must contain only git diff sections")
    paths = set()
    for section in sections[1:]:
        lines = section.splitlines()
        match = re.fullmatch(r"diff --git a/(.+) b/(.+)", lines[0])
        if not match or match.group(1) != match.group(2):
            raise ValueError("Patch must modify one declared test path per section")
        path = match.group(1)
        if not _test_path(path):
            raise ValueError("Patch modifies a path outside test files")
        old_headers = [line for line in lines if line.startswith("--- ")]
        new_headers = [line for line in lines if line.startswith("+++ ")]
        if len(old_headers) != 1 or len(new_headers) != 1:
            raise ValueError("Patch requires one old and one new file header")
        if old_headers[0] not in {"--- /dev/null", f"--- a/{path}"} or new_headers[0] != f"+++ b/{path}":
            raise ValueError("Patch contains an undeclared file path or deletion")
        if any(line.startswith(("rename from ", "rename to ", "copy from ", "copy to ", "GIT binary patch")) for line in lines):
            raise ValueError("Patch renames, copies, or binary changes are unsupported")
        if any(line.startswith(("old mode ", "new mode ", "deleted file mode "))
               or (line.startswith("new file mode ") and line != "new file mode 100644")
               for line in lines):
            raise ValueError("Patch must use regular, non-executable test files")
        if path.casefold() in {item.casefold() for item in paths}:
            raise ValueError("Patch contains colliding test paths")
        paths.add(path)
    return paths


async def validate_patch(state: AgentState) -> dict:
    generated = state["patch"]
    patch = generated["patch"]
    if not patch:
        return {"validated_paths": []}
    files = generated["test_files"]
    if _patch_paths(patch) != set(files):
        raise ValueError("Patch modifies paths outside its declared test files")
    source = Path(state["context"]["source_path"])
    agent = state["agent"]
    for path in files:
        normalized = path.replace("\\", "/").lower()
        if agent in {"vitest", "playwright"}:
            owned = (f".ardberg.{agent}." in normalized or
                     f"tests/ardberg/{agent}/" in normalized)
            if not owned or (source / path).exists():
                raise ValueError(f"{agent} must create a new agent-owned test path")
        elif ".ardberg.vitest." in normalized or ".ardberg.playwright." in normalized:
            raise ValueError("Built-in agent cannot edit a specialist-owned test path")
    result = subprocess.run(
        ["git", "apply", "--check", "-"], cwd=source, input=patch.encode("utf-8"),
        capture_output=True, timeout=20,
    )
    if result.returncode:
        raise ValueError(f"git apply --check failed: {result.stderr.decode(errors='replace').strip()[:1000]}")
    return {"validated_paths": sorted(files)}


async def select_commands(state: AgentState) -> dict:
    generated = state["patch"]
    commands = [generated["test_command"]]
    if state["agent"] == "builtin":
        commands.extend(state["context"]["analysis"]["existing_test_commands"])
    commands = list(dict.fromkeys(command for command in commands if command.strip()))
    if state["agent"] == "builtin" and len(commands) > 1:
        manifest_snippets = {
            path: content for path, content in state["context"]["snippets"].items()
            if Path(path).name in {"package.json", "pyproject.toml", "Makefile"}
        }
        selection = await parse(
            _prompt("builtin") + "\nSelect the smallest set of exact candidate commands "
            "for distinct existing repository suites. The generated-case command is "
            "always included by the executor and does not need to be selected. "
            "Omit aliases or umbrella commands that duplicate a selected suite. "
            "Include every distinct existing suite explicitly requested in the user instruction. "
            "Do not invent or rewrite commands. Explain coverage briefly.",
            {"candidates": state["context"]["analysis"]["existing_test_commands"],
             "generated_command": generated["test_command"],
             "instruction": state["context"]["instruction"],
             "manifests": manifest_snippets}, CommandSelection,
        )
        existing = state["context"]["analysis"]["existing_test_commands"]
        chosen = [command for command in selection.commands if command in existing]
        ignored = [command for command in selection.commands if command not in existing]
        if generated["test_command"]:
            chosen.insert(0, generated["test_command"])
        instruction_words = set(re.findall(r"[a-z0-9]+", state["context"]["instruction"].lower()))
        for command in existing:
            suite = re.search(r"\btest:([a-z0-9:_-]+)", command.lower())
            if suite and set(re.findall(r"[a-z0-9]+", suite.group(1))) & instruction_words:
                chosen.append(command)
        if existing and not any(command in chosen for command in existing):
            chosen.append(existing[0])
        commands = list(dict.fromkeys(chosen))
        return {"commands": commands, "selection_rationale": selection.rationale,
                "ignored_unlisted_commands": ignored}
    if state["agent"] != "builtin" and not commands:
        raise ValueError("Generated tests have no execution command")
    return {"commands": commands}


async def builtin_inspect_change(state: AgentState) -> dict:
    return await _run_stage(state, "inspect_change", inspect_change)


async def builtin_confirm_framework(state: AgentState) -> dict:
    return await _run_stage(state, "confirm_framework", confirm_framework)


async def builtin_select_skills(state: AgentState) -> dict:
    return await _run_stage(state, "select_skills", select_skills)


async def builtin_map_requested_behavior(state: AgentState) -> dict:
    return await _run_stage(state, "map_requested_behavior", map_requested_behavior)


async def builtin_plan_cases(state: AgentState) -> dict:
    return await _run_stage(state, "plan_cases", plan_cases)


async def builtin_generate_patch(state: AgentState) -> dict:
    return await _run_stage(state, "generate_patch", generate_patch)


async def builtin_select_commands(state: AgentState) -> dict:
    return await _run_stage(state, "select_commands", select_commands)


async def builtin_validate_patch(state: AgentState) -> dict:
    return await _run_stage(state, "validate_patch", validate_patch)


async def playwright_select_skills(state: AgentState) -> dict:
    return await _run_stage(state, "select_skills", select_skills)


async def playwright_applicability(state: AgentState) -> dict:
    return await _run_stage(state, "applicability", applicability)


async def playwright_map_requested_behavior(state: AgentState) -> dict:
    return await _run_stage(state, "map_requested_behavior", map_requested_behavior)


async def playwright_plan_cases(state: AgentState) -> dict:
    return await _run_stage(state, "plan_cases", plan_cases)


async def playwright_generate_patch(state: AgentState) -> dict:
    return await _run_stage(state, "generate_patch", generate_patch)


async def playwright_validate_patch(state: AgentState) -> dict:
    return await _run_stage(state, "validate_patch", validate_patch)


async def playwright_select_commands(state: AgentState) -> dict:
    return await _run_stage(state, "select_commands", select_commands)


async def vitest_select_skills(state: AgentState) -> dict:
    return await _run_stage(state, "select_skills", select_skills)


async def vitest_applicability(state: AgentState) -> dict:
    return await _run_stage(state, "applicability", applicability)


async def vitest_map_requested_behavior(state: AgentState) -> dict:
    return await _run_stage(state, "map_requested_behavior", map_requested_behavior)


async def vitest_plan_cases(state: AgentState) -> dict:
    return await _run_stage(state, "plan_cases", plan_cases)


async def vitest_generate_patch(state: AgentState) -> dict:
    return await _run_stage(state, "generate_patch", generate_patch)


async def vitest_validate_patch(state: AgentState) -> dict:
    return await _run_stage(state, "validate_patch", validate_patch)


async def vitest_select_commands(state: AgentState) -> dict:
    return await _run_stage(state, "select_commands", select_commands)


async def route_after_stage(state: AgentState) -> str:
    return END if state.get("failed") else "next"


STAGES = {
    "builtin": [
        ("inspect_change", builtin_inspect_change),
        ("confirm_framework", builtin_confirm_framework),
        ("select_skills", builtin_select_skills),
        ("map_requested_behavior", builtin_map_requested_behavior),
        ("plan_cases", builtin_plan_cases), ("generate_patch", builtin_generate_patch),
        ("select_commands", builtin_select_commands),
        ("validate_patch", builtin_validate_patch),
    ],
    "playwright": [
        ("select_skills", playwright_select_skills),
        ("applicability", playwright_applicability),
        ("map_requested_behavior", playwright_map_requested_behavior),
        ("plan_cases", playwright_plan_cases),
        ("generate_patch", playwright_generate_patch),
        ("validate_patch", playwright_validate_patch),
        ("select_commands", playwright_select_commands),
    ],
    "vitest": [
        ("select_skills", vitest_select_skills), ("applicability", vitest_applicability),
        ("map_requested_behavior", vitest_map_requested_behavior),
        ("plan_cases", vitest_plan_cases),
        ("generate_patch", vitest_generate_patch),
        ("validate_patch", vitest_validate_patch),
        ("select_commands", vitest_select_commands),
    ],
}
STAGE_NAMES = {agent: [name for name, _ in steps] for agent, steps in STAGES.items()}


def build_agent_graph(agent: str):
    if agent not in AGENTS:
        raise ValueError(f"Unknown agent {agent}")
    steps = STAGES[agent]
    graph = StateGraph(AgentState)
    for name, function in steps:
        graph.add_node(
            name, function,
            metadata={"execute_in": "activity", "start_to_close_timeout": timedelta(
                seconds=540 if name == "generate_patch" else 240),
                      "heartbeat_timeout": timedelta(seconds=15),
                      "retry_policy": RetryPolicy(maximum_attempts=1)},
        )
    graph.add_edge(START, steps[0][0])
    for index, (name, _) in enumerate(steps):
        next_node = steps[index + 1][0] if index + 1 < len(steps) else END
        graph.add_conditional_edges(name, route_after_stage,
                                    {END: END, "next": next_node})
    return graph
