import { useEffect, useRef, useState, type FormEvent } from "react";

import { downloadText, markdownText } from "../ui/exportText";
import { isValidPdfRect, type PdfRect } from "../reader/CitationHighlight";
import type { ReaderDocument } from "../reader/PdfReader";
import type { SelectedText } from "../reader/selection";
import { coverageText, isIncrementalStudy, parseFullTextCoverage, studyUsageText, StudyCoverage, type FullTextCoverage } from "./fullTextStudy";


type Fetcher = typeof fetch;

export type ResearchCitation = {
  physical_page: number;
  excerpt: string;
  rects: readonly PdfRect[];
};

export type LibraryCitation = {
  document_id: string;
  display_name: string;
  physical_page: number;
  excerpt: string;
  rects: readonly PdfRect[];
  content_id?: string;
  source_changed?: boolean;
};

export type DocumentAnswer = {
  scope: "document";
  answer: string;
  citations: ResearchCitation[];
  provider: string;
  model: string;
  fallback_reason: string | null;
  citation_status?: "verified" | "missing";
};

export type LibraryAnswer = {
  scope: "library";
  answer: string;
  citations: LibraryCitation[];
  provider: string;
  model: string;
  fallback_reason: string | null;
  indexed_documents: number;
  citation_status?: "verified" | "missing";
};

export type Answer = (DocumentAnswer | LibraryAnswer) & { entry_id?: string; history_saved?: boolean; uncovered?: string[]; source_content_id?: string; source_document_id?: string; fullText?: FullTextCoverage };

type Scope = "document" | "library" | "selected";

type AssistantPanelProps = {
  document: ReaderDocument | null;
  documentPending?: boolean;
  onJumpToCitation(citation: ResearchCitation): void;
  onJumpToLibraryCitation?(citation: LibraryCitation): void;
  onOpenSettings?(): void;
  fetcher?: Fetcher;
  selectedDocuments?: { document_id: string; filename: string }[];
  selectionRequest?: number;
  onOpenNotebook?(entryId?: string): void;
  initialQuestion?: string;
  draftQuestion?: string;
  onDraftQuestionConsumed?(): void;
  selectionDraft?: { selection: SelectedText; nonce: number };
  studyDraft?: { documentId: string; nonce: number };
  onStudySaved?(): void;
};


const MAX_QUESTION_CHARS = 2000;


async function csrf(fetcher: Fetcher, signal?: AbortSignal): Promise<string> {
  const response = await fetcher("/api/session/current", {
    credentials: "same-origin",
    signal,
  });
  if (!response.ok) throw new Error("session_unavailable");
  const value = await response.json() as { csrf_token: string };
  return value.csrf_token;
}


export function assistantErrorMessage(code: string | null | undefined): string {
  const known: Record<string, string> = {
    document_not_found: "文献信息不可用，请重新打开文献",
    parse_required: "文档尚未完成解析，请稍后再提问",
    question_empty: "问题不能为空",
    question_too_long: `问题请控制在 ${MAX_QUESTION_CHARS} 字以内`,
    deepseek_key_unavailable: "DeepSeek 密钥未配置，可在设置中添加",
    credential_store_failed: "DeepSeek 凭据暂时无法读取",
    deepseek_authorization_failed: "DeepSeek 密钥未获授权",
    deepseek_request_rejected: "DeepSeek 配置或请求被拒绝",
    qwen_failed: "本地模型暂时不可用",
    selection_version_stale: "选区对应的文件已更新，请重新选择原文",
    selection_not_current: "选区已过期，请重新选择原文",
    selection_parse_pending: "选区所在文献尚未完成解析，请稍后再试",
    selection_text_mismatch: "选区未能与原文匹配，请重新选择更小的范围",
    full_text_not_ready: "所选文献尚未全部解析就绪，请检查解析状态后重试；未调用 AI。",
    full_text_incomplete: "解析页数与 PDF 总页数不一致，不能当作全文发送；请重新解析。未调用 AI。",
    full_text_empty_page: "存在未提取到文字的页面，可能需要 OCR；请检查后重新解析。未调用 AI。",
    full_text_too_large: "全文超过本次发送上限，请减少所选篇数。没有截断，也未调用 AI。",
    full_text_source_changed: "全文版本已变化，本次结果不作为当前证据返回。请重新检查全文；若模型已被调用，可能已计费。",
    full_text_online_required: "全文模式目前需要在线模型。当前为仅本地模式，未发送文献。",
    full_text_plan_changed: "缓存或发送计划已变化，请重新检查全文。本次未调用 AI。",
    full_text_cache_unavailable: "缓存暂时不可读取。为避免意外重复计费，本次未调用 AI，请稍后重试。",
    full_text_unverified_answer: "模型返回的格式或原文引句未通过核验，未展示为可信结论。本次模型请求可能已计费；重试会重新请求。",
    deepseek_request_too_large: "请求超过发送上限，请减少篇数或缩短问题；未调用 AI。",
  };
  return known[code ?? ""] ?? "回答失败，请重试";
}


