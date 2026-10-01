"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import ReactMarkdown from "react-markdown";
import { Activity, ArrowRight, Check, ChevronRight, CircleHelp, ExternalLink, GitBranch, GitPullRequest, LayoutDashboard, LoaderCircle, Play, Plus, RefreshCw, ShieldCheck, Workflow, X } from "lucide-react";
import AgentFlow from "./AgentFlow";
import { API_URL, api, type Connections, type FlowDefinition, type PullRequest, type RepoPreview, type ResolvedRepo, type Run, type RunEvent, type RunSummary } from "@/lib/api";

const agentNames: Record<string, string> = { builtin: "Built-in Change", playwright: "Playwright", vitest: "Vitest" };
const agentDescriptions: Record<string, string> = { builtin: "Repository context and native tests", playwright: "Browser and API behavior", vitest: "Focused unit tests" };

function reportHref(href: string | undefined, runId: string): string | undefined {
  if (!href) return undefined;
  if (href.startsWith("#") || /^https?:\/\//.test(href)) return href;
  const segments = href.split("/");
  if (["context", "patches", "logs", "evidence"].includes(segments[0])
      && segments.length > 1
      && segments.slice(1).every(segment => /^[A-Za-z0-9._-]+$/.test(segment) && segment !== "..")) {
    return API_URL + "/api/runs/" + runId + "/artifacts/" + segments.map(encodeURIComponent).join("/");
  }
  return undefined;
}

function latestFor(events: RunEvent[], agent: string) {
  return [...events].reverse().find(event => event.agent === agent && event.node && event.status !== "artifact");
}

export default function Dashboard({ initialRunId }: { initialRunId?: string }) {
  const router = useRouter();
  const [tab, setTab] = useState<"workspace" | "flow">("workspace");
  const [url, setUrl] = useState("");
  const [instruction, setInstruction] = useState("");
  const [frameworks, setFrameworks] = useState<string[]>([]);
  const [resolved, setResolved] = useState<ResolvedRepo | null>(null);
  const [selectedPr, setSelectedPr] = useState<PullRequest | null>(null);
  const [preview, setPreview] = useState<RepoPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [run, setRun] = useState<Run | null>(null);
  const [recentRuns, setRecentRuns] = useState<RunSummary[]>([]);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [flow, setFlow] = useState<FlowDefinition | null>(null);
  const [health, setHealth] = useState<{ github_configured: boolean; model_configured: boolean } | null>(null);
  const [connections, setConnections] = useState<Connections | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [installUrl, setInstallUrl] = useState("");

  const refresh = useCallback(async () => {
    if (!initialRunId) return;
    const [nextRun, nextEvents, nextFlow] = await Promise.all([
      api.run(initialRunId), api.events(initialRunId), api.flow(initialRunId),
    ]);
    setRun(nextRun); setEvents(nextEvents); setFlow(nextFlow);
  }, [initialRunId]);
  const eventCursor = useRef(0);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
    api.connections().then(setConnections).catch(() => setConnections({
      github: { connected: false, error: "Cannot reach the backend connection check" },
      model: { connected: false, model: "", error: "Cannot reach the backend connection check" },
    }));
    if (initialRunId) Promise.all([api.run(initialRunId), api.events(initialRunId), api.flow(initialRunId)])
      .then(([nextRun, nextEvents, nextFlow]) => {
        setRun(nextRun); setEvents(nextEvents); setFlow(nextFlow);
        eventCursor.current = Math.max(eventCursor.current, ...nextEvents.map(event => event.id), 0);
      }).catch(err => setError(err.message));
    else api.flow().then(setFlow).catch(() => {});
  }, [initialRunId, refresh]);

  useEffect(() => {
    if (initialRunId) return;
    const loadRuns = () => api.runs().then(setRecentRuns).catch(() => {});
    loadRuns();
    const timer = setInterval(loadRuns, 15000);
    return () => clearInterval(timer);
  }, [initialRunId]);

  useEffect(() => {
    if (!initialRunId || run?.status === "completed" || run?.status === "failed") return;
    const source = new EventSource(API_URL + "/api/runs/" + initialRunId + "/stream?after=" + eventCursor.current);
    source.addEventListener("node", (message) => {
      const id = Number((message as MessageEvent).lastEventId);
      if (Number.isFinite(id)) eventCursor.current = Math.max(eventCursor.current, id);
      refresh().catch(() => {});
    });
    source.addEventListener("done", () => { refresh().catch(() => {}); source.close(); });
    // EventSource reconnects automatically and sends its last event ID.
    return () => source.close();
  }, [initialRunId, refresh, run?.status]);

  useEffect(() => {
    if (!resolved || !selectedPr) return;
    let active = true;
    api.preview(resolved.repository, selectedPr.number)
      .then(result => { if (active) setPreview(result); })
      .catch(err => { if (active) setError(err.message); })
      .finally(() => { if (active) setPreviewing(false); });
    return () => { active = false; };
  }, [resolved, selectedPr]);

  async function resolveRepo(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError(""); setInstallUrl("");
    try {
      const data = await api.resolve(url);
      setResolved(data);
      setPreview(null); setFrameworks([]); setPreviewing(data.pull_requests.length === 1);
      setSelectedPr(data.pull_requests.length === 1 ? data.pull_requests[0] : null);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not load repository";
      setError(message);
      const match = message.match(/https:\/\/github\.com\/apps\/[^\s"}]+/);
      if (match) setInstallUrl(match[0]);
    } finally { setBusy(false); }
  }

  async function startRun() {
    if (!resolved || !selectedPr || instruction.trim().split(/\s+/).length < 4) {
      setError("Choose a PR and describe the behavior and expected result in the testing instruction.");
      return;
    }
    if (!preview) { setError("Wait for repository framework analysis before starting."); return; }
    if (preview.needs_framework_selection && frameworks.length === 0) {
      setError("No native test framework was found. Choose Playwright, Vitest, or both.");
      return;
    }
    setBusy(true); setError("");
    try {
      const result = await api.createRun({ repository: resolved.repository, pr_number: selectedPr.number,
        instruction: instruction.trim(), selected_frameworks: frameworks });
      router.push("/runs/" + result.run_id);
    } catch (err) { setError(err instanceof Error ? err.message : "Could not start run"); }
    finally { setBusy(false); }
  }

  function toggleFramework(name: string) {
    setFrameworks(value => value.includes(name) ? value.filter(item => item !== name) : [...value, name]);
  }

  function newRun() {
    if (initialRunId) { router.push("/"); return; }
    setUrl(""); setInstruction(""); setFrameworks([]);
    setResolved(null); setSelectedPr(null); setPreview(null);
    setPreviewing(false); setError(""); setInstallUrl("");
  }

  const agentEvents = Object.keys(agentNames).map(key => ({ key, current: latestFor(events, key) }));
  const passed = events.filter(event => event.status === "passed" && event.node).length;
  const failed = events.filter(event => event.status === "failed" && event.node).length;
  const artifacts = [...new Set(events.flatMap(event => {
    const detail = event.detail;
    return [detail.path, detail.log, ...(Array.isArray(detail.paths) ? detail.paths : [])]
      .filter((item): item is string => typeof item === "string");
  }))];

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Workflow size={20} /></div><div><strong>ardberg</strong><span>PR TESTING</span></div></div>
      <div className="sidebar-section">WORKSPACE</div>
      <button className={["nav-item", tab === "workspace" ? "active" : ""].join(" ")} onClick={() => setTab("workspace")}><LayoutDashboard size={17} /> Overview <ChevronRight size={15} /></button>
      <button className={["nav-item", tab === "flow" ? "active" : ""].join(" ")} onClick={() => setTab("flow")}><Workflow size={17} /> Agent flow <ChevronRight size={15} /></button>
      <div className="sidebar-bottom"><div className="sidebar-help"><CircleHelp size={17} /><div><strong>Agent flow</strong><span>Inspect each node and see how your instruction travels.</span></div></div><div className="sidebar-foot"><span className="online-dot" /> LOCAL WORKSPACE</div></div>
    </aside>
    <main className="main-area">
      <header className="topbar"><div className="breadcrumb">Workspace <ChevronRight size={14} /> <strong>{tab === "flow" ? "Agent flow" : "Overview"}</strong></div><div className="topbar-right"><span className={["topbar-pill", connections ? (connections.github.connected ? "connected" : "disconnected") : "checking"].join(" ")}>{connections?.github.connected ? <ShieldCheck size={14} /> : connections ? <X size={14} /> : <LoaderCircle size={14} className="spin" />}{connections ? (connections.github.connected ? "GitHub connected" : "GitHub disconnected") : "Checking GitHub"}</span><span className={["topbar-pill", connections ? (connections.model.connected ? "connected" : "disconnected") : "checking"].join(" ")}>{connections?.model.connected ? <ShieldCheck size={14} /> : connections ? <X size={14} /> : <LoaderCircle size={14} className="spin" />}{connections ? (connections.model.connected ? "Model connected" : "Model disconnected") : "Checking model"}</span><span className="avatar">A</span></div></header>
      {tab === "flow" ? (flow ? <AgentFlow flow={flow} instruction={instruction} /> : <div className="loading-page"><LoaderCircle className="spin" /> Loading agent flow...</div>) :
      <div className="workspace-content">
        <div className="page-intro"><div><div className="eyebrow"><span className="eyebrow-line" /> AUTOMATED PR REVIEW</div><h1>{run ? "Run overview" : "Test a pull request"}</h1><p>{run ? "Track the agents, test execution, and evidence for this review." : "Give the agents a PR and tell them what behavior matters. Follow every step as it runs."}</p></div><button className="ghost-button" onClick={newRun}><Plus size={16} /> New run</button></div>
        {error && <div className="error-banner"><X size={17} /><span>{error}</span>{installUrl && <a href={installUrl} target="_blank" rel="noreferrer">Install GitHub App <ExternalLink size={14} /></a>}</div>}
        {health && (!health.github_configured || !health.model_configured) && <div className="setup-banner"><CircleHelp size={18} /><span>Setup needed: {[
          !health.github_configured ? "GitHub App credentials" : "",
          !health.model_configured ? "OpenAI API key" : "",
        ].filter(Boolean).join(" and ")} are missing from the backend environment.</span></div>}
        {connections && ((health?.github_configured && !connections.github.connected) || (health?.model_configured && !connections.model.connected)) && <div className="setup-banner"><CircleHelp size={18} /><span>{[
          health?.github_configured && !connections.github.connected ? connections.github.error : "",
          health?.model_configured && !connections.model.connected ? connections.model.error : "",
        ].filter(Boolean).join(" · ")}</span></div>}
        {!run ? <><div className="intake-grid">
          <section className="panel intake-panel"><div className="panel-title"><span className="step-number">01</span><div><h2>Choose a pull request</h2><p>Connect a repository with the GitHub App.</p></div></div><p className="helper-row">Analyzing a selected PR sends repository source snippets and diff context to the configured OpenAI model.</p><form onSubmit={resolveRepo}><label htmlFor="repo-url">GitHub repository or PR link</label><div className="url-row"><GitBranch size={18} /><input id="repo-url" value={url} onChange={event => setUrl(event.target.value)} placeholder="https://github.com/owner/repository/pull/42" required /><button type="submit" disabled={busy}>{busy ? <LoaderCircle className="spin" size={16} /> : <ArrowRight size={17} />}</button></div></form>
            {resolved && <div className="pr-list"><div className="field-heading">{resolved.repository} <span>{resolved.pull_requests.length} open PRs</span></div>{resolved.pull_requests.length ? resolved.pull_requests.map(pr => <button key={pr.number} className={["pr-option", selectedPr?.number === pr.number ? "selected" : ""].join(" ")} onClick={() => { if (selectedPr?.number !== pr.number) { setPreview(null); setFrameworks([]); setPreviewing(true); setSelectedPr(pr); } }}><div className="pr-icon"><GitPullRequest size={17} /></div><div><strong>#{pr.number} {pr.title}</strong><small>{pr.head_sha.slice(0, 10)}</small></div><span className="radio-circle" /></button>) : <p className="empty-text">No open pull requests found.</p>}</div>}
          </section>
          <section className="panel instruction-panel">
            <div className="panel-title"><span className="step-number">02</span><div><h2>Set the testing intent</h2><p>This instruction goes to every enabled agent.</p></div></div>
            <label htmlFor="instruction">What should be tested?</label>
            <textarea id="instruction" value={instruction} onChange={event => setInstruction(event.target.value)} placeholder="For the login form, wrong passwords must show an error without creating a session. Valid credentials must open the dashboard." rows={7} />
            <div className="helper-row"><CircleHelp size={14} /> Describe the behavior, an action or input, and the expected result.</div>
            {previewing && <div className="framework-detected"><LoaderCircle size={15} className="spin" /> Detecting repository test framework…</div>}
            {preview && !preview.needs_framework_selection && <div className="framework-detected"><Check size={15} /> Native test framework: <strong>{preview.analysis.native_test_framework}</strong></div>}
            {preview?.needs_framework_selection && <>
              <div className="field-heading framework-heading">Choose a test framework <span>No native test framework found</span></div>
              <div className="framework-options">
                <button className={frameworks.includes("playwright") ? "selected" : ""} onClick={() => toggleFramework("playwright")}><span className="framework-icon pw">P</span><div><strong>Playwright</strong><small>Browser / API</small></div>{frameworks.includes("playwright") && <Check size={16} />}</button>
                <button className={frameworks.includes("vitest") ? "selected" : ""} onClick={() => toggleFramework("vitest")}><span className="framework-icon vt">V</span><div><strong>Vitest</strong><small>Unit tests</small></div>{frameworks.includes("vitest") && <Check size={16} />}</button>
              </div>
            </>}
            <button className="primary-button" disabled={busy || !selectedPr || !preview || previewing} onClick={startRun}><Play size={16} fill="currentColor" /> Start test run <ArrowRight size={17} /></button>
          </section>
        </div>
        {recentRuns.length > 0 && <section className="panel recent-runs">
          <div className="section-header"><div><div className="eyebrow">GITHUB APP + MANUAL RUNS</div><h2>Recent runs</h2></div><RefreshCw size={16} /></div>
          <div className="recent-run-list">{recentRuns.map(item =>
            <button key={item.id} className="recent-run" onClick={() => router.push("/runs/" + item.id)}>
              <span className="recent-run-pr">{item.repository} / #{item.pr_number}</span>
              <strong>{item.title || "PR testing run"}</strong>
              <span className="recent-run-sha">{item.head_sha.slice(0, 10)}</span>
              <span className={["status-badge", item.status].join(" ")}>{item.status}</span>
              <ArrowRight size={15} />
            </button>)}</div>
        </section>}</> :
        <><div className="run-hero panel"><div className="run-hero-main"><span className="run-label"><GitPullRequest size={15} /> {run.repository} · PR #{run.pr_number}</span><h2>{run.title}</h2><div className="run-meta"><span><GitBranch size={14} /> {run.head_sha.slice(0, 12)}</span><span className={["status-badge", run.status].join(" ")}>{run.status}</span><span>Stage: {run.stage}</span></div></div><button className="outline-button" onClick={() => refresh().catch(err => setError(err.message))}><RefreshCw size={15} /> Refresh</button></div>
          <div className="stats-row"><div className="stat-card"><span>AGENTS</span><strong>{agentEvents.filter(item => item.current).length}<small> / 3</small></strong><p>with activity</p></div><div className="stat-card"><span>PASSED NODES</span><strong>{passed}</strong><p>completed successfully</p></div><div className="stat-card"><span>FAILED NODES</span><strong>{failed}</strong><p>trigger global stop</p></div><div className="stat-card"><span>CURRENT STAGE</span><strong className="stage-value">{run.stage}</strong><p>{run.status}</p></div></div>
          <div className="run-grid"><section className="panel agent-panel"><div className="section-header"><div><div className="eyebrow">PARALLEL BRANCHES</div><h2>Testing agents</h2></div><button className="text-button" onClick={() => setTab("flow")}>Open flow <ArrowRight size={15} /></button></div>{agentEvents.map(item => <div className="agent-row" key={item.key}><div className={["agent-symbol", item.key].join(" ")}>{item.key === "builtin" ? <GitBranch size={18} /> : item.key === "playwright" ? "P" : "V"}</div><div className="agent-copy"><strong>{agentNames[item.key]}</strong><small>{item.current ? (item.current.node?.replaceAll("_", " ") ?? "") + " · " + item.current.status : agentDescriptions[item.key]}</small></div><span className={["status-badge", item.current?.status ?? "not_started"].join(" ")}>{item.current?.status ?? "waiting"}</span></div>)}</section>
          <section className="panel activity-panel"><div className="section-header"><div><div className="eyebrow">RUN ACTIVITY</div><h2>Latest events</h2></div><Activity size={18} /></div><div className="activity-list">{events.slice(-8).reverse().map(event => <div className="activity-row" key={event.id}><span className={["activity-dot", event.status].join(" ")} /><div><strong>{event.node?.replaceAll("_", " ") ?? event.stage}</strong><small>{event.agent ?? "supervisor"} · {event.status}</small></div><time>{new Date(event.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time></div>)}{events.length === 0 && <div className="empty-state">Waiting for the first node event…</div>}</div></section></div>
          <section className="panel results-panel"><div className="section-header"><div><div className="eyebrow">EVIDENCE & REVIEW</div><h2>Agent report</h2></div><span className={["status-badge", run.report ? "passed" : "not_started"].join(" ")}>{run.report ? "ready" : "pending"}</span></div>{run.report ? <article className="report-markdown"><ReactMarkdown components={{ a: ({ href, children }) => { const url = reportHref(href, run.id); return url ? <a href={url} target="_blank" rel="noreferrer">{children}</a> : <span>{children}</span>; } }}>{run.report}</ReactMarkdown></article> : <div className="empty-state"><LoaderCircle size={21} className={run.status === "running" ? "spin" : ""} /> The report appears here when the evidence review finishes.</div>}{run.error && <div className="report-error">{run.error}</div>}{artifacts.length > 0 && <div className="artifacts" id="artifacts"><div className="eyebrow">RUN ARTIFACTS</div><div className="artifact-list">{artifacts.map(path => <a key={path} href={API_URL + "/api/runs/" + run.id + "/artifacts/" + path} target="_blank" rel="noreferrer"><span>{path}</span><ExternalLink size={13} /></a>)}</div></div>}</section>
        </>}
        <footer className="footer">Ardberg · Pinned-commit testing · Validated tests commit after passing suites</footer>
      </div>}
    </main>
  </div>;
}
