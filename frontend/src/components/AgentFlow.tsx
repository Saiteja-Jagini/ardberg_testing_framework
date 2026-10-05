"use client";

import { useMemo, useState } from "react";
import { ReactFlow, Background, Controls, Handle, MarkerType, MiniMap, Position, type NodeProps } from "@xyflow/react";
import { Activity, Check, Circle, LoaderCircle, X } from "lucide-react";
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
  return <div className={["diagram-node", detail.group, detail.status, selected ? "selected" : ""].join(" ")}>
    <Handle type="target" position={Position.Left} />
    <div className="diagram-node-top"><span className="diagram-node-group">{detail.group}</span><span className="diagram-node-state"><StatusIcon status={detail.status} /></span></div>
    <strong>{detail.label}</strong>
    <small>{detail.status.replaceAll("_", " ")}</small>
    <Handle type="source" position={Position.Right} />
  </div>;
}

const nodeTypes = { workflow: DiagramNode };

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
      markerEnd: { type: MarkerType.ArrowClosed, color: "#a4acc2" },
      style: { stroke: edge.label.includes("instruction") ? "#8a69e8" : "#a4acc2", strokeWidth: 1.6 },
      labelStyle: { fill: "#7c8499", fontSize: 10, fontWeight: 600 },
    })),
  }), [flow]);

  const analysis = flow.run?.context?.analysis as Record<string, unknown> | undefined;

  return <div className="flow-layout">
    <section className="flow-main">
      <div className="flow-heading">
        <div><div className="eyebrow"><Activity size={14} /> {flow.run ? "THIS RUN’S WORKFLOW" : "REFERENCE WORKFLOW"}</div><h2>Agent flow</h2><p>{flow.run
          ? `${flow.run.repository} PR #${flow.run.pr_number} · run ${flow.run.id.slice(0, 8)} · commit ${flow.run.head_sha.slice(0, 12)}`
          : "Follow the instruction from PR intake through each agent, execution, and reporting."}</p></div>
        <div className="flow-status"><span className={["dot", flow.run?.status ?? "idle"].join(" ")} />{flow.run ? flow.run.status + " · " + flow.run.stage : "Reference flow"}</div>
      </div>
      <div className="flow-legend"><span><i className="legend-swatch purple" /> Instruction & context</span><span><i className="legend-swatch blue" /> Agent work</span><span><i className="legend-swatch green" /> Passed</span><span><i className="legend-swatch red" /> Failed</span><span><i className="legend-swatch gray" /> Waiting</span></div>
      <div className="flow-canvas">
        <ReactFlow nodes={graph.nodes} edges={graph.edges} nodeTypes={nodeTypes}
          nodesDraggable={false} nodesConnectable={false} elementsSelectable
          onNodeClick={(_, node) => setSelected(node.id)} fitView fitViewOptions={{ padding: 0.12 }} minZoom={0.1} maxZoom={1.5}>
          <Background gap={20} size={1} color="#e3e7f0" />
          <Controls showInteractive={false} />
          <MiniMap pannable zoomable nodeStrokeWidth={2} maskColor="rgba(17,24,44,.07)" />
        </ReactFlow>
      </div>
      <div className="flow-note">Built-in, Playwright, and Vitest start in parallel, with ordered nodes inside each branch. A specialist without an executable target stops at applicability; other branches continue. Browser tests start the app only when the Playwright browser branch produces runnable tests. The preview is an optional human testing path.</div>
    </section>
    <aside className="flow-inspector">
      <div className="eyebrow">NODE INSPECTOR</div>
      <h3>{selectedNode?.label ?? "Select a node"}</h3>
      <p>{selectedNode?.description}</p>
      <div className="inspector-status"><span className={["status-badge", selectedNode?.status ?? "not_started"].join(" ")}>{selectedNode?.status.replaceAll("_", " ")}</span><span>{selectedNode?.group}</span></div>
      <div className="inspector-divider" />
      <div className="eyebrow">{flow.run?.context?.instruction_source === "inferred" ? "INFERRED PR REVIEW GOAL" : flow.run?.context?.instruction_source === "automatic_fallback" ? "AUTOMATIC REVIEW GOAL · FEATURE INTENT UNCLEAR" : "INSTRUCTION PASSED TO AGENTS"}</div>
      <div className="instruction-card">{flow.run?.instruction || instruction || "Leave the testing intent blank to derive a review goal from the PR, or enter a specific behavior and expected result."}</div>
      {selectedNode?.event && <><div className="inspector-divider" /><div className="eyebrow">LATEST NODE EVENT</div><div className="event-meta">Event #{selectedNode.event.id} · {new Date(selectedNode.event.created_at).toLocaleString()}</div><div className="event-meta">Boolean result: {selectedNode.event.success === null ? "pending" : String(selectedNode.event.success)}</div><pre className="event-json">{JSON.stringify(selectedNode.event.detail, null, 2)}</pre></>}
      {analysis && <><div className="inspector-divider" /><div className="eyebrow">SHARED CONTEXT</div><p className="context-summary">{String(analysis.application_framework ?? "Framework unknown")} · {String(analysis.native_test_framework ?? "Tests unknown")}</p><span className="muted-small">Pinned commit {String(flow.run?.context?.head_sha ?? "").slice(0, 12)}</span></>}
      {Boolean(flow.run?.context?.impact_map) && <><div className="inspector-divider" /><div className="eyebrow">IMPACT MAP FOR THIS RUN</div><p className="context-summary">{String((flow.run?.context.impact_map as { feature_summary?: string }).feature_summary ?? "Impact not established")}</p></>}
    </aside>
  </div>;
}
