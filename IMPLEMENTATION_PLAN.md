# PR Testing Agent Framework — Selected Implementation Plan

## Goal

Build a minimal Next.js frontend and Python backend that accepts a GitHub repository or PR link, lets a user say exactly what to test, generates tests, runs them in a local isolated environment, investigates failures, and publishes an agent-written review in the dashboard and on the GitHub PR.

This version records the implementation choices provided by the user. It is a plan; no application code has been implemented yet.

## 1. Selected decisions

| Area | Selected behavior |
| --- | --- |
| Failure policy | **Global fail-fast:** the first failed node stops the run and cancels other active agents or suites. Final evidence collection and reporting still run. |
| GitHub integration | **GitHub App** for repository access, PR webhooks, checks, and PR reporting. |
| Shared context | **Shared preflight** creates one immutable repository and PR context record before agent branches start. |
| Framework choice | Use the **repository's existing test framework**. If none exists, ask the user to choose **Playwright, Vitest, or both**; these are the only new-framework choices in the first version. |
| Testing instruction | One **free-text “What should be tested?”** field. It must describe observable behavior and an expected result. |
| Agent graphs | **LangGraph** defines each agent's ordered nodes, state, prompts, and skill access. |
| Durable orchestration | **Temporal** owns the run lifecycle, parallel branches, cancellation, retries, and recovery. |
| Applicability | Attempt an **adapter** when an enabled agent does not directly fit the repository. If no valid adapter exists, that agent fails and global fail-fast applies. |
| Generated tests | Agents return **patches only**. Patches are validated and applied in an isolated local checkout for execution. |
| Test publication | Keep generated test patches as **run artifacts**. Never commit them to the PR branch. |
| Runner | Execute repository code and browser tests in **local isolated containers** on our infrastructure. |
| Suite scheduling | **Parallel suite nodes inside the Test Execution Agent**; the agent's surrounding stages stay sequential. |
| Failure investigation | **Rules-based classification** using recorded execution facts. |
| Final report | A **report-writing agent** generates the full analysis from evidence. No deterministic prose template. |
| Report destinations | Show results in the **dashboard and on the GitHub PR** through a check run and an updated PR comment; no repository files are changed. |

The choices of GitHub check plus PR comment, Server-Sent Events for UI updates, PostgreSQL for run data, and local artifact files resolve previously unselected details with feasible initial implementations.

## 2. End-to-end flow

```text
Repository/PR link + “What should be tested?” + framework choice if needed
                              ↓
GitHub App intake and shared preflight at pinned base/head commits
                              ↓
                    Temporal supervisor
          ┌───────────────────┼───────────────────┐
          ↓                   ↓                   ↓
  Built-in Change       Playwright Agent      Vitest Agent
  Agent graph           graph                 graph
  nodes in order        nodes in order        nodes in order
          └───────────────────┼───────────────────┘
                              ↓
               Validate and apply test patches
                              ↓
                  Test Execution Agent
          sequential setup → parallel suite nodes → collect
                              ↓
                Rules-based failure investigation
                              ↓
                  Evidence collection
                              ↓
                  Report-writing agent
                              ↓
             Dashboard + GitHub check + PR comment
```

The three enabled agent branches start together after preflight. Each branch's own nodes run in order. The execution agent has an explicit nested fan-out for test suites: its setup and collection stages remain sequential, while the suite nodes run concurrently.

**Global fail-fast rule:** when a preflight, agent, patch-validation, or test-suite node returns `false`, Temporal requests cancellation of all other active work and starts the failure finalization path. No later test-generation or execution stage begins. Already produced evidence is retained. Failure investigation, evidence collection, and report generation are finalization stages, so a failed run still gets a useful report. Cancellation is cooperative at graph-node boundaries and forceful for local runner containers after a short grace period.

Every node that actually executes returns `success: true` or `success: false` with evidence. A cancelled or never-started node is recorded as `cancelled` or `not_started` and has no invented Boolean result.

## 3. GitHub App and repository intake

1. The user enters a repository URL and selects a PR, or enters a direct PR URL.
2. The UI checks whether the GitHub App is installed for that repository and offers the installation flow if needed.
3. The backend fetches repository details, PR metadata, changed files, base/head commit IDs, and installation identity.
4. The user enters the testing instruction and, only if the repository has no test framework, selects Playwright, Vitest, or both.
5. The backend starts a run tied to that exact head commit.
6. The GitHub App listens for relevant PR events such as `opened`, `synchronize`, and `reopened`; a new head commit creates a new run. A webhook-triggered rerun reuses the most recently saved testing instruction and framework selection for that PR.

