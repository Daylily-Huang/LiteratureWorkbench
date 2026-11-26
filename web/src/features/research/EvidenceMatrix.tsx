import { useEffect, useRef, useState } from "react";
import type { DocumentListItem } from "../library/documents";
import { downloadBlob } from "../ui/exportText";
import type { LibraryCitation } from "./AssistantPanel";
import { COMPARISON_PROMPTS } from "./comparisonPrompts";
import { workspaceRequest } from "./workspaceApi";

export type EvidenceRow = {
  id: string; title: string; question: string; note: string; document_ids: string[];
  created_at: string; updated_at: string; scope: string | null; provider: string | null; model: string | null;
  citations: LibraryCitation[];
};
export type EvidenceDocument = DocumentListItem & { available?: boolean };
type EvidencePage = { items: EvidenceRow[]; total: number; offset: number; has_more: boolean; viewed_at: string };
const PAGE_SIZE = 20;

export function EvidenceMatrix({ documents, refreshKey = 0, onOpen, onNotebook, onEvidence, onAsk }: {
  documents: EvidenceDocument[]; refreshKey?: number;
  onOpen(id: string, side: "left" | "right", page?: number): void; onNotebook(id: string): void;
  onEvidence?(citation: LibraryCitation, side: "left" | "right"): void;
  onAsk?(question: string): void;
}) {
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [refresh, setRefresh] = useState(0);
  const scope = JSON.stringify(documents.map(d => d.document_id));
  const base = JSON.stringify([scope, search]);
  const [cursor, setCursor] = useState({ base, offset: 0 });
  const offset = cursor.base === base ? cursor.offset : 0;
  const requestKey = JSON.stringify([base, offset, refresh, refreshKey]);
  const [result, setResult] = useState<{ key: string; value: EvidencePage }>();
  const [failure, setFailure] = useState<{ key: string; message: string }>();
  const [exporting, setExporting] = useState(false);
  const [exportNotice, setExportNotice] = useState<{ key: string; message: string }>();
  const exporter = useRef<AbortController | null>(null);
  const owner = useRef(base); owner.current = base;
  const mounted = useRef(true);
  const params = new URLSearchParams({ q: search });
  for (const id of JSON.parse(scope) as string[]) params.append("document_ids", id);
  const parameters = params.toString();

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; exporter.current?.abort(); exporter.current = null; };
  }, []);
  useEffect(() => {
    exporter.current?.abort(); exporter.current = null; setExporting(false);
  }, [base]);
  useEffect(() => {
    if (scope === "[]") return;
    const controller = new AbortController();
    let active = true;
    void workspaceRequest<EvidencePage>(`notebook/evidence?${parameters}&offset=${offset}&limit=${PAGE_SIZE}`, undefined, "GET", controller.signal)
      .then(value => { if (active) { setResult({ key: requestKey, value }); setFailure(undefined); } })
      .catch(() => { if (active) setFailure({ key: requestKey, message: "证据暂未读取成功，请重试。已有记录仍保存在工作台。" }); });
    return () => { active = false; controller.abort(); };
  }, [requestKey, scope, parameters, offset]);

  const page = result?.key === requestKey ? result.value : null;
  const error = failure?.key === requestKey ? failure.message : "";
  const loading = documents.length > 0 && !page && !error;
  async function exportAll(format: "markdown" | "json") {
    if (exporter.current) return;
    const controller = new AbortController(); exporter.current = controller;
    const key = base;
    setExporting(true); setExportNotice(undefined);
    try {
      const response = await fetch(`/api/workspace/notebook/evidence/export?${parameters}&format=${format}`, { credentials: "same-origin", signal: controller.signal });
      if (!response.ok) throw new Error("export_failed");
      const blob = await response.blob();
      const ending = await blob.slice(-160).text();
      if (!(format === "json" ? /"export_complete":\s*true\s*}\s*$/.test(ending) : ending.endsWith("<!-- literature-workbench-evidence-export-complete -->\n"))) throw new Error("export_incomplete");
      if (controller.signal.aborted || !mounted.current || owner.current !== key) return;
      downloadBlob(`多文献证据对照.${format === "json" ? "json" : "md"}`, blob);
      setExportNotice({ key, message: "已生成导出文件，包含当前筛选的全部记录及原始问答和来源。" });
    } catch {
      if (mounted.current && owner.current === key && !controller.signal.aborted) setExportNotice({ key, message: "导出未完成，请重试。不会重新请求 AI。" });
    } finally {
      if (exporter.current === controller) { exporter.current = null; if (mounted.current) setExporting(false); }
    }
  }
  function cancelExport() {
    exporter.current?.abort(); exporter.current = null; setExporting(false);
    setExportNotice({ key: base, message: "已取消导出。" });
  }
  return <section className="evidence-matrix" aria-label="证据矩阵" aria-busy={loading}>
    <div className="graph-controls matrix-controls"><form onSubmit={event => { event.preventDefault(); setSearch(query.trim()); setCursor({ base: JSON.stringify([scope, query.trim()]), offset: 0 }); if (search === query.trim()) setRefresh(n => n + 1); }}>
      <input aria-label="搜索证据问题" maxLength={2000} placeholder="搜索问题、回答、思考或原文引句…" value={query} onChange={event => setQuery(event.target.value)} />
      <button type="submit" disabled={!documents.length}>搜索证据</button>{(query || search) && <button type="button" onClick={() => { setQuery(""); setSearch(""); }}>清除搜索</button>}
    </form><button disabled={!documents.length || loading} onClick={() => setRefresh(n => n + 1)}>刷新证据</button>
      <button disabled={!page?.total || exporting} onClick={() => void exportAll("markdown")}>导出全部匹配（Markdown）</button><button disabled={!page?.total || exporting} onClick={() => void exportAll("json")}>导出全部匹配（JSON）</button>
      {exporting && <><span role="status">正在整理全部匹配记录…</span><button onClick={cancelExport}>取消导出</button></>}
    </div>
    {exportNotice?.key === base && <p role="status" className="graph-explanation">{exportNotice.message}</p>}
    {onAsk && <nav className="comparison-prompts" aria-label="比较维度"><span>按维度准备比较：</span>{COMPARISON_PROMPTS.map(p => <button key={p.label} disabled={documents.length < 2 || documents.some(d => d.available === false)} onClick={() => onAsk(p.question)}>{p.label}</button>)}<small>先带入助手，点击提问后才调用 AI。</small></nav>}
    <p className="graph-explanation">按当前所选 {documents.length} 篇，查找全部历史中的相关问答；参与原提问或被引用即纳入。未引用表示该次回答未覆盖，不代表论文没有相关证据。导出保留原提问范围与组外依据，不调用 AI。</p>
    {loading && <p role="status">正在读取证据…</p>}
    {error && <p role="alert">{error}<button onClick={() => setRefresh(n => n + 1)}>重试读取证据</button></p>}
    {!documents.length && <div className="studio-empty"><p>请先加入文献或打开研读组。</p></div>}
    {page && <><div className="matrix-pagination"><span>共 {page.total} 条{search && ` · 检索：${search}`} · {page.items.length ? `${offset + 1}–${offset + page.items.length}` : page.total ? "当前页暂无记录" : "0"}</span><button disabled={!offset} onClick={() => setCursor({ base, offset: Math.max(0, offset - PAGE_SIZE) })}>上一页证据</button><button disabled={!page.has_more} onClick={() => setCursor({ base, offset: offset + PAGE_SIZE })}>下一页证据</button></div>
      {!page.items.length && <div className="studio-empty"><h3>{search ? "没有匹配的证据记录" : "把比较建立在具体证据上"}</h3><p>{search ? "可调整关键词；搜索涵盖原回答、个人思考和原文引句。" : "对本组文献提问并保存带来源的回答后，这里会按问题整理原文片段。"}</p>{offset > 0 && <button onClick={() => setCursor({ base, offset: 0 })}>回到第一页证据</button>}</div>}
      {!!page.items.length && <div className="matrix-scroll"><table><thead><tr><th>研究问题 / 我的思考</th>{documents.map(d => <th key={d.document_id}><button disabled={d.available === false} onClick={() => onOpen(d.document_id, "left")}>{d.filename}</button>{d.available === false && <small>原文暂不可用，保留历史证据</small>}</th>)}</tr></thead><tbody>{page.items.map(entry => <tr key={entry.id}><th><button onClick={() => onNotebook(entry.id)}>{entry.question}</button>{entry.note && <details><summary>我的思考</summary><p>{entry.note}</p></details>}<small>引用覆盖 {documents.filter(d => entry.citations.some(c => c.document_id === d.document_id)).length} / {documents.length} 篇 · 查看完整问答与笔记 ↑</small><small>{entry.provider || "来源未记录"} / {entry.model || "模型未记录"} · {new Date(entry.created_at).toLocaleDateString()}</small>{[...entry.document_ids, ...entry.citations.map(c => c.document_id)].some(id => !documents.some(d => d.document_id === id)) && <small>原提问还包含组外文献；完整上下文见记录或导出。</small>}</th>{documents.map(d => {
        const citations = entry.citations.filter(c => c.document_id === d.document_id);
        return <td key={d.document_id}>{!citations.length && <span className="matrix-missing">本次回答未引用</span>}{citations.map((citation, index) => <article key={index}><blockquote>{citation.excerpt}</blockquote>{citation.source_changed && <p className="graph-warning">来源已变化，请重新核对</p>}<div className="graph-actions">{(["left", "right"] as const).map(side => <button key={side} disabled={d.available === false} onClick={() => { if (!citation.source_changed || window.confirm("来源已变化，仍然打开当前文件吗？")) onEvidence ? onEvidence(citation, side) : onOpen(d.document_id, side, citation.physical_page); }}>{side === "left" ? "左栏" : "右栏"} · 第 {citation.physical_page} 页</button>)}</div></article>)}</td>;
      })}</tr>)}</tbody></table></div>}
    </>}
  </section>;
}
