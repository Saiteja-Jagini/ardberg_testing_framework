# Ardberg PR critique

Ardberg is a local Next.js and Python application for reviewing GitHub pull requests. A GitHub App fetches pinned base and PR snapshots, PR description, and commit messages. Shared preflight maps change impact and discovers how the changed code can run. A behavior agent extracts expected behavior, a code critic records a code status and caller trace for every behavior, and a runtime planner selects focused browser, HTTP, or script probes. A disposable container runs the changed code and compares the base revision when useful. An evidence critic separates confirmed findings from potential or unverified concerns. The final critique is saved to the dashboard and published to the PR. The prior generated-test workflow is available only when explicitly selected; dashboard and webhook runs default to critique.

The separate **Agent flow** tab is a read-only visual map inspired by n8n workflow canvases. Each run URL loads its own diagram and event history, labelled with its PR, run ID, and commit. New runs show expected-behavior extraction, code critique, runtime planning, disposable execution, evidence classification, and report stages. The overview lists recent manual and webhook runs. The **Interactive preview** tab lets a reviewer start the pinned application in a disposable local container, open it in a browser, inspect logs, and record hands-on observations.

## Local setup

Prerequisites: Python 3.11+, Node.js 20+, Docker Desktop with Linux containers, a GitHub App, and an OpenAI API key.

1. Copy .env.example to .env. Fill in the GitHub App and OpenAI values. Put the GitHub private key in GITHUB_PRIVATE_KEY with literal backslash-n between lines, or set the variable in your shell with real newlines.
   Restart the API and worker after changing .env. The UI's connection indicators verify the GitHub App and configured OpenAI model.
