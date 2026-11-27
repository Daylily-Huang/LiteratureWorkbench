import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ReaderDocument } from "../reader/PdfReader";
import { AssistantPanel } from "./AssistantPanel";


const DOCUMENT: ReaderDocument = {
  documentId: "doc-1",
  documentVersionId: "ver-1",
  contentId: "c".repeat(64),
  filename: "paper.pdf",
};

const ANSWER = {
  answer: "Alpha is the first letter [p1].",
  citations: [{ physical_page: 3, excerpt: "Alpha beta gamma", rects: [[10, 20, 300, 80]] }],
  provider: "deepseek",
  model: "deepseek-v4-flash",
  fallback_reason: null,
  citation_status: "verified",
};


describe("AssistantPanel", () => {
  afterEach(cleanup);

  it("prepares selected reading without a request, then submits its exact identity only to its document", async () => {
    const selection = { ...DOCUMENT, pdfPage: 3, text: "Alpha beta gamma", contextBefore: "", contextAfter: "", rects: [], pdfQuads: [], buttonAnchor: { x: 0, y: 0 }, viewportAnchor: { x: 0, y: 0 }, translationEligible: true };
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...ANSWER, content_id: DOCUMENT.contentId })));
    const jump = vi.fn();
    const props = { document: DOCUMENT, fetcher, onJumpToCitation: jump, selectionDraft: { selection, nonce: 1 }, selectedDocuments: [{ document_id: "other", filename: "other.pdf" }] };
    const { rerender } = render(<AssistantPanel {...props} />);
    expect(screen.getByRole("region", { name: "选区精读原文" })).toHaveTextContent("Alpha beta gamma");
    expect(fetcher).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "方法与假设" }));
    fireEvent.click(screen.getByRole("button", { name: "提问" }));
    await screen.findByText(ANSWER.answer);
    expect(fetcher.mock.calls[1]![0]).toBe("/api/documents/doc-1/ask");
    const payload = JSON.parse(fetcher.mock.calls[1]![1].body);
    expect(payload.source_selection).toEqual({ document_version_id: "ver-1", content_id: DOCUMENT.contentId, physical_page: 3, selected_text: "Alpha beta gamma" });
    expect(payload.document_ids).toBeUndefined();
    rerender(<AssistantPanel {...props} document={{ ...DOCUMENT, contentId: "d".repeat(64), documentVersionId: "ver-2" }} />);
    expect(screen.getByRole("button", { name: "提问" })).toBeDisabled();
    expect(screen.getByText(/这份回答对应的 PDF 已变化/)).toBeVisible();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "第 3 页" }));
    expect(jump).toHaveBeenCalledWith({ ...ANSWER.citations[0], rects: [] });
    confirm.mockRestore();
    fireEvent.click(screen.getByRole("button", { name: "移除选区" }));
    expect(screen.getByRole("button", { name: "提问" })).toBeEnabled();
  });

  it("defaults to the library scope without an open document", () => {
    render(<AssistantPanel document={null} onJumpToCitation={() => undefined} />);

    expect(screen.getByRole("button", { name: "提问" })).toBeDisabled();
    expect(screen.getByLabelText("向文献库提问")).toBeEnabled();
    expect(screen.getByRole("button", { name: "当前文献" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "整个文献库" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("asks the whole library and renders attributed citations", async () => {
    const libraryAnswer = {
      answer: "库里有 alpha [d1 p1]",
      citations: [
        {
          document_id: "doc-9",
          display_name: "other.pdf",
          physical_page: 2,
          excerpt: "Alpha beta gamma",
          rects: [[10, 20, 300, 80]],
        },
      ],
      provider: "deepseek",
      model: "deepseek-v4-flash",
      fallback_reason: null,
      indexed_documents: 3,
      citation_status: "verified",
    };
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify(libraryAnswer), { status: 200 }));
    const onJumpToLibraryCitation = vi.fn();
    render(
      <AssistantPanel
        document={DOCUMENT}
        fetcher={fetcher}
        onJumpToCitation={() => undefined}
        onJumpToLibraryCitation={onJumpToLibraryCitation}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "整个文献库" }));
    fireEvent.change(screen.getByLabelText("向文献库提问"), {
      target: { value: "库里有 alpha 吗？" },
    });

    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText("库里有 alpha [d1 p1]")).toBeVisible();
    expect(screen.getByText("可检索 3 篇 · 引用 1 篇")).toBeVisible();
    const askCall = fetcher.mock.calls[1];
    expect(askCall?.[0]).toBe("/api/libraries/ask");

    fireEvent.click(screen.getByRole("button", { name: "《other.pdf》第 2 页" }));
    expect(onJumpToLibraryCitation).toHaveBeenCalledWith({
      document_id: "doc-9",
      display_name: "other.pdf",
      physical_page: 2,
      excerpt: "Alpha beta gamma",
      rects: [[10, 20, 300, 80]],
    });
  });

  it("asks the library directly when no document is open", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            answer: "没有命中",
            citations: [],
            provider: "retrieval_only",
            model: "",
            fallback_reason: null,
            indexed_documents: 0,
            citation_status: "missing",
          }),
          { status: 200 },
        ),
      );
    render(<AssistantPanel document={null} fetcher={fetcher} onJumpToCitation={() => undefined} />);
    fireEvent.change(screen.getByLabelText("向文献库提问"), {
      target: { value: "什么是 alpha？" },
    });

    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText("没有命中")).toBeVisible();
    expect(screen.getByText("库内检索")).toBeVisible();
    expect(fetcher.mock.calls[1]?.[0]).toBe("/api/libraries/ask");
  });

  it("asks with csrf and renders the answer with clickable citations", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify(ANSWER), { status: 200 }));
    const onJumpToCitation = vi.fn();
    render(
      <AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={onJumpToCitation} />,
    );
    fireEvent.change(screen.getByLabelText("向当前文献提问"), {
      target: { value: "  What is alpha?  " },
    });

    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText("Alpha is the first letter [p1].")).toBeVisible();
    expect(screen.getByText("DeepSeek · deepseek-v4-flash")).toBeVisible();
    const askCall = fetcher.mock.calls[1];
    expect(askCall?.[0]).toBe("/api/documents/doc-1/ask");
    expect(askCall?.[1]).toMatchObject({
      headers: { "Content-Type": "application/json", "X-LW-CSRF": "csrf" },
    });
    expect(JSON.parse(String((askCall?.[1] as RequestInit).body))).toEqual({
      question: "What is alpha?",
    });

    fireEvent.click(screen.getByRole("button", { name: "第 3 页" }));
    expect(onJumpToCitation).toHaveBeenCalledWith({
      physical_page: 3,
      excerpt: "Alpha beta gamma",
      rects: [[10, 20, 300, 80]],
    });
  });

  it("renders notice when answer has missing citations", async () => {
    const missingAnswer = {
      answer: "Alpha without citation.",
      citations: [],
      provider: "deepseek",
      model: "deepseek-v4-flash",
      fallback_reason: null,
      citation_status: "missing",
    };
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify(missingAnswer), { status: 200 }));
    render(
      <AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={() => undefined} />,
    );
    fireEvent.change(screen.getByLabelText("向当前文献提问"), {
      target: { value: "What is alpha?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText("Alpha without citation.")).toBeVisible();
    expect(screen.getByText("回答未生成可验证的原文引用，请核对原文。")).toBeVisible();
  });

  it("clears answer when document changes to prevent race/cross-doc pollution", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify(ANSWER), { status: 200 }));
    const { rerender } = render(
      <AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={() => undefined} />,
    );
    fireEvent.change(screen.getByLabelText("向当前文献提问"), {
      target: { value: "What is alpha?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText("Alpha is the first letter [p1].")).toBeVisible();

    const otherDoc: ReaderDocument = {
      documentId: "doc-2",
      documentVersionId: "ver-2",
      contentId: "d".repeat(64),
      filename: "other.pdf",
    };

    rerender(
      <AssistantPanel document={otherDoc} fetcher={fetcher} onJumpToCitation={() => undefined} />,
    );

    expect(screen.queryByText("Alpha is the first letter [p1].")).not.toBeInTheDocument();
  });

  it("syncs draft question passed from external props", () => {
    const onConsumed = vi.fn();
    const { rerender } = render(
      <AssistantPanel
        document={DOCUMENT}
        draftQuestion=""
        onDraftQuestionConsumed={onConsumed}
        onJumpToCitation={() => undefined}
      />,
    );

    rerender(
      <AssistantPanel
        document={DOCUMENT}
        draftQuestion="从 Dashboard 传入的推荐问题"
        onDraftQuestionConsumed={onConsumed}
        onJumpToCitation={() => undefined}
      />,
    );

    expect(screen.getByLabelText("向当前文献提问")).toHaveValue("从 Dashboard 传入的推荐问题");
    expect(onConsumed).toHaveBeenCalled();
  });

  it("flags local-model fallback answers distinctly", async () => {
    const fallbackAnswer = {
      ...ANSWER,
      provider: "qwen",
      model: "qwen-local",
      fallback_reason: "deepseek_total_timeout",
    };
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify(fallbackAnswer), { status: 200 }));
    render(
      <AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={() => undefined} />,
    );
    fireEvent.change(screen.getByLabelText("向当前文献提问"), {
      target: { value: "What is alpha?" },
    });

    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(
      await screen.findByText("本地模型回答（云端不可用，已自动切换）"),
    ).toBeVisible();
  });

  it("surfaces backend error codes as readable messages", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ code: "parse_required" }), { status: 409 }),
      );
    render(
      <AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={() => undefined} />,
    );
    fireEvent.change(screen.getByLabelText("向当前文献提问"), {
      target: { value: "What is alpha?" },
    });

    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "文档尚未完成解析，请稍后再提问",
    );
  });

  it("keeps the previous answer out of view after a failed follow-up", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify(ANSWER), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ code: "deepseek_total_timeout" }), { status: 502 }),
      );
    render(
      <AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={() => undefined} />,
    );
    const input = screen.getByLabelText("向当前文献提问");
    fireEvent.change(input, { target: { value: "What is alpha?" } });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));
    expect(await screen.findByText("Alpha is the first letter [p1].")).toBeVisible();

    fireEvent.change(input, { target: { value: "And beta?" } });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("回答失败，请重试");
    await waitFor(() => {
      expect(screen.queryByText("Alpha is the first letter [p1].")).not.toBeInTheDocument();
    });
  });

  it("cancels in-flight request and resets busy on scope switch", async () => {
    let abortSignal: AbortSignal | undefined;
    const fetcher = vi.fn().mockImplementation((url, init) => {
      abortSignal = init?.signal;
      return new Promise(() => {
        // never resolves to simulate pending request
      });
    });

    render(
      <AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={() => undefined} />,
    );
    const input = screen.getByLabelText("向当前文献提问");
    fireEvent.change(input, { target: { value: "What is alpha?" } });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(screen.getByRole("button", { name: "回答中…" })).toBeDisabled();

    // Switch scope to library
    fireEvent.click(screen.getByRole("button", { name: "整个文献库" }));

    // In-flight request was aborted and button is no longer busy
    expect(abortSignal?.aborted).toBe(true);
    expect(screen.getByRole("button", { name: "提问" })).not.toBeDisabled();
  });
});

