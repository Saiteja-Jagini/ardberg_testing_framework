# Ardberg PR testing

Ardberg is a local Next.js and Python application for testing GitHub pull requests. A GitHub App fetches a pinned PR snapshot. Shared preflight identifies the repository and its test framework. LangGraph agents plan and generate tests in parallel, Temporal coordinates global fail-fast behavior, and a local container runner executes the generated patches. An agent writes an evidence-linked report for the dashboard and PR.

The separate **Agent flow** tab is a read-only visual map inspired by n8n workflow canvases. It shows the saved testing instruction, shared context, live node states, suite fan-out, and report stages. Node states come from the backend event log. The overview lists recent manual and webhook runs.

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

6. Open http://127.0.0.1:3000. Enter a repository or PR URL, a detailed free-text instruction, and choose Playwright and/or Vitest only when the repository has no native test framework.

GitHub must be able to reach the webhook at /webhooks/github over public HTTPS. For local development, use an HTTPS tunnel that exposes only this webhook path. The dashboard and API are intended to stay on localhost. Set PUBLIC_DASHBOARD_URL only when the dashboard is reachable by PR reviewers.

## Behavior

- The three agent definitions are Built-in Change, Playwright, and Vitest. Only enabled agents run. Existing native tests are handled by the Built-in Change Agent; when no native framework exists, the UI selection enables Playwright, Vitest, or both.
- Each agent has its own system prompt and LangGraph node chain. A model chooses from an allowlisted skill catalog using PR context.
- Generated tests are unified-diff artifacts and run first in a disposable checkout. After every suite passes, the GitHub App commits the validated test files to the exact PR head with a non-force update. A failed run keeps its patches as artifacts and still gets a report.
- A failed node cancels active sibling agents or suites. Failure classification, evidence collection, and the report-writing agent still run.
- The final report appears in the dashboard, a GitHub check, and an updated PR comment. The report is generated from real node events and test results.
- Changed PR documents are included in the agent's evidence review and discussed in the website report, with truncation or missing evidence stated explicitly.
- Preflight uses a pinned base/head comparison. Existing test frameworks are detected from repository files. Specialist branches run only when their evidence-backed targets are executable; an explicitly selected fallback without a valid adapter fails preflight.
- Test-only environment values must be present verbatim in repository context before they are passed to the disposable runner. Application secrets and GitHub credentials are never passed to it.
- If repository evidence requires PostgreSQL, preflight declares a test service and its connection variable. The runner starts a disposable database on the private test network, supplies a generated test-only URL, and runs repository-derived schema setup commands before suites. It never uses Ardberg's own database.
- New commits trigger webhook runs for PRs with a saved testing instruction. Open them from Recent runs in the overview.

## Configuration and limits

- OPENAI_API_KEY and GitHub App credentials are required for a real run. The UI reports missing configuration; it does not substitute fake test results.
- Repository source snippets, PR diff context, test plans, generated patches, and execution evidence are sent to the configured OpenAI model for analysis and report writing. The intake UI discloses this before PR analysis; use the app only with repositories authorized for that processing.
- The runner image includes Node.js, Playwright browsers, and Python. Repository-specific dependencies are installed inside the disposable container. Other language runtimes need an added runner image.
- The optional PostgreSQL test image is configured by POSTGRES_TEST_IMAGE; other external services need runner support before they can be used in a test run.
- Dependency installation uses the configured npm and Python indexes. Test processes run on a private Docker network after installation; install scripts still execute while the package source is reachable.
- The first version supports GitHub.com URLs. A selected specialist agent with no executable target or adapter fails the run.
- The Temporal LangGraph integration is in Public Preview. Keep compatible dependency versions pinned during deployment and verify replay/cancellation in your environment.
- The local API has no multi-user authentication. Bind it to localhost and expose only the signed webhook path through a tunnel or reverse proxy.

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
.\.venv\Scripts\python.exe -m tests.manual_no_native_vitest_smoke
.\.venv\Scripts\python.exe -m tests.manual_playwright_adapter_smoke
.\.venv\Scripts\python.exe -m tests.manual_postgres_smoke
~~~

A full PR run requires the GitHub App credentials and model key.
