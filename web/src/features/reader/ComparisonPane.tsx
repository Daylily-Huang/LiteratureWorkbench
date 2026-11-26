import { lazy, Suspense, useEffect, useState } from "react";
import { createAnnotation } from "../annotations/annotations";
import type { CitationJump } from "./PdfReader";
import type { AnnotatedDocument } from "./useAnnotatedDocument";
import type { SelectedText } from "./selection";
const PdfReader = lazy(() => import("./PdfReader").then(module => ({ default: module.PdfReader })));

export function ComparisonPane({ state, jump, activeAnnotationId, onOpenAnnotation, initialPage, initialScale, onProgress, onScaleChange, onSelectionChange, onDismissTranslation, onTranslate, onAskSelection, onClear }: {
  state: AnnotatedDocument; jump?: CitationJump | null; activeAnnotationId?: string | null;
  onOpenAnnotation?(id: string): void;
  initialPage: number; initialScale?: number;
  onProgress(id: string, page: number, total: number): void;
  onScaleChange(id: string, scale: number): void;
  onSelectionChange?(selection: SelectedText): void;
  onDismissTranslation?(): void;
  onTranslate(selection: SelectedText): void;
  onAskSelection?(selection: SelectedText): void;
  onClear(): void;
}) {
  const { document, documentError, retry, annotations, busy, runMutation } = state;
  const [highlight, setHighlight] = useState<CitationJump | null>(null);
  useEffect(() => {
    setHighlight(jump ?? null);
    const timer = window.setTimeout(() => setHighlight(null), 5000);
    return () => window.clearTimeout(timer);
  }, [jump]);
  if (documentError) return <div className="workbench-error-wrap" role="alert"><p>右侧原文暂时无法读取</p><button type="button" onClick={retry}>重试右侧文献</button></div>;
  if (!document) return <p role="status">正在读取右侧文献…</p>;
  return <>{state.notice && <p role="status">{state.notice}</p>}<Suspense fallback={<p>正在加载阅读器…</p>}><PdfReader key={document.documentId} {...document}
    annotations={annotations?.items ?? []} annotationBusy={busy} activeAnnotationId={activeAnnotationId}
    requestedPage={jump?.page} pageRequestId={jump?.nonce} citationHighlight={highlight} onOpenAnnotation={onOpenAnnotation}
    onAnnotateSelection={(selected, type, color, comment) => {
      if (selected.documentId === document.documentId) void runMutation(() => createAnnotation(selected, type, color, comment));
    }}
    initialPage={initialPage} initialScale={initialScale} fitOnOpen={initialScale === undefined}
    onProgress={onProgress} onScaleChange={onScaleChange} onSelectionChange={onSelectionChange} onDismissTranslation={onDismissTranslation} onTranslateSelection={onTranslate} onAskSelection={onAskSelection} onSelectionClear={onClear} /></Suspense></>;
}
