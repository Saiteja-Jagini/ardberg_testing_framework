"use client";

import { useMemo, useState } from "react";
import { ReactFlow, Background, Handle, MarkerType, MiniMap, Panel, Position, useReactFlow, type NodeProps } from "@xyflow/react";
import { Activity, Check, Circle, LoaderCircle, Maximize, Minus, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import type { FlowDefinition, FlowNode } from "@/lib/api";

type DiagramData = { label: string; group: string; status: string; description: string };

function StatusIcon({ status }: { status: string }) {
  if (status === "passed") return <Check size={15} />;
  if (status === "failed") return <X size={15} />;
  if (status === "running") return <LoaderCircle size={15} className="spin" />;
  return <Circle size={11} />;
}

function DiagramNode({ data, selected }: NodeProps) {
  const detail = data as DiagramData;
  return <Card className={["diagram-node", detail.group, detail.status, selected ? "selected" : ""].join(" ")}>
    <Handle type="target" position={Position.Left} />
    <div className="diagram-node-top"><Badge variant="secondary" className="diagram-node-group">{detail.group}</Badge><span className="diagram-node-state"><StatusIcon status={detail.status} /></span></div>
    <strong>{detail.label}</strong>
    <small>{detail.status.replaceAll("_", " ")}</small>
    <Handle type="source" position={Position.Right} />
  </Card>;
}

const nodeTypes = { workflow: DiagramNode };

function DiagramControls() {
  const flow = useReactFlow();
  return <Panel position="bottom-left" className="flex gap-1 rounded-lg border bg-card p-1 shadow-sm">
    <Button variant="ghost" size="icon-sm" aria-label="Zoom in" onClick={() => flow.zoomIn()}><Plus /></Button>
    <Button variant="ghost" size="icon-sm" aria-label="Zoom out" onClick={() => flow.zoomOut()}><Minus /></Button>
    <Button variant="ghost" size="icon-sm" aria-label="Fit diagram" onClick={() => flow.fitView({ padding: 0.12 })}><Maximize /></Button>
  </Panel>;
}

export default function AgentFlow({ flow, instruction }: { flow: FlowDefinition; instruction: string }) {
  const [selected, setSelected] = useState<string>("input");
  const selectedNode: FlowNode | undefined = flow.nodes.find(node => node.id === selected) ?? flow.nodes[0];
  const graph = useMemo(() => ({
    nodes: flow.nodes.map(node => ({
      id: node.id, type: "workflow", position: { x: node.x, y: node.y },
      data: { label: node.label, group: node.group, status: node.status, description: node.description },
      sourcePosition: Position.Right, targetPosition: Position.Left,
    })),
    edges: flow.edges.map(edge => ({
      id: edge.id, source: edge.source, target: edge.target,
      label: edge.label, type: "smoothstep", animated: edge.label.includes("instruction"),
      markerEnd: { type: MarkerType.ArrowClosed, color: "var(--muted-foreground)" },
      style: { stroke: edge.label.includes("instruction") ? "var(--primary)" : "var(--muted-foreground)", strokeWidth: 1.6 },
      labelStyle: { fill: "var(--muted-foreground)", fontSize: 10, fontWeight: 600 },
    })),
  }), [flow]);

  const analysis = flow.run?.context?.analysis as Record<string, unknown> | undefined;

  return <div className="flow-layout">
    <Card className="flow-main">
      <div className="flow-heading">
        <div><div className="eyebrow"><Activity size={14} /> {flow.run ? "THIS RUN’S WORKFLOW" : "REFERENCE WORKFLOW"}</div><h2>Agent flow</h2><p>{flow.run
          ? `${flow.run.repository} PR #${flow.run.pr_number} · run ${flow.run.id.slice(0, 8)} · commit ${flow.run.head_sha.slice(0, 12)}`
          : "Follow the instruction from PR intake through each agent, execution, and reporting."}</p></div>
        <Badge variant="outline" className="flow-status"><span className={["dot", flow.run?.status ?? "idle"].join(" ")} />{flow.run ? flow.run.status + " · " + flow.run.stage : "Reference flow"}</Badge>
      </div>
      <div className="flow-legend"><span><i className="legend-swatch primary" /> Instruction & context</span><span><i className="legend-swatch work" /> Agent work</span><span><i className="legend-swatch green" /> Passed</span><span><i className="legend-swatch red" /> Failed</span><span><i className="legend-swatch gray" /> Waiting</span></div>
      <div className="flow-canvas">
        <ReactFlow nodes={graph.nodes} edges={graph.edges} nodeTypes={nodeTypes}
          nodesDraggable={false} nodesConnectable={false} elementsSelectable
          onNodeClick={(_, node) => setSelected(node.id)} fitView fitViewOptions={{ padding: 0.12 }} minZoom={0.1} maxZoom={1.5}>
          <Background gap={20} size={1} color="var(--border)" />
          <DiagramControls />
          <MiniMap pannable zoomable nodeStrokeWidth={2} maskColor="color-mix(in oklch, var(--foreground) 7%, transparent)" />
        </ReactFlow>
      </div>
      <div className="flow-note">{!flow.run || flow.run.context?.mode === "critique"
        ? "The behavior agent extracts expectations, the code critic traces possible gaps, and the runtime planner selects focused scenarios. The runner observes pinned PR behavior in a disposable container and compares the base when useful. The evidence critic classifies findings before the report is published."
        : "Built-in, Playwright, and Vitest start in parallel, with ordered nodes inside each branch. A specialist without an executable target stops at applicability; other branches continue. Browser tests start the app only when the Playwright browser branch produces runnable tests. The preview is an optional human testing path."}</div>
    </Card>
    <aside className="flow-inspector"><ScrollArea className="h-full pr-3">
      <div className="eyebrow">NODE INSPECTOR</div>
      <h3>{selectedNode?.label ?? "Select a node"}</h3>
      <p>{selectedNode?.description}</p>
      <div className="inspector-status"><Badge variant="outline" className={["status-badge", selectedNode?.status ?? "not_started"].join(" ")}>{selectedNode?.status.replaceAll("_", " ")}</Badge><Badge variant="secondary">{selectedNode?.group}</Badge></div>
      <Separator className="my-5" />
      <div className="eyebrow">{flow.run?.context?.instruction_source === "inferred" ? "INFERRED PR REVIEW GOAL" : flow.run?.context?.instruction_source === "automatic_fallback" ? "AUTOMATIC REVIEW GOAL · FEATURE INTENT UNCLEAR" : "INSTRUCTION PASSED TO AGENTS"}</div>
      <div className="instruction-card">{flow.run?.instruction || instruction || "Leave review intent blank to infer expected behavior from the PR, or describe what the change should do."}</div>
      {selectedNode?.event && <><Separator className="my-5" /><div className="eyebrow">LATEST NODE EVENT</div><div className="event-meta">Event #{selectedNode.event.id} · {new Date(selectedNode.event.created_at).toLocaleString()}</div><div className="event-meta">Boolean result: {selectedNode.event.success === null ? "pending" : String(selectedNode.event.success)}</div><ScrollArea className="max-h-72 rounded-md border"><pre className="event-json">{JSON.stringify(selectedNode.event.detail, null, 2)}</pre></ScrollArea></>}
      {analysis && <><Separator className="my-5" /><div className="eyebrow">SHARED CONTEXT</div><p className="context-summary">{String(analysis.application_framework ?? "Framework unknown")} · {String(analysis.native_test_framework ?? "Tests unknown")}</p><span className="muted-small">Pinned commit {String(flow.run?.context?.head_sha ?? "").slice(0, 12)}</span></>}
      {Boolean(flow.run?.context?.impact_map) && <><Separator className="my-5" /><div className="eyebrow">IMPACT MAP FOR THIS RUN</div><p className="context-summary">{String((flow.run?.context.impact_map as { feature_summary?: string }).feature_summary ?? "Impact not established")}</p></>}
    </ScrollArea></aside>
  </div>;
}
