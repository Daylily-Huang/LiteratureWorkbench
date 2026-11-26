import { useState } from "react";
import { downloadText } from "../ui/exportText";
import { DIFFERENCE_LABELS, questionKey, type GraphData, type GraphEdge } from "./graphModel";

export function evidenceMarkdown(edges: GraphEdge[], data: GraphData) {
  const name = (id: string) => data.nodes.find(n => n.id === id)?.label ?? "已不可用的文献";
  return ["# 研究问题与证据对照", "以下为人工记录或经用户确认的 AI 建议，分类和解释尚需核对原文。", ...edges.map(e => [
    `## ${e.question || "未归属研究问题"}`, `关系：${e.label}（左侧 → 右侧）`,
    `差异来源：${DIFFERENCE_LABELS[e.difference_basis ?? "unspecified"] ?? "尚未判断"}`,
    `左侧：${name(e.source)} · 第 ${e.source_page} 页 · 文献 ID ${e.source} · 版本 ${e.source_content_id ?? "未记录"}`,
    `右侧：${name(e.target)} · 第 ${e.target_page} 页 · 文献 ID ${e.target} · 版本 ${e.target_content_id ?? "未记录"}`,
    `形成方式：${e.origin === "ai_confirmed" ? "AI 建议，经用户确认" : "手动记录"}`,
    ...(e.evidence?.source ? [`左侧原文：${e.evidence.source.excerpt}`, `右侧原文：${e.evidence.target?.excerpt ?? ""}`, `模型：${e.evidence.provider} / ${e.evidence.model}`, `确认时间：${e.evidence.confirmed_at}`, `AI 原始解释：${e.evidence.ai_proposal?.rationale ?? ""}`, `AI 待核实点：${e.evidence.ai_proposal?.caveats?.join("；") ?? ""}`] : []),
    `关系依据：${e.note ?? ""}`, `对照说明：${e.comparison_note || "待补充"}`,
    `来源状态：${e.source_changed ? "已变化或不可用，需重新核对" : "版本与记录一致；不代表判断已经验证"}`,
  ].join("\n\n"))].join("\n\n");
}

export function QuestionEvidence({ data, scope, initialQuestion = "", onPair, onGraph, onDelete, onRead }: {
  data: GraphData; scope: string[]; initialQuestion?: string; onPair(edge: GraphEdge): void;
  onGraph(question: string): void; onDelete(id: string): void; onRead(): void;
}) {
  const [question, setQuestion] = useState(initialQuestion);
  const [basis, setBasis] = useState("all");
  const [relation, setRelation] = useState("all");
  const [changedOnly, setChangedOnly] = useState(false);
  const all = data.edges.filter(e => e.kind === "manual" && (!scope.length || [e.source, e.target].every(id => scope.includes(id.replace(/^doc:/, "")))));
  const questions = [...new Map(all.filter(e => e.question).map(e => [questionKey(e.question!), e.question!])).values()];
  const visible = all.filter(e => (!question || questionKey(e.question ?? "") === questionKey(question)) && (basis === "all" || (e.difference_basis ?? "unspecified") === basis) && (relation === "all" || e.relation === relation) && (!changedOnly || e.source_changed));
  const name = (id: string) => data.nodes.find(n => n.id === id)?.label ?? "已不可用的文献";
  return <section className="question-evidence" aria-label="研究问题与分歧">
    <header><h3>围绕问题，对照证据与差异</h3><p>先看两篇研究是否回答同一个问题，再核对方法、样本和条件。分类来自你的记录，不能仅凭结论不同认定矛盾。</p></header>
    <div className="question-controls">
      <label>研究问题<select aria-label="筛选研究问题" value={question} onChange={e => setQuestion(e.target.value)}><option value="">全部问题（含未归属）</option>{!questions.some(q => questionKey(q) === questionKey(question)) && question && <option>{question}</option>}{questions.map(q => <option key={questionKey(q)}>{q}</option>)}</select></label>
      <label>关系<select aria-label="筛选证据关系" value={relation} onChange={e => setRelation(e.target.value)}><option value="all">全部关系</option><option value="supports">支持</option><option value="contradicts">存在分歧</option><option value="method">方法对照</option><option value="related">相关研究</option></select></label>
      <label>差异来源<select aria-label="筛选差异来源" value={basis} onChange={e => setBasis(e.target.value)}><option value="all">全部差异</option>{Object.entries(DIFFERENCE_LABELS).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label>
      <label><input type="checkbox" checked={changedOnly} onChange={e => setChangedOnly(e.target.checked)} />只看来源已变化</label>
      <button disabled={!visible.length} onClick={() => downloadText("问题与证据对照.md", evidenceMarkdown(visible, data))}>导出当前对照</button>
    </div>
    <p>{visible.length} / {all.length} 条已记录证据关系 · {scope.length ? "当前研读组（两篇均在组内）" : "整个文献库"}</p>
    {!visible.length && <div className="studio-empty"><p>{all.length ? "当前筛选下没有关系，可切换问题或差异类别。" : "还没有人工证据对照。打开两篇论文，填写研究问题与关系依据，即可在这里集中比较。"}</p><button onClick={onRead}>去双栏记录证据</button></div>}
    <div className="question-cards">{visible.map(e => <article key={e.id} className="question-card"><header><h4>{e.question || "未归属研究问题"}</h4><span>{e.label} · {e.origin === "ai_confirmed" ? "AI 建议，经用户确认" : "人工判断，待核对"}</span></header><p className="question-basis">{DIFFERENCE_LABELS[e.difference_basis ?? "unspecified"]}</p><dl><dt>左侧 · 第 {e.source_page} 页</dt><dd>{name(e.source)}</dd><dt>右侧 · 第 {e.target_page} 页</dt><dd>{name(e.target)}</dd>{e.evidence?.source && <><dt>左侧原文</dt><dd>{e.evidence.source.excerpt}</dd><dt>右侧原文</dt><dd>{e.evidence.target?.excerpt}</dd></>}<dt>关系依据</dt><dd>{e.note}</dd><dt>方法、样本与适用条件对照</dt><dd>{e.comparison_note || "尚未补充，不能据此排除条件差异。"}</dd></dl>{e.source_changed && <p className="graph-warning">来源已变化或不可用，历史页码与判断需要重新核对。</p>}<footer><button onClick={() => { if (!e.source_changed || window.confirm("来源已变化，仍然打开当前文件的对应页码吗？")) onPair(e); }}>对照两处原文</button>{e.question && <button onClick={() => onGraph(e.question!)}>在图谱中查看问题</button>}<button onClick={() => onDelete(e.relation_id!)}>删除这条关系</button></footer></article>)}</div>
  </section>;
}
