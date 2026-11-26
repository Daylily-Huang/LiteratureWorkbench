import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
  PdfReader: ({ documentId, citationHighlight, initialPage, initialScale, onProgress, onScaleChange }: {
    documentId: string; citationHighlight: unknown; initialPage: number; initialScale: number;
    onProgress(id: string, page: number, total: number): void; onScaleChange(id: string, scale: number): void;
  }) => (
    <div data-testid="reader">{JSON.stringify({ documentId, citationHighlight, initialPage, initialScale })}
      <button onClick={() => onProgress(documentId, 7, 20)}>Read page 7</button>
      <button onClick={() => onScaleChange(documentId, 1.5)}>Zoom 150</button>
    </div>
  ),
}));

vi.mock("../features/settings/DeepSeekBilling", () => ({ DeepSeekBilling: () => null }));
vi.mock("../features/annotations/AnnotationPanel", () => ({
  AnnotationPanel: ({ state }: { state: unknown }) => <div data-testid="annotations">{JSON.stringify(state)}</div>,
}));

beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("multi-document reader tabs", () => {
  it("compares two tabs, keeps pages and zoom on swap, then returns the right paper to single reading", async () => {
    setup();
    await screen.findByRole("tab", { name: "doc-a.pdf" });
    fireEvent.click(screen.getByText("Open B"));
    await screen.findByRole("tab", { name: "doc-b.pdf" });
    fireEvent.click(screen.getByRole("button", { name: "右侧对照 doc-a.pdf" }));
    const left = within(screen.getByRole("region", { name: "左侧文献" }));
    const right = within(screen.getByRole("region", { name: "右侧文献" }));
    await waitFor(() => expect(right.getByTestId("reader")).toHaveTextContent('"documentId":"doc-a"'));
    fireEvent.click(left.getByText("Read page 7"));
    fireEvent.click(right.getByText("Zoom 150"));
    fireEvent.click(screen.getByRole("button", { name: "交换左右" }));
    await waitFor(() => expect(left.getByTestId("reader")).toHaveTextContent('"documentId":"doc-a"'));
    expect(left.getByTestId("reader")).toHaveTextContent('"initialScale":1.5');
    await waitFor(() => expect(right.getByTestId("reader")).toHaveTextContent('"documentId":"doc-b"'));
    expect(right.getByTestId("reader")).toHaveTextContent('"initialPage":7');
    fireEvent.click(screen.getByRole("button", { name: "单篇研读" }));
    await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent('"documentId":"doc-b"'));
    expect(screen.getByTestId("reader")).toHaveTextContent('"initialPage":7');
    expect(screen.queryByRole("separator", { name: "调整两篇文献宽度" })).not.toBeInTheDocument();
  });
  it("keeps separate pages and zoom, deduplicates opens, and closes only the chosen tab", async () => {
    setup();
    await screen.findByRole("tab", { name: "doc-a.pdf" });
    fireEvent.click(screen.getByText("Read page 7"));
    fireEvent.click(screen.getByText("Zoom 150"));
    fireEvent.click(screen.getByText("Open B"));
    await screen.findByRole("tab", { name: "doc-b.pdf" });
    expect(screen.getByTestId("reader")).toHaveTextContent('"initialPage":1');
    expect(screen.getByTestId("reader")).toHaveTextContent('"initialScale":1');
    fireEvent.click(screen.getByRole("tab", { name: "doc-a.pdf" }));
    await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent('"documentId":"doc-a"'));
    expect(screen.getByTestId("reader")).toHaveTextContent('"initialPage":7');
    expect(screen.getByTestId("reader")).toHaveTextContent('"initialScale":1.5');
    fireEvent.click(screen.getByRole("tab", { name: "doc-a.pdf" }));
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "关闭标签 doc-b.pdf" }));
    expect(screen.getByTestId("reader")).toHaveTextContent('"documentId":"doc-a"');
    expect(screen.getAllByRole("tab")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "恢复关闭的标签" }));
    await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent('"documentId":"doc-b"'));
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "关闭标签 doc-b.pdf" }));
    await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent('"documentId":"doc-a"'));
    fireEvent.click(screen.getByRole("button", { name: "关闭标签 doc-a.pdf" }));
    expect(screen.queryByTestId("reader")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
    expect(new URLSearchParams(location.search).has("document")).toBe(false);
  });

  it("restores tabs after a refresh and keeps them through the library overview", async () => {
    setup();
    await screen.findByRole("tab", { name: "doc-a.pdf" });
    fireEvent.click(screen.getByText("Open B"));
    await screen.findByRole("tab", { name: "doc-b.pdf" });
    cleanup();
    render(<Workbench />);
    await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent('"documentId":"doc-b"'));
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "文献总览" }));
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    fireEvent.click(screen.getByRole("tab", { name: "doc-a.pdf" }));
    await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent('"documentId":"doc-a"'));
    expect(screen.queryByRole("heading", { name: "我的文献" })).not.toBeInTheDocument();
  });

  it("uses arrow keys to switch tabs and Delete to close the active tab", async () => {
    setup();
    await screen.findByRole("tab", { name: "doc-a.pdf" });
    fireEvent.click(screen.getByText("Open B"));
    const b = await screen.findByRole("tab", { name: "doc-b.pdf" });
    fireEvent.keyDown(b, { key: "ArrowLeft" });
    const a = screen.getByRole("tab", { name: "doc-a.pdf" });
    expect(a).toHaveFocus();
    await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent('"documentId":"doc-a"'));
    fireEvent.keyDown(a, { key: "Delete" });
    await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent('"documentId":"doc-b"'));
    expect(screen.getAllByRole("tab")).toHaveLength(1);
    expect(b).toHaveFocus();
  });
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
        body = { items: [], document_id: url.split("/")[3] };
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

