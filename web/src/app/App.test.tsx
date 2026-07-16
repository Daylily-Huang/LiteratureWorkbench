import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../features/library/LibraryPanel", () => ({
  LibraryPanel: ({ activeDocumentId, onOpenDocument }: {
    activeDocumentId?: string | null;
    onOpenDocument?: (documentId: string) => void;
  }) => (
    <button onClick={() => onOpenDocument?.("document-next")} type="button">
      打开测试文献 {activeDocumentId ?? "none"}
    </button>
  ),
}));

vi.mock("../features/reader/PdfReader", () => ({
  PdfReader: ({ documentId, onTranslateSelection }: {
    documentId: string;
    onTranslateSelection?: (selected: unknown) => void;
  }) => (
    <>
      <p>阅读 {documentId}</p>
      <button
        onClick={() => onTranslateSelection?.({
          documentId,
          documentVersionId: `version-${documentId}`,
          contentId: "a".repeat(64),
          pdfPage: 1,
          text: `selection-${documentId}`,
          contextBefore: "",
          contextAfter: "",
          rects: [],
          pdfQuads: [],
          buttonAnchor: { x: 0, y: 0 },
          viewportAnchor: { x: 100, y: 100 },
          translationEligible: true,
        })}
        type="button"
      >
        选择测试文字
      </button>
    </>
  ),
}));

vi.mock("../features/translation/TranslationPopover", () => ({
  TranslationPopover: ({ selected }: { selected: { text: string } }) => (
    <section aria-label="选区翻译">{selected.text}</section>
  ),
}));

import { App } from "./App";

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string | URL | Request) => {
    const url = String(input);
    if (url.endsWith("/annotations")) {
      const documentId = url.split("/").at(-2) ?? "document-next";
      return Promise.resolve(new Response(JSON.stringify({
        items: [],
        document_id: documentId,
        current_document_version_id: `version-${documentId}`,
        content_id: "a".repeat(64),
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    }
    const documentId = url.split("/").at(-1) ?? "document-next";
    return Promise.resolve(new Response(JSON.stringify({
      document_id: documentId,
      current_document_version_id: `version-${documentId}`,
      content_id: "a".repeat(64),
      filename: `${documentId}.pdf`,
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("renders the approved three-column workbench", () => {
    render(<App />);
    expect(screen.getByRole("complementary", { name: "文献库" })).toBeVisible();
    expect(screen.getByRole("main", { name: "PDF 阅读器" })).toBeVisible();
    expect(screen.getByRole("complementary", { name: "研究助手" })).toBeVisible();
  });

  it("opens a selected document without reload and follows browser history", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("button", { name: /打开测试文献 none/ }));
    await screen.findByText("阅读 document-next");
    expect(window.location.search).toBe("?document=document-next&page=1");

    window.history.pushState({}, "", "/?document=document-history&page=3");
    window.dispatchEvent(new PopStateEvent("popstate"));
    await waitFor(() => expect(screen.getByText("阅读 document-history")).toBeVisible());
  });

  it("clears a translation selection when another document opens", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/?document=document-current&page=1");
    render(<App />);

    await screen.findByText("阅读 document-current");
    await user.click(screen.getByRole("button", { name: "选择测试文字" }));
    expect(screen.getByRole("region", { name: "选区翻译" })).toHaveTextContent(
      "selection-document-current",
    );

    await user.click(screen.getByRole("button", { name: /打开测试文献 document-current/ }));
    await screen.findByText("阅读 document-next");
    expect(screen.queryByRole("region", { name: "选区翻译" })).not.toBeInTheDocument();
  });
});
