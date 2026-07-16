import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { LibraryPanel } from "./LibraryPanel";
import type { DocumentApi } from "./documents";
import type { ImportApi } from "./dropFiles";

afterEach(cleanup);

function api(): ImportApi {
  return {
    createImportIntent: vi.fn().mockResolvedValue({
      intent_id: "intent-1",
      upload_token: "token-1",
      expires_at: "2026-07-15T09:15:00Z",
    }),
    uploadImport: vi.fn().mockResolvedValue({
      intent_id: "intent-1",
      status: "uploaded",
      document_id: null,
      error_code: null,
      poll_after_ms: 1,
    }),
    getImport: vi.fn()
      .mockResolvedValueOnce({
        intent_id: "intent-1",
        status: "validating",
        document_id: null,
        error_code: null,
        poll_after_ms: 1,
      })
      .mockResolvedValueOnce({
        intent_id: "intent-1",
        status: "completed",
        document_id: "document-1",
        error_code: null,
        poll_after_ms: null,
      }),
    listActiveImports: vi.fn().mockResolvedValue([]),
  };
}

function documentApi(items: Awaited<ReturnType<DocumentApi["listDocuments"]>> = []): DocumentApi {
  return { listDocuments: vi.fn().mockResolvedValue(items) };
}

test("document inventory is clickable and marks the active PDF", async () => {
  const user = userEvent.setup();
  const onOpenDocument = vi.fn();
  render(
    <LibraryPanel
      activeDocumentId="document-active"
      documentApi={documentApi([
        { document_id: "document-active", filename: "active.pdf" },
        { document_id: "document-next", filename: "next.pdf" },
      ])}
      importApi={api()}
      onOpenDocument={onOpenDocument}
    />,
  );

  expect(await screen.findByRole("button", { name: "active.pdf" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await user.click(screen.getByRole("button", { name: "next.pdf" }));
  expect(onOpenDocument).toHaveBeenCalledWith("document-next");
});

test("a newly completed import opens once, while restored history does not", async () => {
  const user = userEvent.setup();
  const importApi = api();
  vi.mocked(importApi.uploadImport).mockResolvedValue({
    intent_id: "intent-1",
    file_name: "new.pdf",
    status: "completed",
    document_id: "document-new",
    error_code: null,
    poll_after_ms: null,
  });
  vi.mocked(importApi.listActiveImports).mockResolvedValue([
    {
      intent_id: "history",
      file_name: "history.pdf",
      status: "completed",
      document_id: "document-history",
      error_code: null,
      poll_after_ms: null,
    },
  ]);
  const onOpenDocument = vi.fn();
  render(
    <LibraryPanel
      documentApi={documentApi()}
      importApi={importApi}
      onOpenDocument={onOpenDocument}
    />,
  );

  await screen.findByText("history.pdf");
  expect(onOpenDocument).not.toHaveBeenCalled();
  await user.upload(
    screen.getByLabelText("导入文献"),
    new File(["%PDF-1.7"], "new.pdf", { type: "application/pdf" }),
  );
  await user.click(screen.getByRole("button", { name: "确认导入" }));

  await waitFor(() => expect(onOpenDocument).toHaveBeenCalledTimes(1));
  expect(onOpenDocument).toHaveBeenCalledWith("document-new");
});

test("document inventory failure is sanitized without hiding import controls", async () => {
  const failedDocuments: DocumentApi = {
    listDocuments: vi.fn().mockRejectedValue(new Error("private server detail")),
  };
  render(<LibraryPanel documentApi={failedDocuments} importApi={api()} />);

  expect(await screen.findByText("文献列表暂时无法读取")).toBeVisible();
  expect(screen.queryByText(/private server detail/)).not.toBeInTheDocument();
  expect(screen.getByLabelText("导入文献")).toBeVisible();
});

test("PDF bytes do not upload until explicit confirmation", async () => {
  const user = userEvent.setup();
  const importApi = api();
  render(<LibraryPanel importApi={importApi} />);

  const file = new File(["%PDF-1.7\n%%EOF"], "paper.pdf", {
    type: "application/pdf",
  });
  await user.upload(screen.getByLabelText("导入文献"), file);

  expect(screen.getByText("登记后，这篇 PDF 可按需自动用于云端处理")).toBeVisible();
  expect(importApi.createImportIntent).not.toHaveBeenCalled();
  expect(importApi.uploadImport).not.toHaveBeenCalled();

  await user.click(screen.getByRole("button", { name: "确认导入" }));

  expect(importApi.createImportIntent).toHaveBeenCalledTimes(1);
  expect(importApi.uploadImport).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(screen.getByText("导入完成")).toBeVisible());
});

test("cancelling clears the selected File and calls no endpoint", async () => {
  const user = userEvent.setup();
  const importApi = api();
  render(<LibraryPanel importApi={importApi} />);

  await user.upload(
    screen.getByLabelText("导入文献"),
    new File(["%PDF-1.7"], "cancel.pdf", { type: "application/pdf" }),
  );
  await user.click(screen.getByRole("button", { name: "取消" }));

  expect(screen.queryByRole("button", { name: "确认导入" })).not.toBeInTheDocument();
  expect(importApi.createImportIntent).not.toHaveBeenCalled();
  expect(importApi.uploadImport).not.toHaveBeenCalled();
});

test("non-PDF explains the future one-time permission path", async () => {
  const user = userEvent.setup();
  const importApi = api();
  render(<LibraryPanel importApi={importApi} />);

  await user.upload(
    screen.getByLabelText("导入文献"),
    new File(["docx"], "notes.docx", {
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }),
  );

  expect(
    screen.getByText("首版暂不导入；以后可在单次明确授权后用于云处理"),
  ).toBeVisible();
  expect(screen.queryByText(/永久/)).not.toBeInTheDocument();
  expect(importApi.createImportIntent).not.toHaveBeenCalled();
});

test("refresh resumes current-session active imports from the server inventory", async () => {
  const importApi = api();
  vi.mocked(importApi.listActiveImports).mockResolvedValue([
    {
      intent_id: "intent-resume",
      file_name: "resumed.pdf",
      status: "completed",
      document_id: "document-resume",
      error_code: null,
      poll_after_ms: null,
    },
  ]);

  render(<LibraryPanel importApi={importApi} />);

  await waitFor(() => expect(importApi.listActiveImports).toHaveBeenCalledTimes(1));
  expect(screen.getByText("导入完成")).toBeVisible();
});

test("first confirmation click closes the dialog and a duplicate click creates one intent", async () => {
  const user = userEvent.setup();
  const importApi = api();
  let resolveIntent!: (value: { intent_id: string; upload_token: string; expires_at: string }) => void;
  vi.mocked(importApi.createImportIntent).mockReturnValue(
    new Promise((resolve) => { resolveIntent = resolve; }),
  );
  render(<LibraryPanel importApi={importApi} />);
  await user.upload(
    screen.getByLabelText("导入文献"),
    new File(["%PDF-1.7"], "once.pdf", { type: "application/pdf" }),
  );
  const button = screen.getByRole("button", { name: "确认导入" });

  fireEvent.click(button);
  fireEvent.click(button);

  expect(screen.queryByRole("button", { name: "确认导入" })).not.toBeInTheDocument();
  expect(importApi.createImportIntent).toHaveBeenCalledTimes(1);
  resolveIntent({ intent_id: "intent-1", upload_token: "token-1", expires_at: "2026-07-15T09:15:00Z" });
  await waitFor(() => expect(importApi.uploadImport).toHaveBeenCalledTimes(1));
});

test("server inventory renders every task with its preserved filename", async () => {
  const importApi = api();
  vi.mocked(importApi.listActiveImports).mockResolvedValue([
    { intent_id: "a", file_name: "alpha.pdf", status: "completed", document_id: "doc-a", error_code: null, poll_after_ms: null },
    { intent_id: "b", file_name: "beta.pdf", status: "failed", document_id: null, error_code: "import_failed", poll_after_ms: null },
  ]);

  render(<LibraryPanel importApi={importApi} />);

  expect(await screen.findByText("alpha.pdf")).toBeVisible();
  expect(screen.getByText("beta.pdf")).toBeVisible();
});

test("poll failure is sanitized and automatically recovers", async () => {
  const importApi = api();
  vi.mocked(importApi.listActiveImports).mockResolvedValue([
    { intent_id: "poll", file_name: "poll.pdf", status: "validating", document_id: null, error_code: null, poll_after_ms: 1 },
  ]);
  vi.mocked(importApi.getImport).mockReset()
    .mockRejectedValueOnce(new Error("sensitive server detail"))
    .mockResolvedValueOnce({
      intent_id: "poll",
      file_name: "poll.pdf",
      status: "completed",
      document_id: "doc-poll",
      error_code: null,
      poll_after_ms: null,
    });

  render(<LibraryPanel importApi={importApi} />);

  expect(await screen.findByText("状态更新暂时失败，正在重试")).toBeVisible();
  expect(screen.queryByText(/sensitive server detail/)).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByText("导入完成")).toBeVisible(), { timeout: 2500 });
  expect(importApi.getImport).toHaveBeenCalledTimes(2);
});

test("dropping a PDF with an empty browser MIME still waits for confirmation", () => {
  const importApi = api();
  render(<LibraryPanel importApi={importApi} />);
  const file = new File(["%PDF-1.7"], "dropped.pdf", { type: "" });

  fireEvent.drop(screen.getByLabelText("文献导入区"), {
    dataTransfer: { files: { item: () => file } },
  });

  expect(screen.getByRole("button", { name: "确认导入" })).toBeVisible();
  expect(importApi.createImportIntent).not.toHaveBeenCalled();
  expect(importApi.uploadImport).not.toHaveBeenCalled();
});