it("quick-open hotkey preserves the active page and zoom and does not exit focus mode on modal Escape", async () => {
  setup();
  await screen.findByRole("tab", { name: "doc-a.pdf" });
  fireEvent.click(screen.getByText("Read page 7")); fireEvent.click(screen.getByText("Zoom 150"));
  const reader = screen.getByTestId("reader");
  fireEvent.click(screen.getByRole("button", { name: "专注阅读" }));
  fireEvent.keyDown(window, { key: "k", ctrlKey: true });
  expect(screen.getByRole("combobox", { name: "快速查找" })).toHaveFocus();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "快速打开文献与功能" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "退出专注" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByTestId("reader")).toHaveTextContent('"initialScale":1.5');
  expect(screen.getByTestId("reader")).toBe(reader);
  expect(new URLSearchParams(window.location.search).get("page")).toBe("7");
});

it("does not steal Ctrl+K from text editing or IME composition", async () => {
  setup();
  const question = await screen.findByRole("textbox", { name: "向当前文献提问" });
  fireEvent.keyDown(question, { key: "k", ctrlKey: true });
  expect(screen.queryByRole("dialog", { name: "快速打开文献与功能" })).not.toBeInTheDocument();
  fireEvent.keyDown(window, { key: "k", ctrlKey: true, isComposing: true });
  expect(screen.queryByRole("dialog", { name: "快速打开文献与功能" })).not.toBeInTheDocument();
});

it("keeps the current page when quick-open remounts that same paper in a split", async () => {
  setup();
  await screen.findByRole("tab", { name: "doc-a.pdf" });
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((url, init) => String(url).startsWith("/api/documents?")
    ? Promise.resolve(new Response(JSON.stringify({ total: 2, items: ["doc-a", "doc-b"].map(id => ({ document_id: id, filename: `${id}.pdf`, relative_folder: "papers", current_document_version_id: `v-${id}`, content_id: "a".repeat(64), status: "valid_pdf", cloud_policy: { authorization_source: "test", auto_cloud_eligible: false } })) })))
    : original(url, init));
  fireEvent.click(screen.getByText("Read page 7"));
  fireEvent.click(screen.getByText("Zoom 150"));
  fireEvent.click(screen.getByRole("button", { name: "快速打开文献与功能" }));
  const query = screen.getByRole("combobox", { name: "快速查找" });
  await screen.findByRole("option", { name: /doc-a.pdf/ });
  fireEvent.change(query, { target: { value: "doc-a" } });
  fireEvent.click(screen.getByRole("button", { name: "加入对比" }));
  fireEvent.change(query, { target: { value: "doc-b" } });
  fireEvent.click(screen.getByRole("button", { name: "加入对比" }));
  fireEvent.click(screen.getByRole("button", { name: "并排前两篇" }));
  const left = within(screen.getByRole("region", { name: "左侧文献" }));
  await waitFor(() => expect(left.getByTestId("reader")).toHaveTextContent('"initialPage":7'));
  expect(left.getByTestId("reader")).toHaveTextContent('"initialScale":1.5');
});

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
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    fireEvent.click(citationBtn);
    expect(screen.getAllByRole("tab")).toHaveLength(2);
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

it.each([false, true])("ignores a stale document response after navigation (reject=%s)", async (reject) => {
  window.history.replaceState({}, "", "/?document=doc-a");
  let resolveOld!: (value: unknown) => void;
  let rejectOld!: (error: Error) => void;
  const oldBody = new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject; });
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/documents/doc-a") return { ok: true, json: () => oldBody } as Response;
    const id = url.split("/").at(-1);
    const body = url.endsWith("/annotations") ? { items: [] } : {
      document_id: id, current_document_version_id: `v-${id}`, content_id: "a".repeat(64), filename: `${id}.pdf`,
    };
    return new Response(JSON.stringify(body));
  });
  vi.stubGlobal("fetch", fetcher);
  render(<Workbench />);
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/documents/doc-a", expect.anything()));
  fireEvent.click(screen.getByRole("button", {name:"Open B"}));
  await waitFor(() => expect(screen.getByTestId("reader")).toHaveTextContent("doc-b"));
  await act(async () => {
    if (reject) rejectOld(new Error("old request failed"));
    else resolveOld({document_id:"doc-a",current_document_version_id:"v-a",content_id:"a".repeat(64),filename:"a.pdf"});
  });
  expect(screen.getByTestId("reader")).toHaveTextContent("doc-b");
  expect(screen.queryByText("文献信息暂时无法读取")).not.toBeInTheDocument();
});