Verify webhook signatures, deduplicate delivery IDs, and prevent an older run from replacing the current PR check or comment. A submitted link performs the initial API fetch; the GitHub App installation enables later webhook deliveries. See [GitHub App webhooks](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/using-webhooks-with-github-apps), [PR webhook events](https://docs.github.com/en/webhooks/webhook-events-and-payloads), and [webhook validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries).

The app will request only the permissions needed to read repository contents and PR data and to write the selected check and PR report. Generated tests never require repository-content write permission. GitHub check runs require a GitHub App with Checks write permission. See [GitHub App permissions](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app) and [check runs](https://docs.github.com/en/rest/checks/runs).

**Local deployment constraint:** GitHub must reach the webhook endpoint over the internet. For local development, expose the local FastAPI webhook through an HTTPS tunnel; for a persistent deployment, use a public HTTPS ingress. Test execution can still remain on the local machine.

## 4. Shared preflight and framework selection

Shared preflight produces one immutable context object for the pinned commit. It includes:

- PR diff, changed paths, base/head commits, repository tree, relevant source files, and existing tests.
- Application language and framework, package manager, test frameworks, scripts, CI commands, and service startup commands.
- The user's testing instruction, selected fallback framework(s), and an initial list of target behaviors.
- Applicability and adapter decisions for the Playwright and Vitest agents.

Agent 1 continues with deeper change and test analysis, but all agents receive the same preflight snapshot. Agent 1's later findings do not silently mutate the shared context of agents already running.

Framework policy:

1. If the repository already has a test framework, Agent 1 generates repository-native test cases in that framework. The specialist agents use Playwright and Vitest where compatible.
2. If no test framework exists, the UI asks the user to choose Playwright, Vitest, or both. Only selected specialist branches are enabled for that run; Agent 1 still analyzes the diff and built-in scripts.
3. An **enabled** specialist agent that is not directly compatible tries a defined adapter. For example, Playwright may test an HTTP API if the repository exposes one. An adapter must preserve that agent's test purpose and produce an executable test. If no valid adapter exists, the applicability node returns `false` and the whole run stops.
4. A branch the user did not select is recorded as `not_selected`, not as a failed agent. Global fail-fast applies to enabled branches.

This active-branch rule resolves the conflict between user-selected fallback frameworks and failing an inapplicable agent. Repositories with no compatible Playwright or Vitest target cannot start a successful fallback run in the first version; preflight should explain the reason before expensive generation work.

## 5. User instruction, prompts, skills, and agent nodes

The intake form has one required free-text field labeled **“What should be tested?”**. Its helper text asks for the behavior, action or input, and expected result. Example: “For the login form, wrong passwords must show an error without creating a session; valid credentials must open the dashboard.” A preflight validation node asks the user to improve an instruction that has no observable expected result before starting the agents.

Store the exact instruction with the run. Pass it to each agent as user intent, separate from the agent's versioned system prompt and the repository content. Each agent records which requested behaviors it covers and which it cannot cover. Webhook reruns use the saved instruction until the user edits it in the dashboard.

Each agent has its own system prompt, allowed tools, allowlisted skills, and typed graph state. Skill selection uses the PR diff and detected framework. Repository files are context only and cannot change an agent's system prompt or grant new tools.

| Agent | Sequential LangGraph nodes |
| --- | --- |
| **1. Built-in Change Agent** | Inspect diff and repository context → confirm framework and scripts → map changed behavior and user instruction to test cases → generate native-framework test patch if applicable → select built-in commands → validate output |
| **2. Playwright Agent** | Check applicability or adapter → map requested UI/API behavior → plan browser/API cases → generate Playwright patch → validate output |
| **3. Vitest Agent** | Check applicability or adapter → map requested unit behavior → plan unit cases → generate Vitest patch → validate output |

Every generated test case records its target behavior, expected result, source requirement, target framework, and patch location. Example node result:

```json
{
  "node": "generate_playwright_patch",
  "success": true,
  "evidence": ["requested login failure behavior", "changed app/login/page.tsx"],
  "artifacts": ["runs/123/patches/playwright.diff"],
  "error": null
}
```

Use LangGraph to define the agent graphs and Temporal to run them durably. Temporal's documented LangGraph integration supports graph nodes as Temporal Activities with timeouts and retries; that integration is currently labeled **Public Preview**, so pin compatible versions and prove cancellation and replay behavior early. See [Temporal's LangGraph integration](https://docs.temporal.io/develop/python/integrations/langgraph).

## 6. Patches and local test execution

Agents return unified-diff patches and test-case metadata. They do not directly write to a shared checkout or GitHub. Validate each patch for allowed test paths, syntax, framework compatibility, and overlap with another patch. Apply accepted patches to a disposable checkout of the pinned PR commit inside the runner. Store the original patches as downloadable artifacts. A patch conflict or validation failure is a failed node and triggers global fail-fast.

The local runner uses isolated containers with resource and time limits. GitHub credentials remain in the backend, outside containers executing PR code. The runner receives a source snapshot, accepted patches, and narrowly scoped configuration. Give browser tests access to the application under test through a private container network. Dependency installation needs a controlled package source or local cache.

The Test Execution Agent's stages are:

1. Create disposable checkout and apply accepted patches.
2. Install dependencies and start required services.
3. Fan out **parallel suite nodes** for applicable built-in, Playwright, and Vitest commands.
4. On the first suite failure, cancel sibling suites and stop later execution work.
5. If all suites pass, collect their structured results and artifacts.

The suite fan-out is the deliberate exception to sequential execution inside the Test Execution Agent. Its outer stages remain sequential. Each suite node still returns its own Boolean result. Playwright traces and Vitest machine-readable reports provide evidence for their respective suites. See [Playwright traces](https://playwright.dev/docs/trace-viewer) and [Vitest reporters](https://vitest.dev/guide/reporters).

## 7. Failure investigation and report generation

Rules-based failure investigation reads node outcomes, process exit codes, timeouts, test result files, logs, and patch validation errors. It classifies failures as application assertion failure, generated-test failure, environment/setup failure, unsupported adapter, or orchestration failure. It records the rule and evidence used for each classification; it does not invent a diagnosis when evidence is insufficient.

The **Report Generation Agent** runs after success or failure finalization. It receives the user's instruction, PR diff, test-case plans, generated patches, execution results, classifications, and artifact links. Its sequential nodes are: gather evidence → compare covered and uncovered requested behaviors → write the analysis → verify cited claims against artifacts → publish.

The agent writes the complete report narrative without a fixed prose template. Its output must still include the tested commit, detected and selected frameworks, tests generated and run, pass/fail/cancelled counts, findings, evidence links, limitations, and uncovered user requests. A structured report record supports the UI and GitHub publishing. Unsupported claims fail report verification and return to the writing node for correction within a bounded retry limit.

Publish the report to the dashboard, a GitHub check run on the tested head commit, and one bot comment on the PR that is updated for a newer run. The check and comment link to the dashboard's evidence. Publishing does **not** commit test code or any other file to the PR branch.

## 8. Minimal UI, API, and storage

**Next.js UI**

1. **Intake:** repository or PR URL, installation state, PR selector, required free-text testing instruction, and Playwright/Vitest choice only when no existing test framework is found.
2. **Run:** shared preflight result, three agent lanes with node statuses, execution suite nodes, current stage, and cancellation reason.
3. **Results:** agent-written analysis, requested-behavior coverage, failures, logs, test patches, traces, and GitHub report link.

Use Server-Sent Events for live run updates, with event replay on reconnect and a normal GET endpoint for refresh. This avoids a persistent bidirectional socket for a read-mostly screen.

**Python backend:** FastAPI endpoints for resolving a URL, creating a run, reading a run, streaming events, downloading authorized artifacts, and receiving GitHub App webhooks. Temporal workers handle the long-running run; FastAPI returns a run ID promptly.

**Storage:** PostgreSQL stores users/installations, repository and PR settings, saved testing instructions, runs, node results, events, and report metadata. Local artifact storage holds patches, logs, traces, and rendered reports under run-scoped paths. Temporal uses persistent storage for workflow history. The initial deployment can use local containers for the backend services, database, workflow engine, and runner; the webhook still needs public HTTPS reachability.

## 9. Build sequence and verification

1. Define typed run, context, node result, cancellation, test-case, patch, evidence, and report schemas.
2. Build the GitHub App intake flow and webhook receiver; pin PR commits and deduplicate deliveries.
3. Build shared preflight, framework detection, user-instruction validation, and active-agent selection.
4. Implement one LangGraph agent with Temporal execution; verify node order, Boolean results, replay, and global cancellation before adding other agents.
5. Add all three agent prompts, skill routing, adapters, case planning, and patch generation.
6. Add patch validation, disposable checkout, isolated local containers, and parallel suite fan-out with cancellation.
7. Add rules-based failure classification and the evidence-grounded report-writing agent.
8. Add GitHub check/comment publishing and the minimal Next.js intake, run, and results screens.
9. Verify end to end with: a passing PR; an application test failure; an agent-node failure while other agents run; a suite failure while other suites run; a webhook rerun on a new commit; a repository with no existing test framework; and an enabled agent with no valid adapter.

## 10. Feasibility limits to keep visible

- **Global fail-fast gives partial evidence.** Cancelled agents and suites may have no result. The report must state what was not run.
- **An adapter cannot make every repository compatible.** If an enabled Playwright or Vitest agent has no executable target, the selected policy fails the entire run. The UI should show this during preflight where possible.
- **A local runner does not make the webhook local-only.** GitHub needs a reachable HTTPS endpoint; the runner itself stays local.
- **The Temporal–LangGraph integration is in Public Preview.** The first technical milestone must verify the chosen versions, node result persistence, and cancellation behavior before the rest of the system depends on it.
- **Agent-written reports need evidence checks.** The report verifier must reject claims with no matching node result, log, test result, or patch artifact.
