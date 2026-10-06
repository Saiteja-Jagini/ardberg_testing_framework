"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import ReactMarkdown from "react-markdown";
import { Activity, ArrowRight, Bot, CircleCheck, CircleHelp, CircleX, ExternalLink, GitBranch, GitPullRequest, Github, LayoutDashboard, LoaderCircle, Play, Plus, RefreshCw, Workflow, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Separator } from "@/components/ui/separator";
import { Breadcrumb, BreadcrumbItem, BreadcrumbList, BreadcrumbPage, BreadcrumbSeparator } from "@/components/ui/breadcrumb";
import { Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarGroupLabel, SidebarHeader, SidebarInset, SidebarMenu, SidebarMenuButton, SidebarMenuItem, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import AgentFlow from "./AgentFlow";
import InteractivePreview from "./InteractivePreview";
import ReviewScope from "./ReviewScope";
import { API_URL, api, type Connections, type FlowDefinition, type PullRequest, type ResolvedRepo, type Run, type RunEvent, type RunSummary } from "@/lib/api";

const agentNames: Record<string, string> = { builtin: "Built-in Change", playwright: "Playwright", vitest: "Vitest" };
const agentDescriptions: Record<string, string> = { builtin: "Repository context and native tests", playwright: "Browser and API behavior", vitest: "Focused unit tests" };
const reviewAgentNames: Record<string, string> = { behavior: "Behavior", code_critic: "Code critic", runtime_planner: "Runtime planner", evidence_critic: "Evidence critic" };
const reviewAgentDescriptions: Record<string, string> = { behavior: "Expected behavior and ambiguity", code_critic: "Implementation gaps and impact", runtime_planner: "Focused runtime scenarios", evidence_critic: "Confirmed findings and uncertainty" };

function reportHref(href: string | undefined, runId: string): string | undefined {
  if (!href) return undefined;
  if (href.startsWith("#") || /^https?:\/\//.test(href)) return href;
  const segments = href.split("/");
  if (["context", "patches", "logs", "evidence", "manual", "review"].includes(segments[0])
      && segments.length > 1
      && segments.slice(1).every(segment => /^[A-Za-z0-9._-]+$/.test(segment) && segment !== "..")) {
    return API_URL + "/api/runs/" + runId + "/artifacts/" + segments.map(encodeURIComponent).join("/");
  }
  return undefined;
}

function latestFor(events: RunEvent[], agent: string) {
  return [...events].reverse().find(event => event.agent === agent && event.node && event.status !== "artifact");
}

function ConnectionBadge({ label, connected, checked, error, icon }: {
  label: string; connected: boolean; checked: boolean; error?: string; icon: React.ReactNode;
}) {
  const state = checked ? (connected ? "connected" : "disconnected") : "checking";
  return <Badge variant={!checked ? "outline" : connected ? "secondary" : "destructive"}
    className="connection-badge h-8 gap-2 px-2.5" title={error || `${label} ${state}`}
    aria-label={`${label} ${state}`}>
    {icon}<span className="connection-label">{label} {state}</span>
    {!checked ? <LoaderCircle className="spin" /> : connected ? <CircleCheck /> : <CircleX />}
  </Badge>;
}

export default function Dashboard({ initialRunId }: { initialRunId?: string }) {
  const router = useRouter();
  const [tab, setTab] = useState<"workspace" | "flow" | "interactive">("workspace");
  const [url, setUrl] = useState("");
  const [instruction, setInstruction] = useState("");
  const [testingMode, setTestingMode] = useState(false);
  const [resolved, setResolved] = useState<ResolvedRepo | null>(null);
  const [selectedPr, setSelectedPr] = useState<PullRequest | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [recentRuns, setRecentRuns] = useState<RunSummary[]>([]);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [flow, setFlow] = useState<FlowDefinition | null>(null);
  const [health, setHealth] = useState<{ github_configured: boolean; model_configured: boolean } | null>(null);
  const [connections, setConnections] = useState<Connections | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [installUrl, setInstallUrl] = useState("");

  const activeRunId = useRef(initialRunId);
  const refresh = useCallback(async () => {
    if (!initialRunId) return;
    const [nextRun, nextEvents, nextFlow] = await Promise.all([
      api.run(initialRunId), api.events(initialRunId), api.flow(initialRunId),
    ]);
    if (activeRunId.current !== initialRunId) return;
    setRun(nextRun); setEvents(nextEvents); setFlow(nextFlow);
  }, [initialRunId]);
  const eventCursor = useRef(0);

  useEffect(() => {
    let active = true;
    activeRunId.current = initialRunId;
    const reset = window.setTimeout(() => {
      if (!active) return;
      setRun(null); setEvents([]); setFlow(null); setTab("workspace");
    }, 0);
    eventCursor.current = 0;
    api.health().then(setHealth).catch(() => setHealth(null));
    api.connections().then(setConnections).catch(() => setConnections({
      github: { connected: false, error: "Cannot reach the backend connection check" },
      model: { connected: false, model: "", error: "Cannot reach the backend connection check" },
    }));
    if (initialRunId) Promise.all([api.run(initialRunId), api.events(initialRunId), api.flow(initialRunId)])
      .then(([nextRun, nextEvents, nextFlow]) => {
        if (!active) return;
        setRun(nextRun); setEvents(nextEvents); setFlow(nextFlow);
        eventCursor.current = Math.max(eventCursor.current, ...nextEvents.map(event => event.id), 0);
      }).catch(err => { if (active) setError(err.message); });
    else api.flow().then(value => { if (active) setFlow(value); }).catch(() => {});
    return () => { active = false; window.clearTimeout(reset); };
  }, [initialRunId]);

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
    if (!initialRunId || tab !== "interactive") return;
    const timer = setInterval(() => refresh().catch(() => {}), 5000);
    return () => clearInterval(timer);
  }, [initialRunId, refresh, tab]);

  async function resolveRepo(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError(""); setInstallUrl("");
    try {
      const data = await api.resolve(url);
      setResolved(data);
      setSelectedPr(data.pull_requests.length === 1 ? data.pull_requests[0] : null);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not load repository";
      setError(message);
      const match = message.match(/https:\/\/github\.com\/apps\/[^\s"}]+/);
      if (match) setInstallUrl(match[0]);
    } finally { setBusy(false); }
  }

  async function startRun() {
    if (!resolved || !selectedPr) {
      setError("Choose a pull request before starting a review.");
      return;
    }
    setBusy(true); setError("");
    try {
      const result = await api.createRun({ repository: resolved.repository, pr_number: selectedPr.number,
        instruction: instruction.trim(), selected_frameworks: [],
        mode: testingMode ? "testing" : "critique" });
      router.push("/runs/" + result.run_id);
    } catch (err) { setError(err instanceof Error ? err.message : "Could not start run"); }
    finally { setBusy(false); }
  }

  function newRun() {
    if (initialRunId) { router.push("/"); return; }
    setUrl(""); setInstruction(""); setTestingMode(false);
    setResolved(null); setSelectedPr(null);
    setError(""); setInstallUrl("");
  }

  const isCritique = !run || run.context?.mode === "critique";
  const names = isCritique ? reviewAgentNames : agentNames;
  const descriptions = isCritique ? reviewAgentDescriptions : agentDescriptions;
  const agentEvents = Object.keys(names).map(key => ({ key, current: latestFor(events, key) }));
  const passed = events.filter(event => event.status === "passed" && event.node).length;
  const failed = events.filter(event => event.status === "failed" && event.node).length;
  const artifacts = [...new Set(events.flatMap(event => {
    const detail = event.detail;
    return [detail.path, detail.log, detail.screenshot,
      ...(Array.isArray(detail.paths) ? detail.paths : [])]
      .filter((item): item is string => typeof item === "string");
  }))];

  return <SidebarProvider className="min-h-svh">
    <Sidebar collapsible="icon" className="border-r border-sidebar-border">
      <SidebarHeader className="p-3 group-data-[collapsible=icon]:p-2"><div className="flex items-center gap-3 rounded-lg px-2 py-2 group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:p-0"><div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground group-data-[collapsible=icon]:size-8"><Workflow size={18} /></div><div className="group-data-[collapsible=icon]:hidden"><strong className="block text-base leading-none">ardberg</strong><span className="text-[10px] tracking-[.18em] text-muted-foreground">PR REVIEW</span></div></div></SidebarHeader>
      <SidebarContent><SidebarGroup><SidebarGroupLabel>Workspace</SidebarGroupLabel><SidebarGroupContent><SidebarMenu>
        <SidebarMenuItem><SidebarMenuButton isActive={tab === "workspace"} onClick={() => setTab("workspace")} tooltip="Overview"><LayoutDashboard /> Overview</SidebarMenuButton></SidebarMenuItem>
        <SidebarMenuItem><SidebarMenuButton isActive={tab === "flow"} onClick={() => setTab("flow")} tooltip="Agent flow"><Workflow /> Agent flow</SidebarMenuButton></SidebarMenuItem>
        {run && <SidebarMenuItem><SidebarMenuButton isActive={tab === "interactive"} onClick={() => setTab("interactive")} tooltip="Interactive preview"><Play /> Interactive preview</SidebarMenuButton></SidebarMenuItem>}
      </SidebarMenu></SidebarGroupContent></SidebarGroup></SidebarContent>
      <SidebarFooter className="p-3"><div className="rounded-lg border border-sidebar-border bg-sidebar-accent/50 p-3 text-xs group-data-[collapsible=icon]:hidden"><div className="mb-1 flex items-center gap-2 font-semibold"><CircleHelp size={15} /> Agent flow</div><p className="m-0 text-muted-foreground">Inspect each node and how your instruction travels.</p></div><div className="flex items-center gap-2 px-2 text-[10px] font-medium text-muted-foreground group-data-[collapsible=icon]:hidden"><span className="size-2 rounded-full bg-emerald-500" /> LOCAL WORKSPACE</div></SidebarFooter>
    </Sidebar>
    <SidebarInset className="min-w-0">
      <header className="topbar"><div className="flex items-center gap-3"><SidebarTrigger /><Separator orientation="vertical" className="h-5" /><Breadcrumb><BreadcrumbList><BreadcrumbItem>Workspace</BreadcrumbItem><BreadcrumbSeparator /><BreadcrumbItem><BreadcrumbPage>{tab === "flow" ? "Agent flow" : tab === "interactive" ? "Interactive preview" : "Overview"}</BreadcrumbPage></BreadcrumbItem></BreadcrumbList></Breadcrumb></div><div className="topbar-right"><ConnectionBadge label="GitHub" icon={<Github />} connected={Boolean(connections?.github.connected)} checked={Boolean(connections)} error={connections?.github.error} /><ConnectionBadge label="Model" icon={<Bot />} connected={Boolean(connections?.model.connected)} checked={Boolean(connections)} error={connections?.model.error} /><Avatar size="sm"><AvatarFallback>A</AvatarFallback></Avatar></div></header>
      {tab === "flow" ? (flow && (!initialRunId || flow.run?.id === initialRunId) ? <AgentFlow key={flow.run?.id ?? "reference"} flow={flow} instruction={instruction} /> : <div className="loading-page"><LoaderCircle className="spin" /> Loading this run’s agent flow...</div>) : tab === "interactive" && run ? <InteractivePreview run={run} /> :
      <div className="workspace-content">
        <div className="page-intro"><div><div className="eyebrow"><span className="eyebrow-line" /> AUTOMATED PR REVIEW</div><h1>{run ? "Run overview" : "Review a pull request"}</h1><p>{run ? "Track the code critique, runtime observations, and evidence." : "Give the agents a PR and describe the expected behavior. Follow every step as it runs."}</p></div><Button className="ghost-button" onClick={newRun}><Plus size={16} /> New run</Button></div>
        {error && <Alert variant="destructive" className="mb-4 flex items-center gap-2"><X size={17} /><AlertDescription>{error}</AlertDescription>{installUrl && <a className="ml-auto inline-flex items-center gap-1" href={installUrl} target="_blank" rel="noreferrer">Install GitHub App <ExternalLink size={14} /></a>}</Alert>}
        {health && (!health.github_configured || !health.model_configured) && <Alert className="mb-4 flex items-center gap-2"><CircleHelp size={18} /><AlertDescription>Setup needed: {[
          !health.github_configured ? "GitHub App credentials" : "",
          !health.model_configured ? "OpenAI API key" : "",
        ].filter(Boolean).join(" and ")} are missing from the backend environment.</AlertDescription></Alert>}
        {connections && ((health?.github_configured && !connections.github.connected) || (health?.model_configured && !connections.model.connected)) && <Alert className="mb-4 flex items-center gap-2"><CircleHelp size={18} /><AlertDescription>{[
          health?.github_configured && !connections.github.connected ? connections.github.error : "",
          health?.model_configured && !connections.model.connected ? connections.model.error : "",
        ].filter(Boolean).join(" · ")}</AlertDescription></Alert>}
        {!run ? <><div className="intake-grid">
          <Card className="panel intake-panel"><CardHeader className="panel-title p-0"><span className="step-number">01</span><div><CardTitle>Choose a pull request</CardTitle><CardDescription>Connect a repository with the GitHub App.</CardDescription></div></CardHeader><p className="helper-row">Analyzing a selected PR sends repository source snippets and diff context to the configured OpenAI model.</p><form onSubmit={resolveRepo}><Label htmlFor="repo-url">GitHub repository or PR link</Label><div className="url-row"><GitBranch size={18} /><Input id="repo-url" value={url} onChange={event => setUrl(event.target.value)} placeholder="https://github.com/owner/repository/pull/42" required /><Button type="submit" size="icon" aria-label="Find pull requests" disabled={busy}>{busy ? <LoaderCircle className="spin" size={16} /> : <ArrowRight size={17} />}</Button></div></form>
            {resolved && <div className="pr-list"><div className="field-heading">{resolved.repository} <span>{resolved.pull_requests.length} open PRs</span></div>{resolved.pull_requests.length ? resolved.pull_requests.map(pr => <Button key={pr.number} className={["pr-option", selectedPr?.number === pr.number ? "selected" : ""].join(" ")} onClick={() => setSelectedPr(pr)}><div className="pr-icon"><GitPullRequest size={17} /></div><div><strong>#{pr.number} {pr.title}</strong><small>{pr.head_sha.slice(0, 10)}</small></div><span className="radio-circle" /></Button>) : <p className="empty-text">No open pull requests found.</p>}</div>}
          </Card>
          <Card className="panel instruction-panel">
            <CardHeader className="panel-title p-0"><span className="step-number">02</span><div><CardTitle>Set the review intent</CardTitle><CardDescription>Optional. Describe expected behavior or leave it blank to infer it from the PR.</CardDescription></div></CardHeader>
            <Label htmlFor="instruction">What should the change do? (optional)</Label>
            <Textarea id="instruction" value={instruction} onChange={event => setInstruction(event.target.value)} placeholder="For the login form, wrong passwords must show an error without creating a session. Valid credentials must open the dashboard." rows={7} />
            <div className="helper-row"><CircleHelp size={14} /> Ardberg compares the diff with expected behavior and exercises focused scenarios in a disposable container. Unclear requirements remain explicitly unverified.</div>
            <label className="helper-row"><input type="checkbox" checked={testingMode} onChange={event => setTestingMode(event.target.checked)} /> Run the legacy generated test and commit workflow instead (optional)</label>
            <Button className="primary-button" disabled={busy || !selectedPr} onClick={startRun}><Play size={16} fill="currentColor" /> Start PR review <ArrowRight size={17} /></Button>
          </Card>
        </div>
        {recentRuns.length > 0 && <Card className="panel recent-runs">
          <CardHeader className="p-0"><div className="section-header"><div><div className="eyebrow">GITHUB APP + MANUAL RUNS</div><CardTitle>Recent runs</CardTitle></div><RefreshCw size={16} /></div></CardHeader>
          <CardContent className="p-0"><ScrollArea className="max-h-[420px] w-full"><Table><TableHeader><TableRow><TableHead>Pull request</TableHead><TableHead>Title</TableHead><TableHead>Commit</TableHead><TableHead>Status</TableHead></TableRow></TableHeader><TableBody>{recentRuns.map(item => <TableRow key={item.id}><TableCell><Button variant="link" className="h-auto p-0 text-left" onClick={() => router.push("/runs/" + item.id)}>{item.repository} / #{item.pr_number}</Button></TableCell><TableCell className="max-w-[350px] truncate font-medium">{item.title || "PR testing run"}</TableCell><TableCell className="font-mono text-xs text-muted-foreground">{item.head_sha.slice(0, 10)}</TableCell><TableCell><Badge variant="outline" className={["status-badge", item.status].join(" ")}>{item.status}</Badge></TableCell></TableRow>)}</TableBody></Table></ScrollArea></CardContent>
        </Card>}</> :
        <><Card className="run-hero panel"><div className="run-hero-main"><span className="run-label"><GitPullRequest size={15} /> {run.repository} · PR #{run.pr_number}</span><h2>{run.title}</h2><div className="run-meta"><span><GitBranch size={14} /> {run.head_sha.slice(0, 12)}</span><Badge variant="outline" className={["status-badge", run.status].join(" ")}>{run.status}</Badge><span>Stage: {run.stage}</span></div></div><Button variant="outline" className="outline-button" onClick={() => refresh().catch(err => setError(err.message))}><RefreshCw size={15} /> Refresh</Button></Card>
          <ReviewScope run={run} />
          <div className="stats-row"><Card className="stat-card"><span>AGENTS</span><strong>{agentEvents.filter(item => item.current).length}<small> / {agentEvents.length}</small></strong><p>with activity</p></Card><Card className="stat-card"><span>PASSED NODES</span><strong>{passed}</strong><p>completed successfully</p></Card><Card className="stat-card"><span>FAILED NODES</span><strong>{failed}</strong><p>inspect evidence</p></Card><Card className="stat-card"><span>CURRENT STAGE</span><strong className="stage-value">{run.stage}</strong><p>{run.status}</p></Card></div>
          <div className="run-grid"><Card className="panel agent-panel"><div className="section-header"><div><div className="eyebrow">REVIEW STAGES</div><h2>{isCritique ? "Review agents" : "Testing agents"}</h2></div><Button variant="ghost" className="text-button" onClick={() => setTab("flow")}>Open flow <ArrowRight size={15} /></Button></div>{agentEvents.map(item => <div className="agent-row" key={item.key}><div className={["agent-symbol", item.key].join(" ")}>{item.key === "builtin" ? <GitBranch size={18} /> : names[item.key].slice(0, 1)}</div><div className="agent-copy"><strong>{names[item.key]}</strong><small>{item.current ? (item.current.node?.replaceAll("_", " ") ?? "") + " · " + item.current.status : descriptions[item.key]}</small></div><Badge variant="outline" className={["status-badge", item.current?.status ?? "not_started"].join(" ")}>{item.current?.status ?? "waiting"}</Badge></div>)}</Card>
          <Card className="panel activity-panel"><div className="section-header"><div><div className="eyebrow">RUN ACTIVITY</div><h2>Latest events</h2></div><Activity size={18} /></div><ScrollArea className="activity-list max-h-[340px]">{events.slice(-8).reverse().map(event => <div className="activity-row" key={event.id}><span className={["activity-dot", event.status].join(" ")} /><div><strong>{event.node?.replaceAll("_", " ") ?? event.stage}</strong><small>{event.agent ?? "supervisor"} · {event.status}</small></div><time>{new Date(event.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time></div>)}{events.length === 0 && <div className="empty-state">Waiting for the first node event…</div>}</ScrollArea></Card></div>
          <Card className="panel results-panel"><div className="section-header"><div><div className="eyebrow">EVIDENCE & REVIEW</div><h2>Agent report</h2></div><Badge variant="outline" className={["status-badge", run.report ? "passed" : "not_started"].join(" ")}>{run.report ? "ready" : "pending"}</Badge></div>{run.report ? <ScrollArea className="report-scroll h-[min(680px,70vh)] w-full min-w-0 max-w-full"><article className="report-markdown"><ReactMarkdown components={{ a: ({ href, children }) => { const url = reportHref(href, run.id); return url ? <a href={url} target="_blank" rel="noreferrer">{children}</a> : <span>{children}</span>; } }}>{run.report}</ReactMarkdown></article></ScrollArea> : <div className="empty-state"><LoaderCircle size={21} className={run.status === "running" ? "spin" : ""} /> The report appears here when the evidence review finishes.</div>}{run.error && <Alert variant="destructive"><AlertDescription>{run.error}</AlertDescription></Alert>}{artifacts.length > 0 && <div className="artifacts" id="artifacts"><div className="eyebrow">RUN ARTIFACTS</div><ScrollArea className="max-h-52"><div className="artifact-list">{artifacts.map(path => <a key={path} href={API_URL + "/api/runs/" + run.id + "/artifacts/" + path} target="_blank" rel="noreferrer"><span>{path}</span><ExternalLink size={13} /></a>)}</div></ScrollArea></div>}</Card>
        </>}
      </div>}
      <footer className="footer mx-auto w-full max-w-[1330px] px-6 pb-5"><Separator className="mb-4" /><span>Ardberg · Pinned diff review · Disposable runtime evidence</span></footer>
    </SidebarInset>
  </SidebarProvider>;
}