it("ignores an old annotation list after another document opens", async () => {
  window.history.replaceState({}, "", "/?document=doc-a");
  let resolveOld!: (value: unknown) => void;
  const oldBody = new Promise((resolve) => { resolveOld = resolve; });
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/documents/doc-a/annotations") return { ok: true, json: () => oldBody } as Response;
    const id = url.split("/")[3];
    return new Response(JSON.stringify(url.endsWith("/annotations") ? {items:[],document_id:id} : {
      document_id:id,current_document_version_id:`v-${id}`,content_id:"a".repeat(64),filename:`${id}.pdf`,
    }));
  });
  vi.stubGlobal("fetch", fetcher);
  render(<Workbench />);
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/documents/doc-a/annotations", expect.anything()));
  fireEvent.click(screen.getByRole("button",{name:"Open B"}));
  await waitFor(() => expect(screen.getByTestId("annotations")).toHaveTextContent("doc-b"));
  await act(async () => { resolveOld({items:[],document_id:"doc-a"}); });
  expect(screen.getByTestId("annotations")).toHaveTextContent("doc-b");
});


describe("active comparison document", () => {
  async function compare() {
    setup();
    await screen.findByRole("tab", { name: "doc-a.pdf" });
    fireEvent.click(screen.getByText("Open B"));
    await screen.findByRole("tab", { name: "doc-b.pdf" });
    fireEvent.click(screen.getByRole("button", { name: "右侧对照 doc-a.pdf" }));
    const left = screen.getByRole("region", { name: "左侧文献" });
    const right = screen.getByRole("region", { name: "右侧文献" });
    await waitFor(() => expect(within(right).getByTestId("reader")).toHaveTextContent('"documentId":"doc-a"'));
    return { left, right };
  }
  it("routes the assistant, annotations and citations to the clicked pane without swapping", async () => {
    const { left, right } = await compare();
    fireEvent.pointerDown(right);
    await waitFor(() => expect(screen.getByTestId("annotations")).toHaveTextContent("doc-a"));
    expect(screen.getByText(/助手与笔记跟随右侧：doc-a.pdf/)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "向当前文献提问" }), { target: { value: "Compare methods" } });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));
    await screen.findByText("Review evidence answer");
    expect(vi.mocked(fetch).mock.calls.some(([url]) => url === "/api/documents/doc-a/ask")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: /第 1 页/ }));
    await waitFor(() => expect(within(right).getByTestId("reader")).toHaveTextContent('"documentId":"doc-a","page":1'));
    expect(within(left).getByTestId("reader")).toHaveTextContent('"citationHighlight":null');
    expect(new URLSearchParams(location.search).get("document")).toBe("doc-b");
    fireEvent.focus(within(left).getByText("Read page 7"));
    await waitFor(() => expect(screen.getByTestId("annotations")).toHaveTextContent("doc-b"));
    expect(screen.getByText(/助手与笔记跟随左侧：doc-b.pdf/)).toBeInTheDocument();
    expect(screen.queryByText("Review evidence answer")).not.toBeInTheDocument();
  });
  it("ignores a late right-document answer after the user activates the left pane", async () => {
    const { left, right } = await compare();
    const original = vi.mocked(fetch).getMockImplementation()!;
    let resolve!: (value: Response) => void;
    vi.mocked(fetch).mockImplementation((input, init) => String(input).endsWith("/ask") ? new Promise(r => { resolve = r; }) : original(input, init));
    fireEvent.pointerDown(right);
    fireEvent.change(screen.getByRole("textbox", { name: "向当前文献提问" }), { target: { value: "Waiting" } });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));
    await waitFor(() => expect(resolve).toBeDefined());
    fireEvent.pointerDown(left);
    await act(async () => resolve(new Response(JSON.stringify({ answer: "Late right answer", citations: [], provider: "qwen", model: "local", fallback_reason: null }))));
    expect(screen.queryByText("Late right answer")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "提问" })).toBeEnabled();
  });
});


describe("streamlined workspace controls", () => {
  it("hides the left library sidebar while 文献总览 is open and toggles right dock tabs + auto-translate", async () => {
    setup();
    await screen.findByRole("tab", { name: "doc-a.pdf" });
    const autoBtn = screen.getByRole("button", { name: /划词即译/ });
    expect(autoBtn).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(autoBtn);
    expect(autoBtn).toHaveAttribute("aria-pressed", "false");
    expect(localStorage.getItem("lw-auto-translate")).toBe("false");

    const aiTab = screen.getByRole("button", { name: "AI 助手" });
    const annTab = screen.getByRole("button", { name: /批注与笔记/ });
    expect(aiTab).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(annTab);
    expect(annTab).toHaveAttribute("aria-pressed", "true");
    expect(aiTab).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(screen.getByRole("button", { name: "文献总览" }));
    expect(document.querySelector(".workbench--library-hidden")).not.toBeNull();
  });
});
