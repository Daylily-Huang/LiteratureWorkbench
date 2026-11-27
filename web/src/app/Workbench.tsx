import { lazy, Suspense, useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

import { AnnotationPanel } from "../features/annotations/AnnotationPanel";
import {
  createAnnotation,
  deleteAnnotation,
  mergeSelectionIdentity,
  restoreAnnotationBaseline,
  undoAnnotation,
  updateAnnotation,
  type PdfAnnotation,
} from "../features/annotations/annotations";
import { LiteratureDesk } from "../features/library/LiteratureDesk";
import { StudyHistory } from "../features/research/StudyHistory";
import { DataCenter } from "../features/settings/DataCenter";
import { QuickOpen, type QuickCommand } from "../features/library/QuickOpen";
import { ResearchStudio } from "../features/research/ResearchStudio";
import { ResearchNotebook } from "../features/research/ResearchNotebook";
import { addressNotice, parseLaunchAddress, type LaunchAddress } from "../features/settings/LaunchAddress";
import { DashboardHome } from "../features/dashboard/DashboardHome";
import { LibraryPanel } from "../features/library/LibraryPanel";
import { readReadingRecords, useReadingRecords } from "../features/library/readingRecords";
import type { DocumentListItem } from "../features/library/documents";
import type { PdfRect } from "../features/reader/CitationHighlight";
import type { CitationJump, ReaderDocument } from "../features/reader/PdfReader";
import { ReaderTabs, readReaderTabs, saveReaderTabs, type ReaderTab } from "../features/reader/ReaderTabs";
import { useAnnotatedDocument } from "../features/reader/useAnnotatedDocument";
import { ComparisonPane } from "../features/reader/ComparisonPane";
import { ResizableReadingSplit } from "../features/reader/ResizableReadingSplit";
import type { SelectedText } from "../features/reader/selection";
import { AssistantPanel, type LibraryCitation, type ResearchCitation } from "../features/research/AssistantPanel";
import { SettingsDialog } from "../features/settings/SettingsDialog";
import { DeepSeekBilling } from "../features/settings/DeepSeekBilling";
import { TranslationPopover } from "../features/translation/TranslationPopover";
import { ConfirmDialog } from "../features/ui/ConfirmDialog";
import { SidebarHandle } from "../features/ui/SidebarHandle";
import { useSidebarLayout } from "../features/ui/useSidebarLayout";


const PdfReader = lazy(() => import("../features/reader/PdfReader").then(module => ({ default: module.PdfReader })));

type ReaderLocation = { documentId: string | null; initialPage: number };
type ThemePreference = "system" | "light" | "dark";
type Confirmation = { document: ReaderDocument; side: "left" | "right" } & (
  | { kind: "delete"; annotation: PdfAnnotation }
  | { kind: "restore" });


function readerLocation(): ReaderLocation {
  const params = new URLSearchParams(window.location.search);
  const documentId = params.get("document") ?? params.get("documentId");
  const rawPage = Number(params.get("page"));
  const initialPage = Number.isInteger(rawPage) && rawPage > 0 ? rawPage : (documentId ? readReadingRecords()[documentId]?.page ?? 1 : 1);
  return { documentId, initialPage };
}

function updateReaderLocation(location: ReaderLocation) {
  const params = new URLSearchParams();
  if (location.documentId !== null) params.set("document", location.documentId);
  if (location.initialPage >= 1) params.set("page", String(location.initialPage));
  const query = params.toString();
  const next = query.length === 0 ? "/" : `/?${query}`;
  window.history.pushState(null, "", next);
}


export function Workbench() {
  const { records: readingRecords, update: updateReading, storageFailed, ready: readingReady, retry: retryReading } = useReadingRecords();
  const [studioIds, setStudioIds] = useState<string[] | null>(null);
  const [studyReview, setStudyReview] = useState<{ id: string; document_ids: string[] } | null>(null);
  const [deskOpen, setDeskOpen] = useState(false);
  const [notebook, setNotebook] = useState<{ entryId?: string; query?: string } | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [researchIds, setResearchIds] = useState<string[]>([]);
  const [selectionRequest, setSelectionRequest] = useState(0);
  const importInputRef = useRef<HTMLInputElement>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [quickOpen, setQuickOpen] = useState(false);
  const [dataCenterOpen, setDataCenterOpen] = useState(false);
  const [recoveredWorkspace, setRecoveredWorkspace] = useState("");
  const [launchAddress, setLaunchAddress] = useState<LaunchAddress | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/data/context", { credentials: "same-origin", signal: controller.signal })
      .then(async response => { if (!response.ok) return; const value = await response.json() as { recovered?: boolean; data_root?: string; launch_address?: unknown }; if (!controller.signal.aborted) { if (value.recovered && value.data_root) setRecoveredWorkspace(value.data_root); setLaunchAddress(parseLaunchAddress(value.launch_address)); } })
      .catch(() => {});
    return () => controller.abort();
  }, []);
  const [location, setLocation] = useState<ReaderLocation>(readerLocation);
  const [readerTabs, setReaderTabs] = useState(() => readReaderTabs(location.documentId));
  const [closedTab, setClosedTab] = useState<ReaderTab | null>(null);
  const [splitReading, setSplitReading] = useState(false);
  const [rightDocumentId, setRightDocumentId] = useState<string | null>(null);
  const [rightScales, setRightScales] = useState<Record<string, number>>({});
  const left = useAnnotatedDocument(location.documentId);
  const { document, annotations, busy: annotationBusy, documentError, runMutation } = left;
  const [activeSide, setActiveSide] = useState<"left" | "right">("left");
  const [rightJump, setRightJump] = useState<CitationJump | null>(null);
  const [rightAnnotationId, setRightAnnotationId] = useState<string | null>(null);
  const [documentList, setDocumentList] = useState<DocumentListItem[]>([]);
  const [translationSelection, setTranslationSelection] = useState<SelectedText | null>(null);
  const [activeAnnotationId, setActiveAnnotationId] = useState<string | null>(null);
  const [pageRequestId, setPageRequestId] = useState(0);
  const [requestedPage, setRequestedPage] = useState<number | undefined>();
  const [citationHighlight, setCitationHighlight] = useState<CitationJump | null>(null);

  // A library-scope citation pointing at another document: applied once the
  // target document finishes loading.
  const [pendingCitation, setPendingCitation] = useState<{
    documentId: string;
    page: number;
    rects: readonly PdfRect[] | number[][];
    expectedContentId?: string;
    sourceChanged?: boolean;
  } | null>(null);
  const [draftQuestion, setDraftQuestion] = useState("");
  const [studyHistoryId, setStudyHistoryId] = useState<string | null>(null);
  const [studyRevision, setStudyRevision] = useState(0);
  const [studyDraft, setStudyDraft] = useState<{ documentId: string; nonce: number }>();
  const [selectionDraft, setSelectionDraft] = useState<{ selection: SelectedText; nonce: number }>();
  const [focusMode, setFocusMode] = useState(false);
  const [assistantTab, setAssistantTab] = useState<"ai" | "annotations">("ai");
  const [autoTranslate, setAutoTranslate] = useState<boolean>(() => {
    try { return localStorage.getItem("lw-auto-translate") !== "false"; } catch { return true; }
  });
  const readingDocument = location.documentId !== null && !deskOpen;
  const right = useAnnotatedDocument(splitReading && readingDocument ? rightDocumentId : null);
  const assistantOnRight = splitReading && readingDocument && activeSide === "right" && rightDocumentId !== null;
  const current = assistantOnRight ? right : left;
  const currentDocument = current.document;
  const sidebar = useSidebarLayout(readingDocument, focusMode);
  const { libraryVisible, assistantVisible, setLibraryExpanded, setAssistantExpanded } = sidebar;
  const showLibraryPane = libraryVisible && !deskOpen;
  const [compact, setCompact] = useState(false);
  const [mobilePanel, setMobilePanel] = useState<"library" | "assistant" | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [theme, setTheme] = useState<ThemePreference>(() => {
    try {
      const stored = localStorage.getItem("lw-theme");
      return stored === "light" || stored === "dark" ? stored : "system";
    } catch { return "system"; }
  });
  const [resolvedTheme, setResolvedTheme] = useState<"light" | "dark">("light");
  const libraryAside = useRef<HTMLElement>(null);
  const assistantAside = useRef<HTMLElement>(null);
  const clearTranslationSelection = useCallback(() => {
    setTranslationSelection(value => value?.documentId === activeDocumentIdRef.current ? null : value);
  }, []);

  useEffect(() => {
    const quick = (event: KeyboardEvent) => {
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (event.defaultPrevented || event.repeat || event.isComposing || event.altKey || event.shiftKey || !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "k") return;
      if (target?.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"]') || window.document.querySelector('[aria-modal="true"]')) return;
      event.preventDefault(); setQuickOpen(true);
    };
    window.addEventListener("keydown", quick);
    return () => window.removeEventListener("keydown", quick);
  }, []);

  function activatePane(side: "left" | "right") {
    setActiveSide(side);
    setConfirmation(null);
  }

  useEffect(() => {
    setConfirmation(null);
    setRightJump(null); setRightAnnotationId(null);
  }, [rightDocumentId, splitReading]);

  useEffect(() => {
    setTranslationSelection(selected => {
      if (!selected) return null;
      const owner = [document, right.document].find(item => item?.documentId === selected.documentId);
      if (!owner) return null;
      if (owner.documentVersionId === selected.documentVersionId && owner.contentId === selected.contentId) return selected;
      return mergeSelectionIdentity(selected, { document_id: owner.documentId, current_document_version_id: owner.documentVersionId, content_id: owner.contentId });
    });
  }, [document, right.document]);

  useEffect(() => {
    if (!focusMode) return;
    const exitFocus = (event: KeyboardEvent) => {
      if (!event.defaultPrevented && event.key === "Escape" && !window.document.querySelector('[role="dialog"]')) setFocusMode(false);
    };
    window.addEventListener("keydown", exitFocus);
    return () => window.removeEventListener("keydown", exitFocus);
  }, [focusMode]);

  useEffect(() => {
    const media = window.matchMedia?.("(max-width: 960px)");
    const update = () => setCompact(media?.matches ?? false);
    update(); media?.addEventListener?.("change", update);
    return () => media?.removeEventListener?.("change", update);
  }, []);

  useEffect(() => {
    if (!compact || mobilePanel === null || settingsOpen || confirmation) return;
    const panel = mobilePanel === "library" ? libraryAside.current : assistantAside.current;
    const previous = window.document.activeElement as HTMLElement | null;
    const controls = () => [...(panel?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]):not([tabindex="-1"]), textarea:not([disabled]), select:not([disabled]), summary') ?? [])].filter(el => el.getClientRects().length > 0);
    controls()[0]?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (window.document.querySelector(".modal-dialog")) return;
      if (event.key === "Escape") { event.preventDefault(); setMobilePanel(null); }
      if (event.key !== "Tab") return;
      const items = controls(); const first = items[0]; const last = items.at(-1);
      if (event.shiftKey && window.document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && window.document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    window.document.addEventListener("keydown", handleKey);
    return () => { window.document.removeEventListener("keydown", handleKey); previous?.focus(); };
  }, [compact, mobilePanel, settingsOpen, confirmation]);

  useEffect(() => {
    const updateLocation = () => {
      const next = readerLocation();
      setDeskOpen(false);
      if (next.documentId === activeDocumentIdRef.current) {
        setRequestedPage(next.initialPage);
        setPageRequestId(n => n + 1);
      }
      setLocation(next);
    };
    window.addEventListener("popstate", updateLocation);
    return () => window.removeEventListener("popstate", updateLocation);
  }, []);

  useEffect(() => {
    const media = typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-color-scheme: dark)")
      : null;
    const resolve = () => setResolvedTheme(theme === "system" ? media?.matches ? "dark" : "light" : theme);
    resolve();
    media?.addEventListener?.("change", resolve);
    return () => media?.removeEventListener?.("change", resolve);
  }, [theme]);

  useEffect(() => {
    setConfirmation(null);
    setRightDocumentId(current => current === location.documentId ? null : current);
    setTranslationSelection(null);
    setActiveAnnotationId(null);
    setRequestedPage(undefined);
    setCitationHighlight(null);
    setActiveSide("left");
    setPendingCitation(prev => prev?.documentId === location.documentId ? prev : null);
  }, [location.documentId]);

  useEffect(() => {
    setReaderTabs(current => {
      let changed = false;
      const next = current.map(tab => {
        const title = documentList.find(item => item.document_id === tab.documentId)?.filename
          ?? (document?.documentId === tab.documentId ? document.filename : tab.title);
        if (title === tab.title) return tab;
        changed = true;
        return { ...tab, title };
      });
      if (location.documentId && !next.some(tab => tab.documentId === location.documentId)) {
        next.push({ documentId: location.documentId, title: "正在读取文献…", scale: 1 });
        changed = true;
      }
      return changed ? next : current;
    });
  }, [document, documentList, location.documentId]);

  useEffect(() => { saveReaderTabs(readerTabs); }, [readerTabs]);

  const activeDocumentIdRef = useRef<string | null>(location.documentId);
  activeDocumentIdRef.current = location.documentId;
  const rightDocumentIdRef = useRef<string | null>(null);
  rightDocumentIdRef.current = splitReading && readingDocument ? rightDocumentId : null;

  function openDocument(documentId: string | null, preserveSplit = true) {
    activatePane("left");
    setDeskOpen(false);
    setMobilePanel(null);
    if (documentId && documentId === rightDocumentId && rightScales[documentId] !== undefined) {
      setReaderTabs(current => current.map(tab => tab.documentId === documentId ? { ...tab, scale: rightScales[documentId]! } : tab));
    }
    if (!documentId || !preserveSplit) { setSplitReading(false); setRightDocumentId(null); }
    else if (splitReading && rightDocumentId === documentId && documentId !== location.documentId) {
      if (location.documentId) {
        const leftId = location.documentId;
        const scale = readerTabs.find(tab => tab.documentId === leftId)?.scale ?? 1;
        setRightScales(current => ({ ...current, [leftId]: scale }));
      }
      setRightDocumentId(location.documentId);
    }
    if (documentId === location.documentId && !deskOpen) return;
    const next: ReaderLocation = { documentId, initialPage: documentId ? readingRecords[documentId]?.page ?? 1 : 1 };
    setLocation(next);
    updateReaderLocation(next);
    setMobilePanel(null);
  }

  function openSelectedDocuments() {
    if (!selectedIds.length) return;
    setReaderTabs(current => [...current, ...selectedIds.filter(id => !current.some(tab => tab.documentId === id)).map(documentId => ({
      documentId, title: documentList.find(item => item.document_id === documentId)?.filename ?? "文献", scale: 1,
    }))]);
    openDocument(selectedIds[0]!);
  }

  function studySelected() {
    setQuickOpen(false); setResearchIds(selectedIds); setSelectionRequest(n => n + 1);
    setFocusMode(false); setAssistantExpanded(true); setMobilePanel("assistant");
    setDraftQuestion("比较所选论文的研究问题、方法与结论，逐篇注明原文来源；未覆盖的内容请明确说明。");
  }

  function splitSelected() {
    const first = selectedIds[0], second = selectedIds[1];
    if (!first || !second) return;
    keepCurrentPage();
    setQuickOpen(false); openDocument(first, false);
    setRightDocumentId(second); setSplitReading(true);
    setReaderTabs(current => [...current, ...[first, second].filter(id => !current.some(tab => tab.documentId === id)).map(documentId => ({ documentId, title: documentList.find(d => d.document_id === documentId)?.filename ?? "文献", scale: 1 }))]);
  }

  function closeReaderTab(id: string) {
    const index = readerTabs.findIndex(tab => tab.documentId === id);
    if (index < 0) return;
    setClosedTab(readerTabs[index]!);
    const remaining = readerTabs.filter(tab => tab.documentId !== id);
    setReaderTabs(remaining);
    if (id === rightDocumentId) { setRightDocumentId(null); setTranslationSelection(null); }
    if (location.documentId === id) {
      openDocument(remaining[Math.min(index, remaining.length - 1)]?.documentId ?? null, false);
    }
  }

  const recordScale = useCallback((id: string, scale: number) => {
    if (activeDocumentIdRef.current !== id) return;
    setReaderTabs(current => current.map(tab => tab.documentId === id && tab.scale !== scale ? { ...tab, scale } : tab));
  }, []);

  function keepCurrentPage() {
    setLocation(current => ({ ...current, initialPage: current.documentId ? readingRecords[current.documentId]?.page ?? current.initialPage : 1 }));
    setRequestedPage(undefined); setCitationHighlight(null); setTranslationSelection(null);
  }

  function compareOnRight(id?: string) {
    keepCurrentPage();
    const next = id ?? readerTabs.find(tab => tab.documentId !== location.documentId)?.documentId
      ?? documentList.find(item => item.document_id !== location.documentId)?.document_id ?? null;
    setRightDocumentId(next === location.documentId ? null : next); setSplitReading(true); setDeskOpen(false);
    setReaderTabs(current => !next || current.some(tab => tab.documentId === next) ? current : [...current, {
      documentId: next, title: documentList.find(item => item.document_id === next)?.filename ?? "文献", scale: 1,
    }]);
  }

  const recordRightProgress = useCallback((id: string, page: number, totalPages: number) => {
    if (rightDocumentIdRef.current !== id) return;
    updateReading(id, current => ({ ...current, page, totalPages, lastOpened: Date.now(), readingStatus: current.readingStatus === "read" ? "read" : "reading" }));
  }, [updateReading]);
  const recordRightScale = useCallback((id: string, scale: number) => {
    if (rightDocumentIdRef.current === id) setRightScales(current => current[id] === scale ? current : { ...current, [id]: scale });
  }, []);
  const clearRightTranslation = useCallback(() => {
    setTranslationSelection(current => current?.documentId === rightDocumentIdRef.current ? null : current);
  }, []);
  const translateRight = useCallback((selection: SelectedText) => {
    if (selection.documentId === rightDocumentIdRef.current) setTranslationSelection(selection);
  }, []);
  const handleAutoSelectionLeft = useCallback((selection: SelectedText) => {
    if (autoTranslate && selection.translationEligible) setTranslationSelection(selection);
  }, [autoTranslate]);
  const handleAutoSelectionRight = useCallback((selection: SelectedText) => {
    if (autoTranslate && selection.translationEligible && selection.documentId === rightDocumentIdRef.current) {
      setTranslationSelection(selection);
    }
  }, [autoTranslate]);
  const toggleAutoTranslate = useCallback(() => {
    setAutoTranslate(current => {
      const next = !current;
      try { localStorage.setItem("lw-auto-translate", String(next)); } catch { /* Optional browser storage. */ }
      if (!next) setTranslationSelection(null);
      return next;
    });
  }, []);

  function showLiteratureDesk() {
    setLocation(current => ({ ...current, initialPage: current.documentId ? readingRecords[current.documentId]?.page ?? current.initialPage : 1 }));
    setDeskOpen(true);
  }

  const recordProgress = useCallback((id: string, page: number, totalPages: number) => {
    if (activeDocumentIdRef.current !== id) return;
    updateReading(id, current => ({ ...current, page, totalPages, lastOpened: Date.now(), readingStatus: current.readingStatus === "read" ? "read" : "reading" }));
    const params = new URLSearchParams(window.location.search);
    params.set("document", id);
    params.set("page", String(page));
    window.history.replaceState(null, "", `/?${params}`);
  }, [updateReading]);

  function toggleFavorite(id: string) {
    updateReading(id, current => ({ ...current, favorite: !current.favorite }));
  }

  function goHome() {
    openDocument(null);
  }

  function jumpToPage(page: number) {
    setPageRequestId(value => value + 1);
    setRequestedPage(page);
  }

  function jumpToAnnotation(annotation: PdfAnnotation, side: "left" | "right" = assistantOnRight ? "right" : "left") {
    activatePane(side);
    setAssistantTab("annotations");
    if (side === "right") {
      setRightAnnotationId(annotation.annotation_id);
      setRightJump({ documentId: rightDocumentId ?? undefined, page: annotation.page, rects: [], nonce: Date.now() });
      return;
    }
    setActiveAnnotationId(annotation.annotation_id);
    jumpToPage(annotation.page);
  }

  function jumpInPane(citation: ResearchCitation & { content_id?: string; source_changed?: boolean }, side: "left" | "right") {
    setMobilePanel(null);
    activatePane(side);
    const target = side === "right" ? right.document : document;
    if (!target) return;
    const changed = citation.source_changed || (!!citation.content_id && citation.content_id !== target.contentId);
    if (changed && !window.confirm("引用对应的原文已变化，仍然打开当前文件的对应页码吗？旧高亮不会套用。")) return;
    const jump = { documentId: target.documentId, page: citation.physical_page, rects: changed ? [] : citation.rects, nonce: Date.now() };
    if (side === "right") setRightJump(jump);
    else { setCitationHighlight(jump); jumpToPage(citation.physical_page); }
  }

  function jumpToCitation(citation: ResearchCitation) {
    jumpInPane(citation, assistantOnRight ? "right" : "left");
  }

  function askSelection(selection: SelectedText, side: "left" | "right") {
    const owner = side === "right" ? right.document : document;
    if (!owner || selection.documentId !== owner.documentId || selection.contentId !== owner.contentId || selection.documentVersionId !== owner.documentVersionId) return;
    activatePane(side); setFocusMode(false); setAssistantExpanded(true); setAssistantTab("ai"); setMobilePanel("assistant");
    setTranslationSelection(null);
    setSelectionDraft(current => ({ selection, nonce: (current?.nonce ?? 0) + 1 }));
  }

  function jumpToLibraryCitation(citation: LibraryCitation) {
    setDeskOpen(false);
    if (splitReading && readingDocument && right.document?.documentId === citation.document_id) {
      jumpInPane(citation, "right");
      return;
    }
    if (document?.documentId === citation.document_id) {
      jumpInPane(citation, "left");
      return;
    }
    // Citation points at a different document: record the destination and
    // navigate.  The effect below will apply the highlight and jump once the
    // reader finishes mounting the new document.
    setPendingCitation({
      documentId: citation.document_id,
      page: citation.physical_page,
      rects: citation.rects,
      expectedContentId: citation.content_id,
      sourceChanged: citation.source_changed,
    });
    openDocument(citation.document_id);
  }

  // Once the target document of a cross-document citation jump mounts, apply
  // the highlight and jump to the cited page.
  useEffect(() => {
    if (document === null || pendingCitation === null) return;
    if (pendingCitation.documentId !== document.documentId) return;
    const changed = pendingCitation.sourceChanged || (!!pendingCitation.expectedContentId && pendingCitation.expectedContentId !== document.contentId);
    if (changed && !window.confirm("引用对应的原文已变化，仍然定位当前页码吗？旧高亮不会套用。")) { setPendingCitation(null); return; }
    setCitationHighlight({
      documentId: document.documentId,
      page: pendingCitation.page,
      rects: changed ? [] : pendingCitation.rects,
      nonce: Date.now(),
    });
    jumpToPage(pendingCitation.page);
    setPendingCitation(null);
  }, [document, pendingCitation]);

  // Citation highlight auto-dismisses after 5 s; a new jump restarts it.
  useEffect(() => {
    if (citationHighlight === null) return undefined;
    const timer = window.setTimeout(() => setCitationHighlight(null), 5000);
    return () => window.clearTimeout(timer);
  }, [citationHighlight]);

  let reader: ReactNode;
  if (deskOpen) {
    reader = <LiteratureDesk studyRevision={studyRevision} onViewStudy={setStudyHistoryId} onOpenSelected={openSelectedDocuments} onStudy={() => setStudioIds(selectedIds)} documents={documentList} records={readingRecords} selected={selectedIds} onSelect={setSelectedIds} onOpen={openDocument} onUpdate={updateReading} onCompare={studySelected} />;
  } else if (location.documentId === null) {
    reader = (
      <DashboardHome
        documents={documentList}
        readingRecords={readingRecords}
        onAskQuestion={(q) => {
          setDraftQuestion(q);
          if (focusMode) setFocusMode(false);
          setAssistantExpanded(true);
          setMobilePanel("assistant");
        }}
        onOpenDocument={openDocument}
        onTriggerImport={() => {
          if (focusMode) setFocusMode(false);
          setLibraryExpanded(true);
          setMobilePanel("library");
          importInputRef.current?.click();
        }}
      />
    );
  } else if (documentError) {
    reader = <div className="workbench-error-wrap"><p role="alert">文献信息暂时无法读取</p><button className="button button--primary" onClick={left.retry}>重新加载文献</button><button className="button button--ghost" onClick={goHome}>返回首页</button></div>;
  } else if (document === null || document.documentId !== location.documentId || !readingReady) {
    reader = <div className="workbench-loading-wrap"><span className="workbench-loading-spinner" /><p>正在读取文献信息…</p></div>;
  } else {
    reader = (
      <>
        {left.notice && <p role="status">{left.notice}</p>}
        <Suspense fallback={<p role="status">正在加载阅读器…</p>}>
        <PdfReader
          key={document.documentId}
          {...document}
          onProgress={recordProgress}
          initialScale={readerTabs.find(tab => tab.documentId === document.documentId)?.scale ?? 1}
          onScaleChange={recordScale}
          activeAnnotationId={activeAnnotationId}
          annotationBusy={annotationBusy}
          annotations={annotations?.items ?? []}
          citationHighlight={citationHighlight}
          initialPage={new URLSearchParams(window.location.search).has("page") ? location.initialPage : readingRecords[document.documentId]?.page ?? location.initialPage}
          onAnnotateSelection={(selected, type, color, comment) => {
            if (selected.documentId === document.documentId) void runMutation(() => createAnnotation(selected, type, color, comment));
          }}
          onOpenAnnotation={(annotationId) => {
            const annotation = annotations?.items.find((item) => item.annotation_id === annotationId);
            if (annotation !== undefined) jumpToAnnotation(annotation, "left");
          }}
          onSelectionClear={clearTranslationSelection}
          onSelectionChange={handleAutoSelectionLeft}
          onDismissTranslation={clearTranslationSelection}
          onTranslateSelection={setTranslationSelection}
          onAskSelection={selected => askSelection(selected, "left")}
          pageRequestId={pageRequestId}
          requestedPage={requestedPage}
        />
        </Suspense>
      </>
    );
  }

  return (
    <div className="workbench-shell" data-theme={resolvedTheme}>
      <header className="topbar">
        <div className="topbar__left">
          <button
            aria-label="返回文献工作台首页"
            className="topbar__brand-button"
            onClick={goHome}
            type="button"
          >
            <span aria-hidden="true" className="topbar__mark">📖</span>
            <strong>Literature Workbench</strong>
          </button>
          <nav className="topbar__nav" aria-label="工作区导航">
            <button
              type="button"
              className={`topbar__nav-btn${!deskOpen ? " topbar__nav-btn--active" : ""}`}
              aria-pressed={!deskOpen}
              onClick={() => { if (deskOpen && location.documentId) setDeskOpen(false); else if (!location.documentId) goHome(); }}
            >
              {location.documentId ? "阅读" : "首页"}
            </button>
            <button
              type="button"
              className={`topbar__nav-btn${deskOpen ? " topbar__nav-btn--active" : ""}`}
              aria-pressed={deskOpen}
              onClick={showLiteratureDesk}
            >
              文献总览
            </button>
            <button
              type="button"
              className="topbar__nav-btn"
              onClick={() => setNotebook({})}
            >
              研究记录
            </button>
            <button
              type="button"
              className="topbar__nav-btn"
              onClick={() => setStudioIds(selectedIds.length ? selectedIds : location.documentId ? [location.documentId] : [])}
            >
              联读与图谱
            </button>
          </nav>
        </div>

        <div className="topbar__document" title={currentDocument?.filename ?? "尚未选择文献"}>
          {currentDocument ? (
            <div className="topbar__doc-badge">
              <span className="topbar__doc-tag">PDF</span>
              <span className="topbar__doc-name">{currentDocument.filename}</span>
              <button className="favorite-button" type="button" aria-label={readingRecords[currentDocument.documentId]?.favorite ? "取消收藏当前文献" : "收藏当前文献"} aria-pressed={readingRecords[currentDocument.documentId]?.favorite ?? false} onClick={() => toggleFavorite(currentDocument.documentId)}>{readingRecords[currentDocument.documentId]?.favorite ? "★" : "☆"}</button>
            </div>
          ) : null}
        </div>

        <div className="topbar__actions">
          <button className="topbar__button topbar__search-btn" type="button" aria-label="快速打开文献与功能" title="快速打开（Ctrl / ⌘ K）" onClick={() => setQuickOpen(true)}>⌕<kbd>Ctrl K</kbd></button>
          <button
            aria-expanded={compact ? mobilePanel === "library" : libraryVisible}
            className="topbar__button mobile-only"
            onClick={() => {
              setFocusMode(false);
              if (compact) setMobilePanel(value => value === "library" ? null : "library");
              else setLibraryExpanded(!libraryVisible);
            }}
            type="button"
          >
            文献库
          </button>
          <button
            aria-expanded={compact ? mobilePanel === "assistant" : assistantVisible}
            aria-label="助手与笔记"
            className="topbar__button mobile-only"
            onClick={() => {
              setFocusMode(false);
              if (compact) setMobilePanel(value => value === "assistant" ? null : "assistant");
              else setAssistantExpanded(!assistantVisible);
            }}
            type="button"
          >
            助手
          </button>
          <DeepSeekBilling />
          <button
            aria-pressed={focusMode}
            className={`topbar__button topbar__button--focus ${focusMode ? "topbar__button--active" : ""}`}
            aria-label={focusMode ? "退出专注" : "专注阅读"}
            onClick={() => setFocusMode((value) => !value)}
            type="button"
          >
            {focusMode ? "退出专注" : "专注"}
          </button>
          <button
            aria-label={`切换为${resolvedTheme === "dark" ? "浅色" : "深色"}主题`}
            className="icon-button"
            onClick={() => {
              const next = resolvedTheme === "dark" ? "light" : "dark";
              try { localStorage.setItem("lw-theme", next); } catch { /* Theme still works for this visit. */ }
              setTheme(next);
            }}
            title={resolvedTheme === "dark" ? "切换为浅色主题" : "切换为深色主题"}
            type="button"
          >
            {resolvedTheme === "dark" ? "☀" : "◐"}
          </button>
          <button
            aria-label="打开设置"
            className="icon-button"
            onClick={() => setSettingsOpen(true)}
            title="设置、术语表与数据备份"
            type="button"
          >
            ⚙
          </button>
        </div>
      </header>

      <div
        className={`workbench${readingDocument ? " workbench--reading" : ""}${focusMode ? " workbench--focus" : ""}${!showLibraryPane ? " workbench--library-hidden" : ""}${!assistantVisible ? " workbench--assistant-hidden" : ""}${mobilePanel === "library" ? " workbench--show-library" : ""}${mobilePanel === "assistant" ? " workbench--show-assistant" : ""}`}
        style={{ "--library-width": `${sidebar.libraryWidth}px`, "--assistant-width": `${sidebar.assistantWidth}px` } as CSSProperties}
      >
        {mobilePanel !== null && (
          <button
            aria-label="关闭侧栏"
            className="workbench__scrim"
            onClick={() => setMobilePanel(null)}
            type="button"
          />
        )}
        <aside ref={libraryAside} inert={compact && mobilePanel !== "library"} aria-label="文献库" role={compact && mobilePanel === "library" ? "dialog" : undefined} aria-modal={compact && mobilePanel === "library" ? true : undefined} className="workbench__library">
          <button className="button button--ghost mobile-only drawer-close" type="button" onClick={() => setMobilePanel(null)}>关闭文献库</button>
          <LibraryPanel
            fileInputRef={importInputRef}
            readingRecords={readingRecords}
            onToggleFavorite={toggleFavorite}
            activeDocumentId={location.documentId}
            onDocumentsLoaded={setDocumentList}
            onOpenDocument={openDocument}
          />
        </aside>
        <SidebarHandle side="library" expanded={libraryVisible} width={sidebar.libraryWidth} min={200} max={sidebar.libraryMax}
          onToggle={() => { setFocusMode(false); setLibraryExpanded(!libraryVisible); }}
          onResize={width => sidebar.resize("library", width)} onReset={() => sidebar.resize("library", 240)} />
        <main inert={compact && mobilePanel !== null} aria-label="PDF 阅读器" className="workbench__reader">
          {recoveredWorkspace && <div className="recovery-workspace-banner" role="status" title={recoveredWorkspace}>恢复工作台 · 当前编辑保存在独立副本中 <button type="button" onClick={() => setDataCenterOpen(true)}>查看位置</button></div>}
          {launchAddress && addressNotice(launchAddress) && <div className="launch-address-notice" role="note"><span>{addressNotice(launchAddress)}</span><button type="button" onClick={() => setDataCenterOpen(true)}>查看地址</button></div>}
          {(readerTabs.length > 0 || readingDocument) && (
            <div className="reader-bar">
              <ReaderTabs tabs={readerTabs} activeId={deskOpen ? null : location.documentId} pages={readingRecords}
                onCompare={readingDocument ? compareOnRight : undefined}
                canReopen={closedTab !== null} onOpen={openDocument} onClose={closeReaderTab} onAdd={showLiteratureDesk}
                onReopen={() => {
                  if (!closedTab) return;
                  setReaderTabs(current => current.some(tab => tab.documentId === closedTab.documentId) ? current : [...current, closedTab]);
                  openDocument(closedTab.documentId); setClosedTab(null);
                }} />
              {readingDocument && (
                <div className="reader-bar__tools">
                  <button
                    type="button"
                    aria-pressed={autoTranslate}
                    className={`workspace-nav__switch${autoTranslate ? " workspace-nav__switch--on" : ""}`}
                    onClick={toggleAutoTranslate}
                    title="开启后，在 PDF 中选中文本会自动弹出语境学术翻译卡片"
                  >
                    <span>划词即译</span>
                    <span aria-hidden="true" className="workspace-nav__switch-track"><span className="workspace-nav__switch-thumb" /></span>
                  </button>
                  <button
                    type="button"
                    className={`reader-bar__split-btn${splitReading ? " reader-bar__split-btn--active" : ""}`}
                    aria-pressed={splitReading}
                    onClick={() => {
                      if (splitReading) { keepCurrentPage(); setSplitReading(false); activatePane("left"); }
                      else compareOnRight();
                    }}
                  >
                    {splitReading ? "退出并排" : "并排"}
                  </button>
                </div>
              )}
            </div>
          )}
          <div className="reader-tab-panel" id="reader-tab-panel" role={!deskOpen && location.documentId ? "tabpanel" : undefined}
            aria-labelledby={!deskOpen && location.documentId ? `reader-tab-${location.documentId}` : undefined}>
            {splitReading && readingDocument ? <ResizableReadingSplit
              activeSide={assistantOnRight ? "right" : "left"} onActivate={activatePane}
              left={<><div className="reading-split__heading"><span>{assistantOnRight ? "左侧 · 点击切换助手" : "左侧 · 正在研读"}</span></div>{reader}</>}
              right={<>
                <div className="reading-split__heading">
                  <span>{assistantOnRight ? "右侧 · 正在研读" : "右侧"}</span>
                  <select aria-label="选择右侧对照文献" value={rightDocumentId ?? ""} onChange={event => compareOnRight(event.target.value)}>
                    <option value="" disabled>选择右侧文献</option>
                    {[...new Map([...readerTabs.map(tab => [tab.documentId, tab.title] as const), ...documentList.map(item => [item.document_id, item.filename] as const)]).entries()]
                      .filter(([id]) => id !== location.documentId).map(([id, title]) => <option key={id} value={id}>{title}</option>)}
                  </select>
                  <button type="button" disabled={!rightDocumentId} onClick={() => { if (rightDocumentId) openDocument(rightDocumentId); }}>交换左右</button>
                  <button type="button" disabled={!rightDocumentId} onClick={() => { if (rightDocumentId) openDocument(rightDocumentId, false); }} title="将右侧文献切回单篇阅读">单篇研读</button>
                </div>
                {rightDocumentId ? <ComparisonPane key={rightDocumentId} state={right}
                  jump={rightJump} activeAnnotationId={rightAnnotationId}
                  onOpenAnnotation={id => { const item = right.annotations?.items.find(a => a.annotation_id === id); if (item) jumpToAnnotation(item, "right"); }}
                  initialPage={readingRecords[rightDocumentId]?.page ?? 1} initialScale={rightScales[rightDocumentId]}
                  onProgress={recordRightProgress} onScaleChange={recordRightScale} onSelectionChange={handleAutoSelectionRight} onDismissTranslation={clearRightTranslation} onTranslate={translateRight} onAskSelection={selected => askSelection(selected, "right")} onClear={clearRightTranslation} />
                  : <p className="studio-empty">从上方选择第二篇论文，或点击其它标签的 ⇥ 并排阅读。</p>}
              </>} /> : reader}
          </div>
        </main>
        <SidebarHandle side="assistant" expanded={assistantVisible} width={sidebar.assistantWidth} min={260} max={sidebar.assistantMax}
          onToggle={() => { setFocusMode(false); setAssistantExpanded(!assistantVisible); }}
          onResize={width => sidebar.resize("assistant", width)} onReset={() => sidebar.resize("assistant", 300)} />
        <aside ref={assistantAside} inert={compact && mobilePanel !== "assistant"} aria-label="研究助手" role={compact && mobilePanel === "assistant" ? "dialog" : undefined} aria-modal={compact && mobilePanel === "assistant" ? true : undefined} className="workbench__assistant">
          <button className="button button--ghost mobile-only drawer-close" type="button" onClick={() => setMobilePanel(null)}>关闭研究助手</button>
          <div className="assistant-dock-tabs" role="group" aria-label="右侧栏切换">
            <button
              type="button"
              aria-pressed={assistantTab === "ai"}
              className={`assistant-dock-tab${assistantTab === "ai" ? " assistant-dock-tab--active" : ""}`}
              onClick={() => setAssistantTab("ai")}
            >
              AI 助手
            </button>
            <button
              type="button"
              aria-pressed={assistantTab === "annotations"}
              className={`assistant-dock-tab${assistantTab === "annotations" ? " assistant-dock-tab--active" : ""}`}
              onClick={() => setAssistantTab("annotations")}
            >
              批注与笔记
              <span className="assistant-dock-tab__count">{current.annotations?.items.length ?? 0}</span>
            </button>
          </div>
          {splitReading && readingDocument && <p className="assistant-document-context" role="status">
            助手与笔记跟随{assistantOnRight ? "右侧" : "左侧"}：{currentDocument?.filename ?? "正在读取文献…"}
          </p>}
          <div className={`workbench__assistant-pane${assistantTab !== "ai" ? " workbench__assistant-pane--hidden" : ""}`}>
            <AssistantPanel
              studyDraft={studyDraft} onStudySaved={() => setStudyRevision(n => n + 1)}
              document={currentDocument}
              documentPending={readingDocument && currentDocument === null}
              selectedDocuments={researchIds.map(id => ({ document_id: id, filename: documentList.find(d => d.document_id === id)?.filename ?? "已不可用的文献" }))}
              selectionRequest={selectionRequest}
              onOpenNotebook={entryId => { setMobilePanel(null); setNotebook({ entryId }); }}
              draftQuestion={draftQuestion} selectionDraft={selectionDraft}
              onDraftQuestionConsumed={() => setDraftQuestion("")}
              onJumpToCitation={jumpToCitation}
              onJumpToLibraryCitation={jumpToLibraryCitation}
              onOpenSettings={() => setSettingsOpen(true)}
            />
          </div>
          <div className={`workbench__assistant-pane${assistantTab !== "annotations" ? " workbench__assistant-pane--hidden" : ""}`}>
            <AnnotationPanel
              busy={current.busy}
              document={currentDocument}
              error={current.error}
              onDelete={annotation => {
                if (currentDocument) setConfirmation({ kind: "delete", annotation, document: currentDocument, side: assistantOnRight ? "right" : "left" });
              }}
              onJump={annotation => jumpToAnnotation(annotation)}
              onRestore={() => {
                if (currentDocument) setConfirmation({ kind: "restore", document: currentDocument, side: assistantOnRight ? "right" : "left" });
              }}
              onUndo={() => { if (currentDocument) void current.runMutation(() => undoAnnotation(currentDocument.documentId)); }}
              onUpdate={(annotation, changes) => {
                if (currentDocument) void current.runMutation(() => updateAnnotation(currentDocument, annotation.annotation_id, changes));
              }}
              state={current.annotations}
            />
          </div>
        </aside>
      </div>
      {readingDocument && translationSelection !== null && <TranslationPopover onClose={() => setTranslationSelection(null)} selected={translationSelection} />}
      {dataCenterOpen && <DataCenter onClose={() => setDataCenterOpen(false)} />}
      {quickOpen && <QuickOpen documents={documentList} records={readingRecords} openIds={readerTabs.map(tab => tab.documentId)} selected={selectedIds}
        onDocumentsLoaded={setDocumentList} onSelect={setSelectedIds} onClose={() => setQuickOpen(false)} onCompare={studySelected} onSplit={splitSelected}
        onOpen={id => { setQuickOpen(false); openDocument(id); }} onCache={id => { setQuickOpen(false); setStudyHistoryId(id); }}
        commands={[
          { id: "library", title: "文献总览", description: "查找、筛选、整理与选择多篇文献", keywords: "文献库 标签 收藏", run: showLiteratureDesk },
          { id: "notes", title: "研究记录", description: "查看已保存的回答、个人笔记与原文来源", keywords: "笔记 历史", run: () => setNotebook({}) },
          { id: "studio", title: "联读与图谱", description: "双栏对照、证据矩阵、研究问题与关系审核", keywords: "比较 知识图谱 AI", run: () => setStudioIds(selectedIds.length ? selectedIds : location.documentId ? [location.documentId] : []) },
          { id: "backup", title: "数据与备份", description: "查看本地保存位置，创建或核验备份", keywords: "恢复 导出", run: () => setDataCenterOpen(true) },
          { id: "settings", title: "模型设置", description: "查看模型、密钥配置与隐私选项", run: () => setSettingsOpen(true) },
          { id: "focus", title: focusMode ? "退出专注阅读" : "专注阅读", description: "展开阅读区域或恢复侧栏布局", run: () => setFocusMode(value => !value) },
        ] satisfies QuickCommand[]} />}
      {studyHistoryId && <StudyHistory key={studyHistoryId} documentId={studyHistoryId} onReview={result => { setStudyHistoryId(null); setStudyReview(result); }} onFindNotes={query => { setStudyHistoryId(null); setNotebook({ query }); }} onClose={() => setStudyHistoryId(null)} onJump={citation => { setStudyHistoryId(null); jumpToLibraryCitation(citation); }} onReread={id => {
        setStudyHistoryId(null); setResearchIds([id]); setSelectionRequest(n => n + 1);
        setDraftQuestion(""); setStudyDraft(current => ({ documentId: id, nonce: (current?.nonce ?? 0) + 1 }));
        setFocusMode(false); setAssistantExpanded(true); setMobilePanel("assistant");
      }} />}
      {storageFailed && <p className="storage-notice" role="status">阅读状态尚未同步到本机数据库，请保持页面打开后重试。<button type="button" onClick={retryReading}>重试同步</button></p>}
      {(studioIds || studyReview) && <ResearchStudio key={studyReview?.id ?? "studio"} initialAnalysisId={studyReview?.id} documents={documentList} initialIds={studyReview?.document_ids ?? studioIds ?? []} records={readingRecords} onUpdate={updateReading} onSolo={(id, page) => { setStudioIds(null); setStudyReview(null); jumpToLibraryCitation({ document_id: id, display_name: "", physical_page: page, excerpt: "", rects: [] }); }} onClose={() => { setStudioIds(null); setStudyReview(null); }} onAsk={(ids, question) => {
        setStudioIds(null); setStudyReview(null); setResearchIds(ids); setSelectedIds(ids); setSelectionRequest(n => n + 1); setDraftQuestion(question); setFocusMode(false); setAssistantExpanded(true); setMobilePanel("assistant");
      }} />}
      {notebook && <ResearchNotebook initialEntryId={notebook.entryId} initialQuery={notebook.query} onClose={() => setNotebook(null)} onJump={jumpToLibraryCitation} />}
      {settingsOpen && <SettingsDialog onClose={() => setSettingsOpen(false)} onOpenDataCenter={() => setDataCenterOpen(true)} />}
      {confirmation?.kind === "delete" && (
        <ConfirmDialog
          confirmLabel="删除批注"
          description="这会从原 PDF 中删除该条本站批注，可以使用撤销上次恢复。"
          label="删除批注"
          onCancel={() => setConfirmation(null)}
          onConfirm={() => {
            const { annotation, document: target, side } = confirmation;
            const owner = side === "right" ? right : left;
            setConfirmation(null);
            if (owner.document?.documentId === target.documentId) void owner.runMutation(() => deleteAnnotation(target, annotation.annotation_id));
          }}
        />
      )}
      {confirmation?.kind === "restore" && (
        <ConfirmDialog
          confirmLabel="恢复原始 PDF"
          description={`此操作会移除本站写入的全部批注。请输入 ${confirmation.document.filename} 继续。`}
          expectedText={confirmation.document.filename}
          label="恢复原始 PDF"
          onCancel={() => setConfirmation(null)}
          onConfirm={() => {
            setConfirmation(null);
            const { document: target, side } = confirmation;
            const owner = side === "right" ? right : left;
            if (owner.document?.documentId === target.documentId) void owner.runMutation(() => restoreAnnotationBaseline(target.documentId, target.filename));
          }}
        />
      )}
    </div>
  );
}