it("clears a completed answer when the user changes its search scope", async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
    .mockResolvedValueOnce(new Response(JSON.stringify(ANSWER)));
  const view = render(<AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={() => undefined} />);
  try {
    fireEvent.change(screen.getByLabelText("向当前文献提问"), {target:{value:"alpha"}});
    fireEvent.click(screen.getByRole("button",{name:"提问"}));
    await screen.findByText(ANSWER.answer);
    fireEvent.click(screen.getByRole("button",{name:"整个文献库"}));
    expect(screen.queryByText(ANSWER.answer)).not.toBeInTheDocument();
    expect(screen.queryByRole("button",{name:"第 3 页"})).not.toBeInTheDocument();
  } finally { view.unmount(); }
});

it("stops waiting and ignores a late answer from the stopped request", async () => {
  let finish!: (value: Response) => void;
  const pending = new Promise<Response>(resolve => { finish = resolve; });
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({csrf_token:"csrf"}))).mockReturnValueOnce(pending);
  const view = render(<AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={() => {}} initialQuestion="Question" />);
  fireEvent.click(screen.getByRole("button",{name:"提问"}));
  await waitFor(()=>expect(fetcher).toHaveBeenCalledTimes(2));
  fireEvent.click(screen.getByRole("button",{name:"停止等待"}));
  expect(screen.getByRole("button",{name:"提问"})).toBeEnabled();
  finish(new Response(JSON.stringify(ANSWER)));
  await waitFor(()=>expect(screen.getByRole("status")).toHaveTextContent("已停止等待"));
  expect(screen.queryByText(ANSWER.answer)).not.toBeInTheDocument();
  view.unmount();
});