function parseResearchCitation(item: unknown): ResearchCitation | null {
  if (!item || typeof item !== "object") return null;
  const obj = item as Record<string, unknown>;
  if (typeof obj.physical_page !== "number" || !Number.isInteger(obj.physical_page) || obj.physical_page < 1) {
    return null;
  }
  if (typeof obj.excerpt !== "string") return null;
  const rawRects = Array.isArray(obj.rects) ? obj.rects : [];
  const rects: PdfRect[] = [];
  for (const r of rawRects) {
    if (isValidPdfRect(r)) rects.push(r);
  }
  return {
    physical_page: obj.physical_page,
    excerpt: obj.excerpt,
    rects,
  };
}


function parseLibraryCitation(item: unknown): LibraryCitation | null {
  if (!item || typeof item !== "object") return null;
  const obj = item as Record<string, unknown>;
  if (typeof obj.document_id !== "string" || !obj.document_id) return null;
  if (typeof obj.display_name !== "string") return null;
  if (typeof obj.physical_page !== "number" || !Number.isInteger(obj.physical_page) || obj.physical_page < 1) {
    return null;
  }
  if (typeof obj.excerpt !== "string") return null;
  const rawRects = Array.isArray(obj.rects) ? obj.rects : [];
  const rects: PdfRect[] = [];
  for (const r of rawRects) {
    if (isValidPdfRect(r)) rects.push(r);
  }
  return {
    document_id: obj.document_id,
    display_name: obj.display_name,
    physical_page: obj.physical_page,
    excerpt: obj.excerpt,
    rects,
    ...(typeof obj.content_id === "string" ? { content_id: obj.content_id } : {}),
    ...(obj.source_changed === true ? { source_changed: true } : {}),
  };
}


function parseDocumentResponse(data: unknown): DocumentAnswer | null {
  if (!data || typeof data !== "object") return null;
  const obj = data as Record<string, unknown>;
  if (typeof obj.answer !== "string") return null;
  if (typeof obj.provider !== "string") return null;
  if (typeof obj.model !== "string") return null;
  const fallback_reason = typeof obj.fallback_reason === "string" ? obj.fallback_reason : null;
  const rawCitations = Array.isArray(obj.citations) ? obj.citations : [];
  const citations: ResearchCitation[] = [];
  for (const c of rawCitations) {
    const parsed = parseResearchCitation(c);
    if (parsed) citations.push(parsed);
  }
  const citation_status = obj.citation_status === "missing" || obj.citation_status === "verified"
    ? obj.citation_status
    : undefined;
  return {
    scope: "document",
    answer: obj.answer,
    citations,
    provider: obj.provider,
    model: obj.model,
    fallback_reason,
    citation_status,
  };
}


function parseLibraryResponse(data: unknown): LibraryAnswer | null {
  if (!data || typeof data !== "object") return null;
  const obj = data as Record<string, unknown>;
  if (typeof obj.answer !== "string") return null;
  if (typeof obj.provider !== "string") return null;
  if (typeof obj.model !== "string") return null;
  const fallback_reason = typeof obj.fallback_reason === "string" ? obj.fallback_reason : null;
  const indexed_documents = typeof obj.indexed_documents === "number" ? obj.indexed_documents : 0;
  const rawCitations = Array.isArray(obj.citations) ? obj.citations : [];
  const citations: LibraryCitation[] = [];
  for (const c of rawCitations) {
    const parsed = parseLibraryCitation(c);
    if (parsed) citations.push(parsed);
  }
  const citation_status = obj.citation_status === "missing" || obj.citation_status === "verified"
    ? obj.citation_status
    : undefined;
  return {
    scope: "library",
    answer: obj.answer,
    citations,
    provider: obj.provider,
    model: obj.model,
    fallback_reason,
    indexed_documents,
    citation_status,
  };
}


function providerBadge(answer: Answer): string {
  if (answer.provider === "deepseek" && answer.model) return `DeepSeek · ${answer.model}`;
  if (answer.provider === "qwen") {
    return answer.fallback_reason !== null
      ? "本地模型回答（云端不可用，已自动切换）"
      : "本地模型回答";
  }
  return answer.scope === "library" ? "库内检索" : "文档内检索";
}

const LIBRARY_PROMPTS = [
  { label: "主题概括", prompt: "概括文献库的主要研究主题，并注明各项结论来源。" },
  { label: "方法对比", prompt: "比较不同文献的方法与适用条件，注明来源。" },
  { label: "一致与分歧", prompt: "找出相互支持或矛盾的研究结论，区分条件差异与证据不足。" },
  { label: "延伸灵感", prompt: "基于库内证据提出值得验证的新研究问题，区分已有证据与推测。" },
];

