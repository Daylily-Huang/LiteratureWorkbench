import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { BackupImport, uploadBackup } from "./BackupImport";
import type { Job } from "./DataCenter";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const file = () => new File(["x".repeat(1000)], "我的文献.lwb.zip");
const job: Job = { id: "import-id", kind: "import", status: "queued", stage: "queued", processed_files: 0 };
function props() { return { disabled: false, fetcher: vi.fn(async () => new Response(JSON.stringify({ csrf_token: "csrf" }))), onJob: vi.fn(), onBusy: vi.fn(), message: (code: string) => `错误：${code}` }; }

it("shows upload progress, then hands only the import job to polling without restoring it", async () => {
  const p = props();
  let done!: (value: Job) => void;
  const upload = vi.fn(() => new Promise<Job>(resolve => { done = resolve; }));
  render(<BackupImport {...p} upload={upload} />);
  fireEvent.change(screen.getByLabelText("导入备份文件"), { target: { files: [file()] } });
  await waitFor(() => expect(upload).toHaveBeenCalledOnce());
  const args = upload.mock.calls[0] as unknown as [File, string, AbortSignal, (n: number) => void];
  expect(args[1]).toBe("csrf"); expect(args[2].aborted).toBe(false);
  act(() => args[3](500));
  expect(screen.getByRole("progressbar", { name: "备份传输进度" })).toHaveValue(50);
  expect(screen.getByRole("button", { name: "选择备份文件" })).toBeDisabled();
  await act(async () => done(job));
  expect(p.onJob).toHaveBeenCalledExactlyOnceWith(job);
  expect(p.onBusy.mock.calls).toEqual([[true], [false]]);
  expect(screen.getByText(/后台核验结果见下方任务状态/)).toBeVisible();
  expect(p.fetcher).toHaveBeenCalledExactlyOnceWith("/api/session/current", expect.anything());
});

it("rejects wrong type, empty or multiple dropped files before any session request", () => {
  const p = props(); const upload = vi.fn(); const { container } = render(<BackupImport {...p} upload={upload} />);
  fireEvent.change(screen.getByLabelText("导入备份文件"), { target: { files: [new File(["pdf"], "paper.pdf")] } });
  expect(screen.getByRole("alert")).toHaveTextContent(".lwb.zip");
  fireEvent.drop(container.querySelector(".backup-import__drop")!, { dataTransfer: { files: [file(), file()] } });
  expect(screen.getByRole("alert")).toHaveTextContent("一次选择一份");
  fireEvent.change(screen.getByLabelText("导入备份文件"), { target: { files: [new File([], "empty.lwb.zip")] } });
  expect(screen.getByRole("alert")).toHaveTextContent("为空");
  expect(p.fetcher).not.toHaveBeenCalled(); expect(upload).not.toHaveBeenCalled();
});

it("aborts on cancel and ignores a late successful response, then permits retry", async () => {
  const p = props(); let done!: (value: Job) => void;
  const upload = vi.fn(() => new Promise<Job>(resolve => { done = resolve; }));
  const { container } = render(<BackupImport {...p} upload={upload} />);
  fireEvent.drop(container.querySelector(".backup-import__drop")!, { dataTransfer: { files: [file()] } });
  await waitFor(() => expect(upload).toHaveBeenCalledOnce());
  const signal = (upload.mock.calls[0] as unknown as [File, string, AbortSignal])[2];
  fireEvent.click(screen.getByRole("button", { name: "取消传输" })); expect(signal.aborted).toBe(true);
  await act(async () => done(job)); expect(p.onJob).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "选择备份文件" })).toBeEnabled();
});

it("aborts on unmount and does not publish a late result", async () => {
  const p = props(); let done!: (value: Job) => void;
  const upload = vi.fn(() => new Promise<Job>(resolve => { done = resolve; }));
  const view = render(<BackupImport {...p} upload={upload} />);
  fireEvent.change(screen.getByLabelText("导入备份文件"), { target: { files: [file()] } });
  await waitFor(() => expect(upload).toHaveBeenCalledOnce());
  const signal = (upload.mock.calls[0] as unknown as [File, string, AbortSignal])[2]; view.unmount();
  expect(signal.aborted).toBe(true); await act(async () => done(job)); expect(p.onJob).not.toHaveBeenCalled();
});

it("keeps errors actionable and allows selecting the same file again", async () => {
  const p = props(), upload = vi.fn().mockRejectedValueOnce(new Error("backup_upload_incomplete")).mockResolvedValueOnce(job);
  render(<BackupImport {...p} upload={upload} />);
  const input = screen.getByLabelText("导入备份文件");
  fireEvent.change(input, { target: { files: [file()] } });
  expect(await screen.findByRole("alert")).toHaveTextContent("backup_upload_incomplete");
  fireEvent.change(input, { target: { files: [file()] } });
  await waitFor(() => expect(p.onJob).toHaveBeenCalledExactlyOnceWith(job));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("sends the File directly with encoded filename and CSRF, and handles request abortion", async () => {
  class Request {
    static last: Request; headers: Record<string, string> = {}; upload = { onprogress: (_event: { loaded: number }) => {} };
    responseType = ""; withCredentials = false; status = 202; response: unknown = job;
    onload = () => {}; onerror = () => {}; onabort = () => {};
    open = vi.fn(); setRequestHeader = (name: string, value: string) => { this.headers[name] = value; };
    send = vi.fn(); abort = () => this.onabort(); constructor() { Request.last = this; }
  }
  vi.stubGlobal("XMLHttpRequest", Request);
  const f = file(), controller = new AbortController(), progress = vi.fn();
  const result = uploadBackup(f, "csrf", controller.signal, progress), request = Request.last;
  expect(request.open).toHaveBeenCalledWith("POST", "/api/data/backups/import");
  expect(request.headers).toMatchObject({ "X-LW-CSRF": "csrf", "X-LW-Backup-Filename": encodeURIComponent(f.name) });
  expect(request.send).toHaveBeenCalledWith(f);
  request.upload.onprogress({ loaded: 500 }); expect(progress).toHaveBeenCalledWith(500);
  request.onload(); await expect(result).resolves.toEqual(job);
  const second = uploadBackup(f, "csrf", controller.signal, progress);
  controller.abort(); await expect(second).rejects.toMatchObject({ name: "AbortError" });
});
