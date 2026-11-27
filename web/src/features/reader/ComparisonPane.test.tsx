import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useAnnotatedDocument } from "./useAnnotatedDocument";
import { ComparisonPane } from "./ComparisonPane";
vi.mock("./PdfReader", () => ({ PdfReader: ({ documentId }: { documentId: string }) => <div data-testid="comparison-pdf">{documentId}</div> }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
function Pane({ documentId, initialPage }: {documentId: string; initialPage: number}) {
  const state = useAnnotatedDocument(documentId);
  return <ComparisonPane state={state} initialPage={initialPage} {...callbacks} />;
}
const callbacks = { onProgress: vi.fn(), onScaleChange: vi.fn(), onTranslate: vi.fn(), onClear: vi.fn() };
test("a late right-side response cannot replace the newly selected paper", async () => {
  let resolve!: (value: unknown) => void;
  const old = new Promise(r => { resolve = r; });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => ({ ok: true, json: () => url.endsWith("a") ? old : Promise.resolve({document_id:"b",current_document_version_id:"v-b",content_id:"b",filename:"B.pdf"}) })));
  const view = render(<Pane documentId="a" initialPage={3} />);
  view.rerender(<Pane documentId="b" initialPage={5} />);
  await waitFor(() => expect(screen.getByTestId("comparison-pdf")).toHaveTextContent("b"));
  await act(async () => { resolve({document_id:"a",current_document_version_id:"v-a",content_id:"a",filename:"A.pdf"}); });
  expect(screen.getByTestId("comparison-pdf")).toHaveTextContent("b");
});
test("rejects metadata belonging to another paper", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ok:true,json:async()=>({document_id:"wrong",current_document_version_id:"v",content_id:"x",filename:"wrong.pdf"})})));
  render(<Pane documentId="a" initialPage={1} />);
  await screen.findByText("右侧原文暂时无法读取");
  expect(screen.queryByTestId("comparison-pdf")).not.toBeInTheDocument();
});