it("sends the explicit selection and opens the archived answer", async () => {
  const fetcher=vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({csrf_token:"csrf"}))).mockResolvedValueOnce(new Response(JSON.stringify({...ANSWER, citations:[], indexed_documents:2,entry_id:"entry-1",history_saved:true})));
  const open=vi.fn();
  render(<AssistantPanel document={null} selectedDocuments={[{document_id:"d1",filename:"A.pdf"},{document_id:"d2",filename:"B.pdf"}]} selectionRequest={1} onOpenNotebook={open} onJumpToCitation={vi.fn()} fetcher={fetcher} />);
  fireEvent.change(screen.getByLabelText("阅读方式"),{target:{value:"retrieval"}});
  fireEvent.change(screen.getByLabelText("向所选文献提问"),{target:{value:"Compare"}});
  fireEvent.click(screen.getByRole("button",{name:"提问"}));
  await screen.findByText(ANSWER.answer);
  expect(JSON.parse(fetcher.mock.calls[1]![1].body)).toEqual({question:"Compare",document_ids:["d1","d2"]});
  fireEvent.click(screen.getByRole("button",{name:"查看记录 / 写笔记"}));
  expect(open).toHaveBeenCalledWith("entry-1");
  cleanup();
});


it("switches current document to full-text mode when clicking 总结全文 and submits full-text ask", async () => {
  const prepareResponse = {
    reading_mode: "full_text",
    text_only: true,
    plan_id: "f".repeat(64),
    cache_status: "miss",
    api_call_required: true,
    context_kind: "full_text",
    request_bytes: 24_000,
    max_request_bytes: 200_000,
    source_versions: { "doc-1": DOCUMENT.contentId },
    coverage: [{
      document_id: "doc-1",
      display_name: "paper.pdf",
      content_id: DOCUMENT.contentId,
      parse_run_id: "run-1",
      total_pages: 15,
      supplied_pages: 15,
      text_characters: 38_000,
    }],
  };
  const askResponse = {
    answer: "Full-paper summary covering pages 1 to 15 [d1 p12].",
    citations: [{ document_id: "doc-1", display_name: "paper.pdf", physical_page: 12, excerpt: "Results on page 12", rects: [[10, 20, 300, 80]] }],
    provider: "deepseek",
    model: "deepseek-v4-flash",
    fallback_reason: null,
    indexed_documents: 1,
    citation_status: "verified",
    reading_mode: "full_text",
    reading_coverage: prepareResponse,
  };
  const fetcher = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
    .mockResolvedValueOnce(new Response(JSON.stringify(prepareResponse)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
    .mockResolvedValueOnce(new Response(JSON.stringify(askResponse)));
  const onJump = vi.fn();
  render(<AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={onJump} />);
  fireEvent.click(screen.getByRole("button", { name: "总结全文" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/libraries/full-text/prepare", expect.anything()));
  await waitFor(() => expect(screen.getByRole("button", { name: "提问" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "提问" }));
  expect(await screen.findByText(/Full-paper summary covering pages 1 to 15/)).toBeVisible();
  expect(fetcher.mock.calls[3]?.[0]).toBe("/api/libraries/ask");
  const payload = JSON.parse(String((fetcher.mock.calls[3]?.[1] as RequestInit).body));
  expect(payload.reading_mode).toBe("full_text");
  expect(payload.document_ids).toEqual(["doc-1"]);
  fireEvent.click(screen.getByRole("button", { name: "《paper.pdf》第 12 页" }));
  expect(onJump).toHaveBeenCalledWith(expect.objectContaining({ physical_page: 12 }));
  cleanup();
});
