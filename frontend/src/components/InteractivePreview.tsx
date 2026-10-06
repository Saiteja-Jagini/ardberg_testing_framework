"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Check, ExternalLink, LoaderCircle, Play, RefreshCw, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
import { ScrollArea } from "@/components/ui/scroll-area";
import { API_URL, api, type InteractivePreviewData, type Run } from "@/lib/api";

export default function InteractivePreview({ run }: { run: Run }) {
  const [data, setData] = useState<InteractivePreviewData | null>(null);
  const [command, setCommand] = useState("");
  const [port, setPort] = useState(3000);
  const [readyPath, setReadyPath] = useState("/");
  const [setupCommands, setSetupCommands] = useState("");
  const [environmentText, setEnvironmentText] = useState("");
  const [steps, setSteps] = useState("");
  const [expected, setExpected] = useState("");
  const [actual, setActual] = useState("");
  const [verdict, setVerdict] = useState<"passed" | "failed" | "blocked">("passed");
  const [logs, setLogs] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const edited = useRef({ command: false, port: false, ready_path: false, setup: false });

  const load = useCallback(async () => {
    const result = await api.interactivePreview(run.id);
    if (!edited.current.command) setCommand(result.defaults.command);
    if (!edited.current.port) setPort(result.defaults.port);
    if (!edited.current.ready_path) setReadyPath(result.defaults.ready_path);
    if (!edited.current.setup) setSetupCommands(result.defaults.setup_commands.join("\n"));
    setData(result);
  }, [run.id]);

  useEffect(() => {
    const initial = setTimeout(() => load().catch(err => setError(err.message)), 0);
    const timer = setInterval(() => load().catch(() => {}), 4000);
    return () => { clearTimeout(initial); clearInterval(timer); };
  }, [load]);

  async function act(action: () => Promise<unknown>, success: string) {
    setBusy(true); setError(""); setMessage("");
    try {
      const result = await action();
      await load();
      const response = result as { queued?: boolean; reason?: string;
        report?: { queued: boolean; reason?: string } } | undefined;
      const report = response?.report ?? response;
      setMessage(report && !report.queued ? report.reason ?? "Observation saved; update the report later." : success);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally { setBusy(false); }
  }

  const session = data?.session;
  const active = session && ["queued", "starting", "ready", "stopping"].includes(session.status);
  const canStart = Boolean(run.context?.source_path && run.context?.analysis);
  const pinnedSha = typeof run.context?.head_sha === "string" ? run.context.head_sha : run.head_sha;
  const checkout = `git fetch https://github.com/${run.repository}.git ${pinnedSha} && git switch --detach ${pinnedSha}`;

  function environmentValues(): Record<string, string> {
    const values: Record<string, string> = {};
    for (const line of environmentText.split("\n").map(item => item.trim()).filter(Boolean)) {
      const separator = line.indexOf("=");
      if (separator < 1) throw new Error(`Use KEY=value for preview variable: ${line}`);
      values[line.slice(0, separator).trim()] = line.slice(separator + 1).trim();
    }
    return values;
  }

  return <div className="workspace-content interactive-page">
    <div className="page-intro"><div><div className="eyebrow"><span className="eyebrow-line" /> HANDS-ON PR TESTING</div>
      <h1>Interactive preview</h1><p>Run the pinned PR application locally, try the feature, and record what you observed.</p></div>
      <Badge variant="outline" className="font-mono">{pinnedSha.slice(0, 12)}</Badge></div>
    {error && <Alert variant="destructive" className="mb-4"><AlertDescription>{error}</AlertDescription></Alert>}
    {message && <Alert className="mb-4"><AlertDescription className="flex items-center gap-2"><Check size={15} /> {message}</AlertDescription></Alert>}
    <Card className="panel interactive-card"><CardHeader className="section-header p-0"><div><div className="eyebrow">LOCAL CONTAINER</div><CardTitle>Start the PR application</CardTitle></div>
      <Badge variant="outline" className={["status-badge", session?.status === "ready" ? "passed" : session?.status ?? "not_started"].join(" ")}>{session?.status ?? "not started"}</Badge></CardHeader>
      <p>The preview uses the same pinned PR snapshot as the automated review. It is available only on this computer and stops after two hours.</p>
      {!canStart && <Alert className="mb-4"><AlertDescription>The shared preflight has not prepared a source snapshot yet. Refresh this tab after it finishes.</AlertDescription></Alert>}
      <div className="interactive-fields"><div className="wide-field"><Label htmlFor="preview-command">Application start command</Label>
        <Input id="preview-command" value={command} onChange={event => { edited.current.command = true; setCommand(event.target.value); }} placeholder="npm run dev -- --host 0.0.0.0" disabled={Boolean(active)} /></div>
        <div><Label htmlFor="preview-port">Container port</Label><Input id="preview-port" type="number" min={1} max={65535} value={port} onChange={event => { edited.current.port = true; setPort(Number(event.target.value)); }} disabled={Boolean(active)} /></div>
        <div><Label htmlFor="preview-ready">Ready path</Label><Input id="preview-ready" value={readyPath} onChange={event => { edited.current.ready_path = true; setReadyPath(event.target.value); }} placeholder="/" disabled={Boolean(active)} /></div></div>
      <Accordion type="single" collapsible className="interactive-advanced"><AccordionItem value="environment"><AccordionTrigger>Environment and database setup</AccordionTrigger><AccordionContent>
        <p>Use repository setup commands for a fresh disposable database and any app-specific preview variables. These values are saved locally; the database connection is managed by Ardberg.</p>
        <div className="manual-grid"><div><Label htmlFor="preview-setup">Setup commands, one per line</Label>
          <Textarea id="preview-setup" rows={3} value={setupCommands} onChange={event => { edited.current.setup = true; setSetupCommands(event.target.value); }} disabled={Boolean(active)} /></div>
          <div><Label htmlFor="preview-env">Additional variables, KEY=value per line</Label>
          <Textarea id="preview-env" rows={3} value={environmentText} onChange={event => setEnvironmentText(event.target.value)} disabled={Boolean(active)} placeholder="APPLICATION_MODE=local" /></div></div>
      </AccordionContent></AccordionItem></Accordion>
      <div className="interactive-actions">
        <Button className="outline-button" disabled={busy || !canStart || Boolean(active) || !command.trim()} onClick={() => act(() => api.startInteractivePreview(run.id, { command, port, ready_path: readyPath,
          setup_commands: setupCommands.split("\n").map(item => item.trim()).filter(Boolean),
          environment: environmentValues() }), "Preview queued. It will open when dependencies and the application are ready.")}><Play size={14} /> Start preview</Button>
        {session?.url && <a className="outline-button" href={session.url} target="_blank" rel="noreferrer">Open application <ExternalLink size={14} /></a>}
        <Button className="outline-button" disabled={busy || !active || session?.status === "stopping"} onClick={() => act(() => api.stopInteractivePreview(run.id), "Preview is stopping.")}><Square size={13} /> Stop</Button>
        <Button className="outline-button" disabled={!session} onClick={() => api.interactivePreviewLogs(run.id).then(result => setLogs(result.text)).catch(err => setError(err.message))}><RefreshCw size={14} /> View logs</Button>
      </div>
      {session?.error && <Alert variant="destructive"><AlertDescription>{session.error}</AlertDescription></Alert>}
      {logs && <ScrollArea className="max-h-80 rounded-lg border bg-muted/40"><pre className="interactive-log">{logs}</pre></ScrollArea>}
      {session && <p className="interactive-hint">Preview ID: {session.id}{session.status === "stopped" && <> · <a href={`${API_URL}/api/runs/${run.id}/artifacts/manual/${session.id}/application.log`} target="_blank" rel="noreferrer">Saved application log</a></>}</p>}
    </Card>
    <Card className="panel interactive-card"><CardHeader className="section-header p-0"><div><div className="eyebrow">YOUR IDE</div><CardTitle>Inspect the pinned commit</CardTitle></div></CardHeader>
      <p>In your local repository checkout, fetch the tested commit and switch to it:</p>
      <div className="checkout-row"><code>{checkout}</code><Button className="outline-button" onClick={() => navigator.clipboard.writeText(checkout).then(() => setMessage("Checkout command copied.")).catch(err => setError(err.message))}>Copy</Button></div>
      <p className="interactive-hint">This checks out the exact commit used for this run. Use a separate worktree if your current checkout has changes.</p>
    </Card>
    <Card className="panel interactive-card"><CardHeader className="section-header p-0"><div><div className="eyebrow">HUMAN EVIDENCE</div><CardTitle>Record a feature check</CardTitle></div></CardHeader>
      <p>Describe the action you tried and the result you expected. Your observation is labeled as a human finding in the refreshed agent report.</p>
      <Label htmlFor="manual-steps">Steps you took</Label><Textarea id="manual-steps" rows={3} value={steps} onChange={event => setSteps(event.target.value)} placeholder="Open the page, submit the form with ..." />
      <div className="manual-grid"><div><Label htmlFor="manual-expected">Expected result</Label><Textarea id="manual-expected" rows={3} value={expected} onChange={event => setExpected(event.target.value)} /></div>
      <div><Label htmlFor="manual-actual">Actual result</Label><Textarea id="manual-actual" rows={3} value={actual} onChange={event => setActual(event.target.value)} /></div></div>
      <div className="interactive-actions"><Label htmlFor="manual-verdict">Verdict</Label><NativeSelect id="manual-verdict" value={verdict} onChange={event => setVerdict(event.target.value as typeof verdict)}><NativeSelectOption value="passed">Passed</NativeSelectOption><NativeSelectOption value="failed">Failed</NativeSelectOption><NativeSelectOption value="blocked">Blocked</NativeSelectOption></NativeSelect>
        <Button className="outline-button" disabled={busy || !session || steps.trim().length < 5 || expected.trim().length < 3 || actual.trim().length < 3} onClick={() => act(async () => { const result = await api.saveManualObservation(run.id, { verdict, steps, expected, actual }); setSteps(""); setExpected(""); setActual(""); return result; }, "Observation saved. The report update is queued.")}>{busy ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />} Save observation</Button>
        <Button className="outline-button" disabled={busy || !session} onClick={() => act(() => api.refreshManualReport(run.id), "Report update queued.")}><RefreshCw size={14} /> Update report</Button></div>
      {data?.observations.map(item => <div className="observation" key={item.id}><div><Badge variant="outline" className={["status-badge", item.verdict === "passed" ? "passed" : "failed"].join(" ")}>{item.verdict}</Badge><time>{new Date(item.created_at).toLocaleString()}</time></div><strong>{item.steps}</strong><p>Expected: {item.expected}</p><p>Actual: {item.actual}</p></div>)}
    </Card>
  </div>;
}