2. Create the GitHub App with webhook subscriptions to \`pull_request\` (opened, synchronize, reopened) and \`installation\` events. Grant repository Contents write for generated-test commits, Pull requests read/write for the PR report, and Checks write. Set its webhook secret. Install the app on repositories to test.
3. Start local services and build the isolated runner:

   ~~~powershell
   docker compose up -d postgres temporal temporal-ui
   docker compose --profile build build runner-image
   ~~~

4. Install the Python backend:

   ~~~powershell
   cd backend
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ~~~

   Start the worker in one terminal and the API in another:

   ~~~powershell
   cd backend
   .\.venv\Scripts\python.exe -m app.worker
   ~~~

   ~~~powershell
   cd backend
   .\.venv\Scripts\python.exe -m uvicorn app.api:app --host 127.0.0.1 --port 8000
   ~~~

5. Start Next.js:

   ~~~powershell
   cd frontend
   npm.cmd install
   npm.cmd run dev
   ~~~

   The frontend uses the shadcn/ui `radix-nova` preset from [shadcn/create](https://ui.shadcn.com/create). Its generated components are in `frontend/src/components/ui`, and theme tokens are in `frontend/src/app/globals.css`.

6. Open http://127.0.0.1:3000. Enter a repository or PR URL. The optional review intent should describe what the change is expected to do. When blank, Ardberg tries to infer expectations from pinned PR evidence and labels any unresolved behavior as unverified.

   The API accepts the configured frontend port from both `localhost` and `127.0.0.1`. If Next.js uses another port or hostname, set `FRONTEND_ORIGIN` in `.env` to that browser address and restart the API.

GitHub must be able to reach the webhook at /webhooks/github over public HTTPS. For local development, use an HTTPS tunnel that exposes only this webhook path. The dashboard and API are intended to stay on localhost. Set PUBLIC_DASHBOARD_URL only when the dashboard is reachable by PR reviewers.

## Current review flow

- The behavior agent extracts expectations from user intent, PR evidence, and relevant documentation. The code critic traces the changed implementation and candidate callers, quotes supporting source, and records implemented, incomplete, contradictory, or unknown for each expectation. The runtime planner chooses at most 12 focused scenarios. These stages preserve ambiguity when the expected result cannot be established.
- The runner installs the pinned code in a disposable container and executes focused browser, local HTTP, or script probes. Browser and HTTP probes require an evidenced start command and readiness URL; library and CLI code can be invoked by a temporary script. Comparable probes may run against a separate pinned base checkout. Generated probe scripts stay in local artifacts and are not committed to the PR.
- The evidence critic marks a behavior confirmed missing only when a PR runtime observation visibly contradicts an evidenced expectation. It marks a regression only with a comparable base observation. Setup failures, unavailable fixtures, and incomplete source remain unverified. The report leads with the critique, impact, reproduction, and limits. A completed run means the review finished; the GitHub check can still fail for confirmed findings or be neutral when review evidence is incomplete.
- Existing test-suite execution and generated-test commits are not part of default critique runs. The dashboard's optional legacy testing choice, or `mode: "testing"` on `POST /api/runs`, starts the generated-test workflow instead. The Interactive preview remains available for a reviewer to inspect the pinned application and add separately attributed human observations.

## Legacy testing workflow

- Built-in Change, Playwright, and Vitest agent graphs start in parallel after shared preflight. Each specialist checks its own applicability and stops with a recorded reason when its target cannot be tested; this does not stop the other branches. Existing native tests are handled by Built-in Change. When no native framework exists, the UI selection decides which specialist branches may generate tests.
- The automated runner starts the repository application only when an applicable Playwright branch produces executable tests and an evidenced start command and readiness URL are available. Browser tests additionally require a browser target; a Playwright HTTP API adapter does not provide browser coverage. The Interactive preview can start the pinned application separately for hands-on review.
- When testing intent is blank, preflight infers observable feature checks from the PR description, commits, diff, and source. The original field stays blank in the run record; the inferred goal and its source are shown in the run flow and overview. If expected behavior cannot be established, available repository checks still run and the report marks the feature unverified. Passing existing suites alone does not make the automatic review successful when no executable feature case was planned. A supplied intent still needs an observable expected result.
- Each agent has its own system prompt and LangGraph node chain. A model chooses from an allowlisted skill catalog using PR context.
- Generated tests are unified-diff artifacts and run first in a disposable checkout. After every suite passes, the GitHub App commits the validated test files to the exact PR head with a non-force update. A failed run keeps its patches as artifacts and still gets a report.
- A failed node stops its own agent branch. Other agents and runnable test suites finish and contribute evidence. A failed branch or suite still makes the overall automated verdict fail. A shared preflight or runner setup failure prevents work that depends on it.
- A rejected test patch is kept as evidence; other accepted patches and their suites can still run. Generated tests are committed only when every enabled agent, patch, and suite succeeds.
- The Interactive preview tab starts the pinned PR application on a Docker port published to `127.0.0.1` for up to two hours. The app start command, container port, ready path, disposable service setup commands, and application environment can be adjusted. Choose the repository's full application command when the feature needs an API or worker. The tab also shows a command for checking out the exact tested commit in an IDE. Reviewer observations include steps, expected behavior, actual behavior, and a verdict. Once automation is done, the report agent can incorporate these as labeled human evidence and update the GitHub check and PR comment.
- The final report appears in the dashboard, a GitHub check, and an updated PR comment. The report is generated from real node events and test results.
- Changed PR documents are included in the agent's evidence review and discussed in the website report, with truncation or missing evidence stated explicitly.
- The run overview shows an evidence-linked impact map: claimed feature, affected UI/API/database/security areas, related files, proposed checks, browser routes, and uncovered prerequisites. Commit messages and PR text are treated as hints and checked against source evidence.
- The runner audits evidenced browser routes at 390, 768, and 1440 pixel widths, saves screenshots and overflow measurements, and flags loading errors or horizontal overflow. When a runnable base revision has no additional service fixture, it captures the same routes there and pairs screenshots. Byte differences are review evidence, not automatic layout defects.
- Checked-in OpenAPI operations are compared between base and PR. Native and specialist agents can generate focused API regression cases for evidenced routes; unsupported auth or service fixtures remain uncovered.
- When repository-declared security scan commands can be verified, the runner executes them and retains their logs. The Built-in agent also records source-backed potential concerns; it does not claim that an untested route is secure.
- When a repository provides evidenced base setup, seed, and upgrade commands, a second disposable PostgreSQL database exercises a base-to-PR migration. Row counts, fingerprints of original columns, and upgraded schema are saved. Changed or removed seeded records require review; intentional transformations are not automatically called defects.
- Preflight uses a pinned base/head comparison and detects existing test frameworks. All three agent graphs start after preflight; each specialist checks its evidence-backed target inside its own branch. A selected fallback without a valid adapter fails that branch while the others continue.
- Test-only environment values must be present verbatim in repository context before they are passed to the disposable runner. Application secrets and GitHub credentials are never passed to it.
- If repository evidence requires PostgreSQL, preflight declares a test service and its connection variable. The runner starts a disposable database on the private test network, supplies a generated test-only URL, and runs repository-derived schema setup commands before suites. It never uses Ardberg's own database.
- New commits trigger webhook runs for PRs with saved test settings, including a deliberately blank testing intent. Blank intent is inferred again from the new pinned commit. Open runs from Recent runs in the overview.

## Configuration and limits

- OPENAI_API_KEY and GitHub App credentials are required for a real run. The UI reports missing configuration; it does not substitute fake test results.
- Repository source snippets, PR diff context, test plans, generated patches, and execution evidence are sent to the configured OpenAI model for analysis and report writing. The intake UI discloses this before PR analysis; use the app only with repositories authorized for that processing.
- The runner image includes Node.js, Playwright browsers, and Python. Repository-specific dependencies are installed inside the disposable container. Other language runtimes need an added runner image.
- PREVIEW_MEMORY sets the interactive container memory cap independently of RUNNER_MEMORY. A heavy development server may need a larger value if the local Docker VM has enough memory; an OOM exit is shown with its saved application log.
- The optional PostgreSQL test image is configured by POSTGRES_TEST_IMAGE. When repository migrations require the `vector` extension, Ardberg selects the pgvector image configured by POSTGRES_VECTOR_IMAGE. Other external services need runner support before they can be used in a test run. The interactive preview prevents overrides of its generated disposable database connection variables.
- Dependency installation uses the configured npm and Python indexes. Test processes run on a private Docker network after installation; install scripts still execute while the package source is reachable.
- When an install or service setup command reports a missing prerequisite, the runner asks the repair agent for one safe installation command, runs it inside the disposable container, and retries the failed command. `SETUP_REPAIR_LIMIT` caps repairs per setup node at 2 by default (maximum 5). Attempt and repair logs are retained; an unresolved prerequisite fails that node. The same behavior applies to automated runs, baseline browser setup, database baseline installation, and interactive previews. For service setup after network isolation, network access is restored only during the repair command.
- The first version supports GitHub.com URLs. A user-selected fallback specialist with no executable target or adapter fails its branch while other branches continue. An automatically evaluated specialist with no executable target records a skipped branch.
- The Temporal LangGraph integration is in Public Preview. Keep compatible dependency versions pinned during deployment and verify replay and branch completion in your environment.
- Interactive previews run repository code with local browser access and retain network access for dependency installation and application use. Run them only for repositories you trust to execute on your machine. The preview container receives only repository-derived test environment values, never Ardberg credentials.
- The local API has no multi-user authentication. Bind it to localhost and expose only the signed webhook path through a tunnel or reverse proxy.
- The impact map is evidence-bound but cannot discover every dynamic dependency. The report must name unverified areas. Browser screenshots do not prove feature behavior; generated assertions and human preview observations remain separate.
- OpenAPI comparison needs a checked-in OpenAPI or Swagger file. Baseline browser execution is skipped when it needs additional service fixtures. Seeded migration review runs only when the repository provides verifiable commands and fixtures; the runner never reads production data.
- A security scan with no finding proves only that its selected checks found none. Repositories without a declared scanner receive a coverage gap, and static model concerns remain potential until tested.

## Verification commands

~~~powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
cd ..\frontend
npm.cmd run typecheck
npm.cmd run build
~~~

For local integration checks without GitHub or a model key, run the following from the backend directory after the services and runner image are ready:

~~~powershell
.\.venv\Scripts\python.exe -m tests.manual_runner_smoke
.\.venv\Scripts\python.exe -m tests.manual_failfast_smoke
.\.venv\Scripts\python.exe -m tests.manual_suite_failfast_smoke
.\.venv\Scripts\python.exe -m tests.manual_interactive_preview_smoke
.\.venv\Scripts\python.exe -m tests.manual_interactive_temporal_smoke
.\.venv\Scripts\python.exe -m tests.manual_report_refresh_smoke
.\.venv\Scripts\python.exe -m tests.manual_no_native_vitest_smoke
.\.venv\Scripts\python.exe -m tests.manual_playwright_adapter_smoke
.\.venv\Scripts\python.exe -m tests.manual_visual_review_smoke
.\.venv\Scripts\python.exe -m tests.manual_postgres_smoke
~~~

A full PR run requires the GitHub App credentials and model key.
