import type { LibraryCitation } from "./AssistantPanel";
export type GraphNode = { id: string; kind: "document" | "topic" | "folder" | "question" | "group"; label: string; document_id?: string; available?: boolean; entry_id?: string; group_id?: string; question_text?: string };
export type GraphEdge = { id: string; source: string; target: string; kind: "tag" | "folder" | "citation" | "manual" | "group" | "question_link"; label: string; source_changed?: boolean; relation_id?: string; origin?: "manual" | "ai_confirmed"; evidence?: { source?: LibraryCitation; target?: LibraryCitation; provider?: string; model?: string; confirmed_at?: string; ai_proposal?: { reading_origin?: { reading_mode: string; reading_coverage?: { source_modes?: Record<string, string> } }; study_origin?: { cache_key: string; documents: { display_name: string }[] }; rationale?: string; comparison_note?: string; caveats?: string[] } }; relation?: string; note?: string; question?: string; difference_basis?: string; comparison_note?: string; source_content_id?: string; target_content_id?: string; source_page?: number; target_page?: number; citation?: LibraryCitation };
export type GraphData = { nodes: GraphNode[]; edges: GraphEdge[]; history_truncated: boolean };
export type ReadingGroup = { id: string; title: string; document_ids: string[]; revision: number };
export const NODE_LABELS = { document: "论文", topic: "主题", folder: "目录", question: "研究问题", group: "研读组" };

export function scopeGraph(data: GraphData, ids: string[], kind = "all"): GraphData {
  const edges = data.edges.filter(e => kind === "all" || (kind === "evidence" ? ["citation", "manual", "question_link"].includes(e.kind) : ["tag", "folder", "group"].includes(e.kind)));
  const connected = new Set(edges.flatMap(e => [e.source, e.target]));
  const nodes = kind === "all" ? data.nodes : data.nodes.filter(n => connected.has(n.id));
  if (!ids.length) return { ...data, nodes, edges };
  const wanted = new Set(ids.map(id => "doc:" + id));
  const documents = new Set(data.nodes.filter(n => n.kind === "document").map(n => n.id));
  for (const edge of edges) {
    if (wanted.has(edge.source) && !documents.has(edge.target)) wanted.add(edge.target);
    if (wanted.has(edge.target) && !documents.has(edge.source)) wanted.add(edge.source);
  }
  return { ...data, nodes: nodes.filter(n => wanted.has(n.id)), edges: edges.filter(e => wanted.has(e.source) && wanted.has(e.target)) };
}

export function neighbors(data: GraphData, id: string): Set<string> {
  const result = new Set([id]);
  for (const edge of data.edges) {
    if (edge.source === id) result.add(edge.target);
    if (edge.target === id) result.add(edge.source);
  }
  return result;
}

export function searchGraph(data: GraphData, query: string, relation = "all", sourceState = "all"): GraphData {
  const edges = data.edges.filter(edge =>
    (relation === "all" || edge.kind === "manual" && edge.relation === relation) &&
    (sourceState === "all" || (sourceState === "changed" ? edge.source_changed === true : edge.source_changed !== true)),
  );
  const filtered = relation !== "all" || sourceState !== "all";
  const connected = new Set(edges.flatMap(edge => [edge.source, edge.target]));
  const nodes = filtered ? data.nodes.filter(node => connected.has(node.id)) : data.nodes;
  const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return { ...data, nodes, edges };
  const matches = (text: string) => words.every(word => text.toLocaleLowerCase().includes(word));
  const labels = new Map(nodes.map(node => [node.id, node.label]));
  const matchedNodes = new Set(nodes.filter(node => matches(node.label)).map(node => node.id));
  const matchedEdges = edges.filter(edge => matchedNodes.has(edge.source) || matchedNodes.has(edge.target) || matches([
    labels.get(edge.source), labels.get(edge.target), edge.label, edge.question, edge.note,
    edge.comparison_note, DIFFERENCE_LABELS[edge.difference_basis ?? ""], edge.evidence?.source?.excerpt,
    edge.evidence?.target?.excerpt, edge.evidence?.ai_proposal?.rationale, edge.citation?.excerpt,
  ].filter(Boolean).join(" ")));
  const wanted = new Set([...matchedNodes, ...matchedEdges.flatMap(edge => [edge.source, edge.target])]);
  return { ...data, nodes: nodes.filter(node => wanted.has(node.id)), edges: matchedEdges };
}

export const DIFFERENCE_LABELS: Record<string, string> = { unspecified: "尚未判断", claim: "结论存在冲突", method: "方法或指标不同", sample: "样本或研究对象不同", context: "条件或适用范围不同" };
export function questionKey(value: string) { return value.trim().replace(/\s+/g, " ").toLocaleLowerCase(); }
