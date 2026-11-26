import { useMemo, useState } from "react";
import type { LibraryCitation } from "./AssistantPanel";
import { downloadText } from "../ui/exportText";
import { DIFFERENCE_LABELS, neighbors, NODE_LABELS, scopeGraph, searchGraph, type GraphData, type GraphEdge, type GraphNode } from "./graphModel";

export function KnowledgeGraph({ data, scope, onOpen, onPair, onNotebook, onGroup, onDeleteRelation, onQuestion, initialFocus, onEvidence }: {
  onQuestion?(question: string): void; initialFocus?: string; onEvidence?(citation: LibraryCitation, side: "left" | "right"): void;
  data: GraphData; scope: string[]; onOpen(id: string, side: "left" | "right", page?: number): void;
  onPair(edge: GraphEdge): void; onNotebook(id: string): void; onGroup(id: string): void; onDeleteRelation(id: string): void;
}) {
  const [filter, setFilter] = useState("evidence");
  const [query, setQuery] = useState("");
  const [relation, setRelation] = useState("all");
  const [sourceState, setSourceState] = useState("all");
  const [focus, setFocus] = useState<string | null>(initialFocus ?? null);
  const [zoom, setZoom] = useState(1);
  const scoped = useMemo(() => scopeGraph(data, scope, filter), [data, scope, filter]);
  const graph = useMemo(() => searchGraph(scoped, query, relation, sourceState), [scoped, query, relation, sourceState]);
  const selected = graph.nodes.find(n => n.id === focus);
  const nearby = focus && selected ? neighbors(graph, focus) : null;
  const matches = [...graph.nodes].sort((a, b) => Number(b.kind === "question") - Number(a.kind === "question"));
  const visible = (nearby ? graph.nodes.filter(n => nearby.has(n.id)) : matches).slice(0, 70);
  const visibleIds = new Set(visible.map(n => n.id));
  const edges = graph.edges.filter(e => visibleIds.has(e.source) && visibleIds.has(e.target));
  const positions = new Map(visible.map((n, i) => {
    if (focus && n.id === focus) return [n.id, { x: 460, y: 330 }];
    const index = i - (focus && visible.some(n => n.id === focus) && i > visible.findIndex(n => n.id === focus) ? 1 : 0);
    const count = Math.max(1, visible.length - (focus ? 1 : 0));
    const angle = 2 * Math.PI * index / count - Math.PI / 2;
    const radius = visible.length > 24 && index % 2 ? 200 : 275;
    return [n.id, { x: 460 + Math.cos(angle) * radius * 1.38, y: 330 + Math.sin(angle) * radius }];
  }));
  const related = selected ? graph.edges.filter(e => e.source === selected.id || e.target === selected.id) : [];
  const hasSearch = !!query.trim() || relation !== "all" || sourceState !== "all";
  function resetSearch() { setFocus(null); setQuery(""); setRelation("all"); setSourceState("all"); setZoom(1); }
  function titleFor(id: string) { return data.nodes.find(n => n.id === id)?.label ?? "文献"; }
  function nodeActions(node: GraphNode) {
    if (node.document_id) return <><button disabled={node.available === false} onClick={() => onOpen(node.document_id!, "left")}>左栏打开</button><button disabled={node.available === false} onClick={() => onOpen(node.document_id!, "right")}>右栏打开</button>{node.available === false && <p>原文件已不可用，保留历史关联。</p>}</>;
    if (node.question_text) return <button onClick={() => onQuestion?.(node.question_text!)}>查看这个问题的证据对照</button>;
    if (node.entry_id) return <button onClick={() => onNotebook(node.entry_id!)}>查看回答与研究笔记</button>;
    if (node.group_id) return <button onClick={() => onGroup(node.group_id!)}>打开这个研读组</button>;
    return null;
  }
  return <section className="knowledge-graph" aria-label="知识图谱">
    <div className="graph-controls"><input aria-label="搜索图谱节点" value={query} placeholder="搜索论文、问题、关系说明或原文依据…" onChange={e => { setQuery(e.target.value); setFocus(null); }} /><select aria-label="图谱关系筛选" value={filter} onChange={e => { setFilter(e.target.value); resetSearch(); }}><option value="all">全部关系</option><option value="evidence">证据与分歧</option><option value="organization">主题与研读组</option></select>{filter !== "organization" && <><select aria-label="筛选文献关系类型" value={relation} onChange={e => { setRelation(e.target.value); setFocus(null); }}><option value="all">所有关系类型</option><option value="supports">支持</option><option value="contradicts">存在分歧</option><option value="method">方法对照</option><option value="related">相关研究</option></select><select aria-label="筛选图谱来源状态" value={sourceState} onChange={e => { setSourceState(e.target.value); setFocus(null); }}><option value="all">所有来源状态</option><option value="changed">来源已变化</option><option value="unchanged">未标记来源变化</option></select></>}<button onClick={resetSearch}>全景</button><button onClick={() => setZoom(z => Math.min(2, z + .25))}>放大</button><button onClick={() => setZoom(z => Math.max(.5, z - .25))}>缩小</button><button onClick={() => downloadText("文献知识图谱.json", JSON.stringify(graph, null, 2))}>导出图谱</button></div>
    <p className="graph-explanation">目录 / 标签是组织线索；“回答引用”来自已保存问答；支持 / 分歧由你标记或确认 AI 候选后加入，需核对原文。{data.history_truncated && " 图谱包含最近 200 条问答；完整历史在研究记录中。"}</p>
    <div className="graph-layout"><div className="graph-canvas"><div className="graph-legend">{Object.entries(NODE_LABELS).map(([kind, label]) => <span key={kind}><i data-kind={kind} />{label}</span>)}</div>
      <svg viewBox={`${460 - 460 / zoom} ${330 - 330 / zoom} ${920 / zoom} ${660 / zoom}`} aria-label="文献关系网络" role="img">
        <defs><marker id="graph-arrow" viewBox="0 0 10 10" refX="19" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" /></marker></defs>
        {edges.map(e => { const a = positions.get(e.source)!; const b = positions.get(e.target)!; return <line key={e.id} x1={a.x} y1={a.y} x2={b.x} y2={b.y} data-kind={e.kind} data-changed={e.source_changed} markerEnd={e.kind === "manual" || e.kind === "citation" ? "url(#graph-arrow)" : undefined}><title>{e.label}{e.kind === "manual" ? (e.origin === "ai_confirmed" ? "（AI 建议，用户确认）" : "（手动标记）") : ""}{e.source_changed ? " · 来源已变化" : ""}</title></line>; })}
        {visible.map(n => { const p = positions.get(n.id)!; return <g key={n.id} role="button" tabIndex={0} aria-label={`${NODE_LABELS[n.kind]}：${n.label}`} aria-pressed={focus === n.id} transform={`translate(${p.x},${p.y})`} data-kind={n.kind} onClick={() => setFocus(n.id)} onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setFocus(n.id); } }}><title>{n.label}</title><circle r={focus === n.id ? 22 : n.kind === "document" ? 12 : 17} /><text y={34} textAnchor="middle">{n.label.length > 19 ? n.label.slice(0, 18) + "…" : n.label}</text></g>; })}
      </svg>
      <p className="graph-count" role="status">{graph.nodes.length} 个节点 · {graph.edges.length} 条关系 · 当前显示 {visible.length} 个节点{(nearby ? nearby.size : matches.length) > 70 && "（最多 70 个，请搜索或聚焦节点）"}{hasSearch && " · 已按当前条件筛选，导出包含全部匹配结果；搜索保留直接关联节点。"}</p>
      {hasSearch && !graph.nodes.length ? <p className="graph-empty">没有匹配的节点或关系，原有图谱未改变。<button onClick={resetSearch}>清除搜索与筛选</button></p> : !scoped.edges.length && <p className="graph-empty">还没有这类关系。保存带来源的回答，或在双栏对照中记录文献关系。<button onClick={() => { setFilter("organization"); resetSearch(); }}>查看目录与主题线索</button></p>}
    </div><aside className="graph-inspector" aria-label="节点与关联详情">
      {selected ? <><button onClick={() => setFocus(null)}>← 返回节点列表</button><span className="eyebrow">{NODE_LABELS[selected.kind]}</span><h3>{selected.label}</h3><div className="graph-actions">{nodeActions(selected)}</div><h4>关联线索 · {related.length}</h4>{related.map(e => <article className="graph-edge" key={e.id}><button className="graph-neighbor" onClick={() => setFocus(e.source === selected.id ? e.target : e.source)}>{titleFor(e.source === selected.id ? e.target : e.source)}</button><p>{e.label} · {e.kind === "manual" ? (e.origin === "ai_confirmed" ? "AI 建议 · 用户已确认，仍需科学核验" : "手动判断，待核对") : e.kind === "citation" ? "AI 回答引用" : e.kind === "question_link" ? "人工问题归属，不代表已验证" : "组织关系"}</p>{e.source_changed && <p className="graph-warning">来源已变化，历史页码需重新核对。</p>}{e.question && <p>研究问题：{e.question}</p>}{e.difference_basis && <p>差异来源：{DIFFERENCE_LABELS[e.difference_basis]}</p>}{e.comparison_note && <blockquote>{e.comparison_note}</blockquote>}{e.evidence?.source && <blockquote>左侧原文：{e.evidence.source.excerpt}</blockquote>}{e.evidence?.target && <blockquote>右侧原文：{e.evidence.target.excerpt}</blockquote>}{e.evidence?.ai_proposal?.reading_origin && <p>来源：全文 / 增量关系分析；旧篇可能复用已核验引句，以生成时的阅读清单为准。本次查看没有调用 AI，关系仍需科学核验。</p>}{e.evidence?.ai_proposal?.study_origin && <p>来源：已有全文研读结论，由用户判断关系后确认；转入未调用 AI。原组：{e.evidence.ai_proposal.study_origin.documents.map(d => d.display_name).join("、")}。</p>}{e.note && <blockquote>{e.note}</blockquote>}{e.citation && <><blockquote>{e.citation.excerpt}</blockquote><button onClick={() => { if (!e.source_changed || window.confirm("来源已变化，仍然打开当前文件对应页码吗？")) onEvidence ? onEvidence({ ...e.citation!, source_changed: e.source_changed || e.citation!.source_changed }, "right") : onOpen(e.citation!.document_id, "right", e.citation!.physical_page); }}>原文第 {e.citation.physical_page} 页</button></>}{e.kind === "manual" && <><p>左侧第 {e.source_page} 页 → 右侧第 {e.target_page} 页</p><button onClick={() => { if (!e.source_changed || window.confirm("来源已变化，仍然对照当前文件吗？")) onPair(e); }}>对照两处原文</button><button onClick={() => onDeleteRelation(e.relation_id!)}>删除这条关系</button></>}</article>)}</>
        : <><h3>选择一个节点，展开它的联系</h3><p>可以从论文进入问题，再沿引用回到另一篇原文。</p><ul className="graph-node-list">{matches.map(n => <li key={n.id}><button onClick={() => setFocus(n.id)}><small>{NODE_LABELS[n.kind]}</small>{n.label}</button></li>)}</ul></>}
    </aside></div>
  </section>;
}
