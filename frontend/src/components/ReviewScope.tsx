import { ExternalLink } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { API_URL, type Run } from "@/lib/api";

type ImpactArea = { surface: string; summary: string; confidence: string;
  changed_files: string[]; related_files: string[]; reason: string };
type ImpactMap = { feature_summary: string; areas: ImpactArea[];
  browser_targets: { path: string }[]; review_gaps: string[];
  checks: { surface: string; behavior: string; expected: string; method: string;
    prerequisites: string[] }[] };
type ContractChange = { route: string; kind: string; changed_fields: string[] };

export default function ReviewScope({ run }: { run: Run }) {
  const impact = run.context?.impact_map as ImpactMap | undefined;
  const contract = run.context?.api_contract as { status: string; changes: ContractChange[] } | undefined;
  if (!impact) return <Card className="panel review-scope"><div className="eyebrow">PR REVIEW SCOPE</div><p>Impact mapping appears after shared preflight.</p></Card>;
  const areas = Array.isArray(impact.areas) ? impact.areas : [];
  const gaps = Array.isArray(impact.review_gaps) ? impact.review_gaps : [];
  const routes = Array.isArray(impact.browser_targets) ? impact.browser_targets : [];
  const checks = Array.isArray(impact.checks) ? impact.checks : [];
  const changes = Array.isArray(contract?.changes) ? contract.changes : [];
  return <Card className="panel review-scope">
    <div className="section-header"><div><div className="eyebrow">PINNED PR REVIEW SCOPE</div><h2>What this run will inspect</h2></div>
      <a href={`${API_URL}/api/runs/${run.id}/artifacts/context/impact-map.json`} target="_blank" rel="noreferrer">Impact evidence <ExternalLink size={13} /></a></div>
    {Boolean(run.context?.instruction_source && run.context.instruction_source !== "user") && <p className="review-meta"><strong>Review intent:</strong> {run.context?.instruction_source === "inferred" ? "Inferred from this PR's evidence" : "Automatic review; expected feature behavior could not be established"}</p>}
    {typeof run.context?.review_verdict === "string" && <p className="review-meta"><strong>Critique verdict:</strong> {run.context.review_verdict.replaceAll("_", " ")}</p>}
    <p className="review-summary">{impact.feature_summary || "No feature summary was established."}</p>
    <div className="review-area-list">{areas.map((area, index) => <Card size="sm" className="review-area" key={`${area.surface}-${index}`}>
      <div><Badge variant="secondary" className="review-surface">{area.surface.replaceAll("_", " ")}</Badge><Badge variant="outline" className="review-confidence">{area.confidence}</Badge></div>
      <strong>{area.summary}</strong><p>{area.reason}</p>
      <small>{[...area.changed_files, ...area.related_files].join(" · ")}</small>
    </Card>)}</div>
    {areas.length === 0 && <p className="review-gap">No impact areas were established from the available source.</p>}
    {checks.length > 0 && <div className="review-checks"><strong>Proposed behavior checks</strong><ul>{checks.map((check, index) =>
      <li key={index}><span>{check.method.replaceAll("_", " ")} · {check.surface}</span><b>{check.behavior}</b><small>Expected: {check.expected}</small>{check.prerequisites?.length > 0 && <small>Needs: {check.prerequisites.join(", ")}</small>}</li>
    )}</ul></div>}
    {routes.length > 0 && <p className="review-meta"><strong>Browser routes:</strong> {routes.map(item => item.path).join(", ")}</p>}
    {contract && <p className="review-meta"><strong>Checked-in API contract:</strong> {contract.status === "compared"
      ? `${changes.length} changed operations${changes.length ? ` (${changes.slice(0, 5).map(item => `${item.kind} ${item.route}`).join(", ")})` : ""}`
      : contract.status.replaceAll("_", " ")}</p>}
    {gaps.length > 0 && <div className="review-gaps"><Separator className="mb-3" /><strong>Needs separate verification</strong><ul>{gaps.map((gap, index) => <li key={index}>{gap}</li>)}</ul></div>}
  </Card>;
}