const QUICK_PROMPTS = [
  { label: "总结全文", prompt: "请用学术严谨的语言总结本文的核心研究问题、主要方法、关键实验结果与结论。", preferFullText: true },
  { label: "提炼方法", prompt: "提炼本文的研究方法、实验设计与关键参数。", preferFullText: false },
  { label: "局限与边界", prompt: "分析本文存在的局限性、适用边界与潜在威胁。", preferFullText: false },
  { label: "写作大纲", prompt: "生成结构化的论文写作大纲与论证脉络。", preferFullText: false },
  { label: "延伸灵感", prompt: "基于当前文献，有哪些值得深入探索的新课题或交叉方向？", preferFullText: false },
];

const SELECTED_PROMPTS = [
  { label: "比较研究", prompt: "比较所选论文的研究问题、研究对象、方法和结论，逐篇注明来源；没有检索到的内容标为未覆盖。" },
  { label: "一致与分歧", prompt: "这些论文的结论有哪些一致与分歧？请区分研究条件不同、证据不足和真正的矛盾。" },
  { label: "延伸问题", prompt: "基于这些论文，有哪些可以继续验证的研究问题？请区分原文证据与推测。" },
];

export function AssistantPanel({
  document: readerDocument,
  documentPending = false,
  onJumpToCitation,
  onJumpToLibraryCitation,
  fetcher = fetch,
  initialQuestion = "",
  draftQuestion,
  onDraftQuestionConsumed,
  selectionDraft,
  selectedDocuments = [],
  selectionRequest = 0,
  onOpenNotebook,
  studyDraft,
  onStudySaved,
}: AssistantPanelProps) {
  const [question, setQuestion] = useState(initialQuestion);
  const [scope, setScope] = useState<Scope>("document");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [submittedQuestion, setSubmittedQuestion] = useState("");
  const [answerSource, setAnswerSource] = useState("");
  const [copyStatus, setCopyStatus] = useState("");
  const [stopNotice, setStopNotice] = useState(false);
  const [sourceSelection, setSourceSelection] = useState<SelectedText | null>(null);
  const [readingMode, setReadingMode] = useState<"full_text" | "retrieval">("full_text");
  const [docReadingMode, setDocReadingMode] = useState<"retrieval" | "full_text">("retrieval");
  const [preparation, setPreparation] = useState<FullTextCoverage | null>(null);
  const [checking, setChecking] = useState(false);
  const [reuseAnalysis, setReuseAnalysis] = useState(true);
  const [forceRefresh, setForceRefresh] = useState(false);
  const consumedSelection = useRef<number | undefined>(undefined);
  const consumedStudy = useRef<number | undefined>(undefined);
  const preparedKeyRef = useRef<string | null>(null);
  const questionRef = useRef<HTMLTextAreaElement>(null);

  const requestIdRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const inFlightScopeRef = useRef<Scope | null>(null);
  const prevDocumentIdRef = useRef<string | null>(readerDocument?.documentId ?? null);
  const previousContent = useRef(readerDocument?.contentId);

  const selectedKey = selectedDocuments.map(d => d.document_id).join(",");
  const effectiveScope: Scope = scope === "selected" && selectedDocuments.length ? "selected" : readerDocument === null ? "library" : scope === "selected" ? "document" : scope;
  const fullTextIds = effectiveScope === "selected"
    ? selectedDocuments.map(d => d.document_id)
    : effectiveScope === "document" && readerDocument
      ? [readerDocument.documentId]
      : [];
  const fullTextKey = fullTextIds.join(",");
  const currentPrepareKey = `${fullTextKey}|${question.trim()}|${reuseAnalysis}|${forceRefresh}`;
  useEffect(() => {
    if (preparedKeyRef.current !== currentPrepareKey) {
      preparedKeyRef.current = null;
      setPreparation(null);
    }
  }, [currentPrepareKey, readingMode, docReadingMode, effectiveScope]);
  useEffect(() => {
    if (!selectionRequest && !selectedKey) return;
    abortRef.current?.abort(); requestIdRef.current++; inFlightScopeRef.current = null;
    setBusy(false); setChecking(false); setAnswer(null); setErrorCode(null);
    setScope(selectedKey ? "selected" : "library");
    setSourceSelection(null); setForceRefresh(false);
  }, [selectedKey, selectionRequest]);

  // Sync draft question from parent (e.g. Dashboard)
  useEffect(() => {
    if (draftQuestion && draftQuestion.trim()) {
      setSourceSelection(null);
      setQuestion(draftQuestion);
      onDraftQuestionConsumed?.();
    }
  }, [draftQuestion, onDraftQuestionConsumed]);

  // Cancel in-flight document requests and clear single-document state on document switch
  useEffect(() => {
    const currentDocId = readerDocument?.documentId ?? null;
    if (prevDocumentIdRef.current !== currentDocId) {
      prevDocumentIdRef.current = currentDocId;
      setErrorCode(null);
      if (inFlightScopeRef.current === "document") {
        abortRef.current?.abort();
        abortRef.current = null;
        inFlightScopeRef.current = null;
        requestIdRef.current++;
        setBusy(false);
        setErrorCode(null);
      }
      setAnswer((prev) => (prev?.scope === "library" ? prev : null));
      setSourceSelection(null);
    }
  }, [readerDocument?.documentId]);

  useEffect(() => {
    if (previousContent.current !== readerDocument?.contentId && inFlightScopeRef.current === "document") {
      abortRef.current?.abort(); requestIdRef.current++; inFlightScopeRef.current = null;
      setBusy(false); setErrorCode("selection_version_stale");
    }
    previousContent.current = readerDocument?.contentId;
  }, [readerDocument?.contentId]);

  useEffect(() => {
    if (!selectionDraft || consumedSelection.current === selectionDraft.nonce || selectionDraft.selection.documentId !== readerDocument?.documentId) return;
    consumedSelection.current = selectionDraft.nonce;
    abortRef.current?.abort(); requestIdRef.current++; inFlightScopeRef.current = null;
    setBusy(false); setChecking(false); setAnswer(null); setErrorCode(null); setScope("document");
    setSourceSelection(selectionDraft.selection);
    setQuestion("请解释这段原文的核心含义、研究假设与适用条件。区分作者结论和推断，缺少依据时明确说明。");
    questionRef.current?.focus({ preventScroll: true });
  }, [selectionDraft, readerDocument?.documentId]);

  // A reread action only prepares a draft; checking and sending remain explicit.
  useEffect(() => {
    if (!studyDraft || consumedStudy.current === studyDraft.nonce || selectedKey !== studyDraft.documentId) return;
    consumedStudy.current = studyDraft.nonce;
    abortRef.current?.abort(); requestIdRef.current++; inFlightScopeRef.current = null;
    setBusy(false); setChecking(false); setAnswer(null); setErrorCode(null); setPreparation(null);
    setScope("selected"); setReadingMode("full_text"); setSourceSelection(null);
    setForceRefresh(true);
    setQuestion("请完整研读这篇文献，整理研究问题、方法与样本、主要结果、局限和适用条件，每项结论附原文引句与页码；区分作者结论和推断。");
    questionRef.current?.focus({ preventScroll: true });
  }, [studyDraft, selectedKey]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  function handleScopeChange(nextScope: Scope) {
    if (nextScope !== "document") setSourceSelection(null);
    if (nextScope !== scope) {
      abortRef.current?.abort();
      abortRef.current = null;
      inFlightScopeRef.current = null;
      requestIdRef.current++;
      setBusy(false);
      setChecking(false);
      setScope(nextScope);
      setAnswer(null);
      setErrorCode(null);
    }
  }

  const trimmed = question.trim();
  const isFullText = (effectiveScope === "selected" && readingMode === "full_text")
    || (effectiveScope === "document" && docReadingMode === "full_text" && sourceSelection === null && readerDocument !== null);
  const selectionValid = !sourceSelection || (sourceSelection.documentId === readerDocument?.documentId && sourceSelection.contentId === readerDocument?.contentId && sourceSelection.documentVersionId === readerDocument?.documentVersionId);
  const canAsk = trimmed.length > 0 && trimmed.length <= MAX_QUESTION_CHARS && !busy && selectionValid && !(documentPending && scope === "document") && (!isFullText || preparation !== null);
  const answerChanged = answer?.scope === "document" && answer.source_document_id === readerDocument?.documentId && !!answer.source_content_id && answer.source_content_id !== readerDocument?.contentId;

  async function checkFullText(overrideQuestion?: string, overrideDocIds?: string[]) {
    const queryText = (overrideQuestion ?? trimmed).trim();
    const targetIds = overrideDocIds ?? fullTextIds;
    const targetKey = targetIds.join(",");
    if (busy || !queryText || queryText.length > MAX_QUESTION_CHARS || !targetIds.length) return;
    const requestId = ++requestIdRef.current;
    abortRef.current?.abort();
    const controller = new AbortController(); abortRef.current = controller;
    inFlightScopeRef.current = effectiveScope;
    setBusy(true); setChecking(true); setPreparation(null); setErrorCode(null); setAnswer(null);
    try {
      const token = await csrf(fetcher, controller.signal);
      if (requestId !== requestIdRef.current) return;
      const response = await fetcher("/api/libraries/full-text/prepare", {
        method: "POST", credentials: "same-origin", signal: controller.signal,
        headers: { "Content-Type": "application/json", "X-LW-CSRF": token },
        body: JSON.stringify({ question: queryText, document_ids: targetIds, reuse_analysis: reuseAnalysis, force_refresh: forceRefresh }),
      });
      const data: unknown = await response.json();
      if (requestId !== requestIdRef.current) return;
      if (!response.ok) { setErrorCode(data && typeof data === "object" && "code" in data ? String(data.code) : "research_failed"); return; }
      const parsed = parseFullTextCoverage(data);
      if (!parsed || !parsed.plan_id || parsed.coverage.map(d => d.document_id).join(",") !== targetKey) { setErrorCode("research_failed"); return; }
      preparedKeyRef.current = `${targetKey}|${queryText}|${reuseAnalysis}|${forceRefresh}`;
      setPreparation(parsed);
    } catch (error) {
      if (requestId === requestIdRef.current && !(error instanceof DOMException && error.name === "AbortError")) setErrorCode("research_failed");
    } finally {
      if (requestId === requestIdRef.current) { setBusy(false); setChecking(false); inFlightScopeRef.current = null; }
    }
  }

  async function submit(event?: FormEvent, customQuery?: string): Promise<void> {
    if (event) event.preventDefault();
    const queryToAsk = (customQuery ?? trimmed).trim();
    if (!queryToAsk || queryToAsk.length > MAX_QUESTION_CHARS || busy || !selectionValid || (documentPending && scope === "document")) return;
    if (isFullText && !preparation) return;

    const requestId = ++requestIdRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    inFlightScopeRef.current = effectiveScope;

    setBusy(true);
    setChecking(false);
    setSubmittedQuestion(queryToAsk);
    setAnswerSource(effectiveScope === "selected" ? `所选 ${selectedDocuments.length} 篇：${selectedDocuments.map(d => d.filename).join("、")}` : effectiveScope === "library" ? "整个文献库" : `${readerDocument?.filename ?? "当前文献"}${sourceSelection ? ` · 第 ${sourceSelection.pdfPage} 页选区` : ""}`);
    setCopyStatus("");
    setStopNotice(false);
    setAnswer(null);
    setErrorCode(null);

    try {
      const token = await csrf(fetcher, controller.signal);
      if (requestId !== requestIdRef.current) return;

      const useLibraryEndpoint = effectiveScope !== "document" || isFullText;
      const url = useLibraryEndpoint
        ? "/api/libraries/ask"
        : `/api/documents/${readerDocument?.documentId ?? ""}/ask`;

      const response = await fetcher(url, {
        method: "POST",
        credentials: "same-origin",
        signal: controller.signal,
        headers: { "Content-Type": "application/json", "X-LW-CSRF": token },
        body: JSON.stringify({ question: queryToAsk, ...(effectiveScope === "selected" || isFullText ? { document_ids: fullTextIds } : {}),
          ...(isFullText && preparation ? { reading_mode: "full_text", source_versions: preparation.source_versions, plan_id: preparation.plan_id, reuse_analysis: reuseAnalysis, force_refresh: forceRefresh } : {}),
          ...(effectiveScope === "document" && !isFullText && sourceSelection ? { source_selection: { document_version_id: sourceSelection.documentVersionId, content_id: sourceSelection.contentId, physical_page: sourceSelection.pdfPage, selected_text: sourceSelection.text } } : {}),
        }),
      });

      if (requestId !== requestIdRef.current) return;

      const rawJson: unknown = await response.json();
      if (requestId !== requestIdRef.current) return;

      if (!response.ok) {
        setAnswer(null);
        const code = rawJson && typeof rawJson === "object" && "code" in rawJson && typeof (rawJson as { code: unknown }).code === "string"
          ? (rawJson as { code: string }).code
          : "research_failed";
        setErrorCode(code);
        if (isFullText && ["full_text_plan_changed", "full_text_source_changed", "full_text_not_ready", "full_text_incomplete", "full_text_empty_page", "full_text_too_large"].includes(code)) setPreparation(null);
        return;
      }

      const parsedAnswer = useLibraryEndpoint
        ? parseLibraryResponse(rawJson)
        : parseDocumentResponse(rawJson);

      if (!parsedAnswer) {
        setAnswer(null);
        setErrorCode("research_failed");
        return;
      }

      const archive = rawJson as { entry_id?: unknown; history_saved?: unknown; content_id?: unknown };
      const fullText = parseFullTextCoverage((rawJson as { reading_coverage?: unknown }).reading_coverage);
      if (isFullText && !fullText) { setErrorCode("research_failed"); return; }
      const citedIds = new Set(parsedAnswer.scope === "library" ? parsedAnswer.citations.map(c => c.document_id) : []);
      setAnswer({ ...parsedAnswer, entry_id: typeof archive.entry_id === "string" ? archive.entry_id : undefined, history_saved: archive.history_saved === true,
        ...(fullText ? { fullText } : {}),
        source_document_id: effectiveScope === "document" ? readerDocument?.documentId : undefined,
        source_content_id: effectiveScope === "document" ? typeof archive.content_id === "string" ? archive.content_id : readerDocument?.contentId : undefined,
        uncovered: effectiveScope === "selected" ? selectedDocuments.filter(d => !citedIds.has(d.document_id)).map(d => d.filename) : [],
      });
      if (isFullText && forceRefresh) setForceRefresh(false);
      if (fullText) onStudySaved?.();
      if (parsedAnswer.provider === "deepseek" && fullText?.api_call_required !== false) {
        window.dispatchEvent(new Event("deepseek-usage-updated"));
      }
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === "AbortError") {
        return;
      }
      if (requestId === requestIdRef.current) {
        setAnswer(null);
        setErrorCode("research_failed");
      }
    } finally {
      if (requestId === requestIdRef.current) {
        setBusy(false);
        inFlightScopeRef.current = null;
      }
    }
  }

  function stopWaiting() {
    abortRef.current?.abort();
    abortRef.current = null;
    inFlightScopeRef.current = null;
    requestIdRef.current++;
    setBusy(false);
    setChecking(false);
    setStopNotice(true);
  }

  function answerText(): string {
    if (!answer) return "";
    const sources = answer.citations.map((citation, index) => {
      const name = "display_name" in citation ? citation.display_name : answerSource;
      return `${index + 1}. ${markdownText(name)} · 第 ${citation.physical_page} 页\n   > ${markdownText(citation.excerpt).replace(/\r?\n/g, "\n   > ")}`;
    });
    return [`# 研究问答`, `问题：${markdownText(submittedQuestion)}`, `范围：${markdownText(answerSource)}`, ...(answer.fullText ? [`${studyUsageText(answer.fullText)}。原文版本覆盖：${markdownText(coverageText(answer.fullText))}。不含图片视觉理解；原文引句已核对，推论仍需核实。`] : []), markdownText(answer.answer), `## 原文来源`, sources.length ? sources.join("\n\n") : "没有可验证引用，请核对原文。"].join("\n\n");
  }

  async function copyAnswer() {
    try { await navigator.clipboard.writeText(answerText()); setCopyStatus("已复制回答与来源"); }
    catch { setCopyStatus("复制未成功，可使用导出回答保存。"); }
  }

  function handlePromptClick(prompt: string, preferFullText = false) {
    setQuestion(prompt);
    if (preferFullText && effectiveScope === "document" && readerDocument && sourceSelection === null) {
      setDocReadingMode("full_text");
      void checkFullText(prompt, [readerDocument.documentId]);
    }
  }

  const placeholder = effectiveScope === "selected"
    ? `仅基于所选 ${selectedDocuments.length} 篇论文研读，逐项核对来源`
    : effectiveScope === "library"
      ? "就整个文献库提问，答案会标注出自哪篇文献的哪一页"
      : isFullText
        ? "基于当前文献全部页面研读提问，结论附原文引句与页码"
        : "就当前文献提问，答案会标注来源页码";

  return (
    <section aria-label="研究助手问答" className="assistant-panel">
      {/* Scope Switcher */}
      <div aria-label="回答范围" className="assistant-scope" role="group">
        <button
          aria-pressed={effectiveScope === "document"}
          className={`assistant-scope__option${effectiveScope === "document" ? " assistant-scope__option--active" : ""}`}
          disabled={readerDocument === null}
          onClick={() => handleScopeChange("document")}
          title={readerDocument === null ? "打开一篇文献后可就单篇提问" : "仅基于当前文献回答"}
          type="button"
        >
          当前文献
        </button>
        <button
          aria-pressed={effectiveScope === "library"}
          className={`assistant-scope__option${effectiveScope === "library" ? " assistant-scope__option--active" : ""}`}
          onClick={() => handleScopeChange("library")}
          title="跨整个文献库检索回答"
          type="button"
        >
          整个文献库
        </button>
        {!!selectedDocuments.length && <button type="button" className={`assistant-scope__option${effectiveScope === "selected" ? " assistant-scope__option--active" : ""}`} aria-pressed={effectiveScope === "selected"} onClick={() => handleScopeChange("selected")}>所选 {selectedDocuments.length} 篇</button>}
      </div>

      {effectiveScope === "document" && readerDocument !== null && sourceSelection === null && (
        <section className="assistant-full-text assistant-full-text--compact" aria-label="当前文献阅读方式">
          <div className="assistant-depth-row">
            <span className="assistant-depth-label">阅读深度</span>
            <div className="assistant-depth-toggle" role="group" aria-label="当前文献阅读深度">
              <button
                type="button"
                aria-pressed={docReadingMode === "retrieval"}
                className={`assistant-depth-toggle__btn${docReadingMode === "retrieval" ? " assistant-depth-toggle__btn--active" : ""}`}
                disabled={busy}
                onClick={() => { setDocReadingMode("retrieval"); setAnswer(null); setErrorCode(null); }}
              >
                快速检索
              </button>
              <button
                type="button"
                aria-pressed={docReadingMode === "full_text"}
                className={`assistant-depth-toggle__btn${docReadingMode === "full_text" ? " assistant-depth-toggle__btn--active" : ""}`}
                disabled={busy}
                onClick={() => { setDocReadingMode("full_text"); setAnswer(null); setErrorCode(null); }}
              >
                全文研读
              </button>
            </div>
          </div>
          {docReadingMode === "full_text" && (
            <div className="assistant-fulltext-compact-body">
              <div className="assistant-fulltext-action-row">
                <span className="assistant-panel__hint">通读全部页面 · 命中缓存不计费</span>
                <button type="button" className="button button--secondary assistant-check-btn" disabled={busy || !trimmed} onClick={() => void checkFullText()}>检查全文</button>
              </div>
              <details className="assistant-advanced">
                <summary>缓存选项</summary>
                <label className="assistant-panel__hint"><input type="checkbox" disabled={busy || forceRefresh} checked={reuseAnalysis} onChange={e => setReuseAnalysis(e.target.checked)} /> 优先复用已有全文分析</label>
                <label className="assistant-panel__hint"><input type="checkbox" disabled={busy} checked={forceRefresh} onChange={e => setForceRefresh(e.target.checked)} /> 跳过缓存，重新读全文（会调用 API）</label>
              </details>
              {preparation && <StudyCoverage study={preparation} />}
            </div>
          )}
        </section>
      )}

      {effectiveScope === "selected" && <details className="assistant-selection"><summary>本次研读范围 · {selectedDocuments.length} 篇</summary><ol>{selectedDocuments.map(d => <li key={d.document_id}>{d.filename}</li>)}</ol><small>文中“第一篇、第二篇”按此顺序解释；改变顺序需重新检查，可能生成新回答，旧篇仍优先复用核验引句。</small></details>}
      {effectiveScope === "selected" && <section className="assistant-full-text" aria-label="所选文献阅读方式">
        <label>阅读方式 <select aria-label="阅读方式" disabled={busy} value={readingMode} onChange={e => { setReadingMode(e.target.value as "full_text" | "retrieval"); setAnswer(null); setErrorCode(null); }}>
          <option value="full_text">全文研读（全部解析文字）</option><option value="retrieval">快速问答（检索片段）</option>
        </select></label>
        {isFullText && <><p className="assistant-panel__hint">先检查再提问：同问题命中缓存不调用 API；新问题优先复用已有分析，生成新回答仍可能计费。</p>
          <label className="assistant-panel__hint"><input type="checkbox" disabled={busy || forceRefresh} checked={reuseAnalysis} onChange={e => setReuseAnalysis(e.target.checked)} /> 旧篇和新问题优先复用已有分析</label>
          <label className="assistant-panel__hint"><input type="checkbox" disabled={busy} checked={forceRefresh} onChange={e => setForceRefresh(e.target.checked)} /> 跳过缓存，重新读全文（会调用 API）</label>
          <button type="button" disabled={busy || !trimmed} onClick={() => void checkFullText()}>检查全文</button>
          {preparation && <StudyCoverage study={preparation} />}</>}
      </section>}
      {sourceSelection && <section className="assistant-passage" aria-label="选区精读原文"><header><strong>选区精读 · 第 {sourceSelection.pdfPage} 页</strong><button type="button" onClick={() => setSourceSelection(null)}>移除选区</button></header><blockquote>{sourceSelection.text}</blockquote>
        <small>{selectionValid ? "仅在点击提问后发送选区与当前篇检索片段。" : "文件或版本已更新，请重新选择原文；也可移除选区后普通提问。"}</small>
        <div>{["解释这段话的核心含义与术语，用通俗语言说明，并标出原文依据。", "分析这段方法的关键假设、步骤与适用条件，原文未交代的部分标为待核实。", "这段结论有哪些局限？哪些内容来自原文，哪些属于推断？请分别说明。"].map((q, i) => <button key={q} disabled={busy} type="button" onClick={() => setQuestion(q)}>{["解释含义", "方法与假设", "局限与边界"][i]}</button>)}</div></section>}
      {/* Compact Quick Prompt Chips */}
      <div className="assistant-quick-chips" aria-label="推荐研究问题">
        {(effectiveScope === "selected" ? SELECTED_PROMPTS : effectiveScope === "library" ? LIBRARY_PROMPTS : QUICK_PROMPTS).map((item, idx) => (
          <button
            key={idx}
            disabled={busy}
            className={`assistant-quick-chip${"preferFullText" in item && item.preferFullText ? " assistant-quick-chip--highlight" : ""}`}
            onClick={() => handlePromptClick(item.prompt, "preferFullText" in item && Boolean(item.preferFullText))}
            title={item.prompt}
            type="button"
          >
            {item.label}
          </button>
        ))}
      </div>

      {/* Question Form */}
      <form className="assistant-panel__form" onSubmit={(event) => { void submit(event); }}>
        <div className="assistant-input-wrap">
          <textarea
            ref={questionRef}
            aria-label={effectiveScope === "selected" ? "向所选文献提问" : effectiveScope === "library" ? "向文献库提问" : "向当前文献提问"}
            disabled={busy}
            maxLength={MAX_QUESTION_CHARS}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={event => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void submit(); } }}
            placeholder={placeholder}
            value={question}
          />
        </div>
        <div className="assistant-panel__actions">
          <span className="assistant-panel__hint">
            {effectiveScope === "selected" ? isFullText ? "所选全文研读 · 结论附原文" : "所选文献片段检索" : effectiveScope === "library" ? "基于整个文献库检索回答" : isFullText ? "当前文献全文研读 · 结论附原文" : "基于当前文献片段检索"}
          </span>
          <button
            className="button button--primary assistant-submit-btn"
            disabled={!canAsk}
            type="submit"
          >
            {busy ? "回答中…" : "提问"}
          </button>
        </div>
      </form>

      {stopNotice && <p role="status" className="assistant-panel__hint">已停止等待；服务端可能仍在处理这次请求。</p>}
      {(busy || answer !== null) && <div className="assistant-question"><span>{answerSource}</span><p>{submittedQuestion}</p></div>}
      {busy && (
        <div className="assistant-status-card" role="status">
          <span className="assistant-status-card__spinner" />
          <span>{checking ? "正在检查正文和可复用缓存，不调用 AI…" : isFullText ? preparation?.cache_status === "hit" ? "正在核对并读取本地缓存…" : isIncrementalStudy(preparation) ? "正在研读新增/变化文献，并结合旧篇分析对比…" : preparation?.context_kind === "analysis_reuse" ? "正在复用分析回答新问题并核对原句…" : "正在提供所选全文并核验回答中的原文引句…" : "正在检索相关片段并生成解答…"}</span><button className="button button--ghost" type="button" onClick={stopWaiting}>停止等待</button>
        </div>
      )}

      {errorCode !== null && (
        <p className="assistant-error" role="alert">{assistantErrorMessage(errorCode)}</p>
      )}

      {answer !== null && (
        <article className="assistant-answer">
          <div className="assistant-answer__header">
            <span className="assistant-answer__badge">{providerBadge(answer)}</span>
            {answer.fullText && <small className="assistant-cache-status" aria-label="研读缓存状态">{studyUsageText(answer.fullText)}</small>}
            {answer.scope === "library" && (
              <span className="assistant-answer__coverage">
                {answer.fullText ? "原文版本覆盖" : "可检索"} {answer.indexed_documents} 篇 · 引用 {new Set(answer.citations.map(c => c.document_id)).size} 篇
              </span>
            )}
          </div>
          <div className="assistant-answer__actions"><button type="button" onClick={() => void copyAnswer()}>复制回答与来源</button><button type="button" onClick={() => downloadText("研究问答.md", answerText())}>导出回答</button></div>
          <p className="assistant-panel__hint">{answer.history_saved ? "已自动保存研究记录" : "研究记录未保存，可导出当前回答"}{answer.history_saved && onOpenNotebook && <button type="button" onClick={() => onOpenNotebook(answer.entry_id)}>查看记录 / 写笔记</button>}</p>
          {copyStatus && <p role="status">{copyStatus}</p>}
          {answer.fullText && <><StudyCoverage study={answer.fullText} sent /><p className="assistant-panel__hint">每条结论所附引句已与对应页文字核对；这不等于推论或比较关系已经科学验证。</p></>}
          {answer.fullText?.cache_saved === false && <p role="note" className="assistant-panel__hint">回答已生成，但缓存保存失败；下次提问可能重新计费，请先导出保存。</p>}
          <p className="assistant-answer__text">{answer.answer}</p>
          {answerChanged && <p className="assistant-answer__missing-hint" role="note">这份回答对应的 PDF 已变化。摘录仍为当时版本；查看当前原文时不套用旧高亮坐标。</p>}
          {!!answer.uncovered?.length && <p className="assistant-answer__missing-hint">本次回答没有引用以下论文，不能视为已完成比较：{answer.uncovered.join("、")}</p>}

          {(answer.citation_status === "missing" || answer.citations.length === 0) && (
            <p className="assistant-answer__missing-hint" role="note">
              回答未生成可验证的原文引用，请核对原文。
            </p>
          )}

          {answer.citations.length > 0 && (
            <div aria-label="引用来源" className="assistant-citations">
              <span className="assistant-citations__title">引用来源:</span>
              {answer.scope === "library"
                ? answer.citations.map((citation, index) => (
                  <button
                    className="assistant-citation"
                    key={`${citation.document_id}:${citation.physical_page}:${index}`}
                    onClick={() => {
                      if (onJumpToLibraryCitation) onJumpToLibraryCitation(citation);
                      else onJumpToCitation(citation);
                    }}
                    title={citation.excerpt}
                    type="button"
                  >
                    《{citation.display_name}》第 {citation.physical_page} 页
                  </button>
                ))
                : answer.citations.map((citation, cIdx) => (
                  <button
                    className="assistant-citation"
                    key={`${citation.physical_page}:${cIdx}`}
                    onClick={() => { if (!answerChanged || window.confirm("原文已变化，仍然打开当前文件的对应页码吗？")) onJumpToCitation(answerChanged ? { ...citation, rects: [] } : citation); }}
                    title={citation.excerpt}
                    type="button"
                  >
                    第 {citation.physical_page} 页
                  </button>
                ))}
            </div>
          )}
        </article>
      )}
    </section>
  );
}
