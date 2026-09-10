import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Workbench } from "./Workbench";

vi.mock("../features/library/LibraryPanel", () => ({
  LibraryPanel: ({ onOpenDocument }: { onOpenDocument: (id: string) => void }) => (
    <div>
      <button onClick={() => onOpenDocument("doc-b")}>Open B</button>
      <button onClick={() => onOpenDocument("doc-c")}>Open C</button>
    </div>
  ),
}));

vi.mock("../features/reader/PdfReader", () => ({
  PdfReader: ({ documentId, citationHighlight }: { documentId: string; citationHighlight: unknown }) => (
    <div data-testid="reader">{JSON.stringify({ documentId, citationHighlight })}</div>
  ),
}));

vi.mock("../features/settings/DeepSeekBilling", () => ({ DeepSeekBilling: () => null }));
vi.mock("../features/annotations/AnnotationPanel", () => ({ AnnotationPanel: () => null }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function setup() {
  window.history.replaceState({}, "", "/?document=doc-a");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      let body: unknown;
      if (url === "/api/session/current") {
        body = { csrf_token: "test" };
      } else if (url.endsWith("/ask")) {
        body = {
          answer: "Review evidence answer",
          provider: "qwen",
          model: "local",
          fallback_reason: null,
          indexed_documents: 2,
          citation_status: "verified",
          citations: [
            {
              document_id: url.includes("/libraries/") ? "doc-b" : undefined,
              display_name: "B.pdf",
              physical_page: 1,
              excerpt: "source",
              rects: [[10, 20, 300, 80]],
            },
          ],
        };
      } else if (url.endsWith("/annotations")) {
        body = { items: [] };
      } else {
        const id = url.split("/").at(-1);
        body = {
          document_id: id,
          current_document_version_id: `version-${id}`,
          content_id: "a".repeat(64),
          filename: `${id}.pdf`,
        };
      }
      return new Response(JSON.stringify(body), { status: 200 });
    }),
  );
  render(<Workbench />);
}

describe("Workbench cross-document citation and highlight tests (R3 & R4)", () => {
  it("preserves library answer when cross-document citation is clicked and opened", async () => {
    setup();
    await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent("doc-a"));

    // Switch to library scope and ask
    fireEvent.click(screen.getByRole("button", { name: "整个文献库" }));
    fireEvent.change(screen.getByLabelText("向文献库提问"), { target: { value: "alpha" } });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    await screen.findByText("Review evidence answer");
    const citationBtn = screen.getByRole("button", { name: "《B.pdf》第 1 页" });
    expect(citationBtn).toBeInTheDocument();

    // Click citation to open Doc B
    fireEvent.click(citationBtn);
    await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent("doc-b"));

    // R3 regression check: The library answer and its citations must remain visible!
    expect(screen.getByText("Review evidence answer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "《B.pdf》第 1 页" })).toBeInTheDocument();
  });

  it("clears citation highlight when manually switching to an unrelated document", async () => {
    setup();
    await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent("doc-a"));

    // Ask single document question and trigger citation highlight on Doc A
    fireEvent.change(screen.getByLabelText("向当前文献提问"), { target: { value: "alpha" } });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));
    await screen.findByText("Review evidence answer");

    fireEvent.click(screen.getByRole("button", { name: "第 1 页" }));
    expect(screen.getByTestId("reader")).toHaveTextContent("[10,20,300,80]");

    // Manually switch to Doc B
    fireEvent.click(screen.getByRole("button", { name: "Open B" }));
    await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent("doc-b"));

    // R4 regression check: Doc B must NOT receive Doc A's highlight coordinates!
    expect(screen.getByTestId("reader")).not.toHaveTextContent("[10,20,300,80]");
  });

  it("applies citation highlight with matching documentId upon cross-document citation jump", async () => {
    setup();
    await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent("doc-a"));

    fireEvent.click(screen.getByRole("button", { name: "整个文献库" }));
    fireEvent.change(screen.getByLabelText("向文献库提问"), { target: { value: "alpha" } });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    await screen.findByText("Review evidence answer");
    fireEvent.click(screen.getByRole("button", { name: "《B.pdf》第 1 页" }));

    await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent("doc-b"));
    // Highlight is applied to Doc B with documentId matching doc-b
    await waitFor(() => {
      expect(screen.getByTestId("reader")).toHaveTextContent('"documentId":"doc-b"');
      expect(screen.getByTestId("reader")).toHaveTextContent("[10,20,300,80]");
    });
  });
});
