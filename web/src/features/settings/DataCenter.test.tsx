import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DataCenter } from "./DataCenter";

afterEach(cleanup);
const overview = { data_root: "D:/ResearchData", backup_directory: "D:/ResearchData/backups/workspace", active_pdf_count: 2,
  counts: { documents: 2, research_entries: 3, document_relations: 1, full_text_study_cache: 4, translation_cache: 8 }, backups: [], backups_total: 0, active_job: null };
const job = { id: "new-backup", kind: "create", status: "queued", stage: "queued", processed_files: 0 };
const backup = { ...job, created_at: "2026-10-23T00:00:00Z", status: "completed", stage: "completed", include_pdfs: true, archive_available: true, size_bytes: 12345, pdf_count: 2, verification_status: "passed", verified_at: "2026-10-23T00:00:00Z" };
function response(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status }); }

it("reads status without creating a backup, and submits a database-only choice once with CSRF", async () => {
  let created = false;
  const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    if (url === "/api/session/current") return response({ csrf_token: "csrf" });
    if (init?.method === "POST") { created = true; return response(job, 202); }
    if (String(url).startsWith("/api/data/jobs/")) return response({ ...job, status: "completed", stage: "completed" });
    return response({ ...overview, ...(created ? { backups: [backup], backups_total: 1 } : {}) });
  });
  render(<DataCenter fetcher={fetcher} onClose={vi.fn()} />);
  await screen.findByText("D:/ResearchData");
  expect(fetcher.mock.calls.filter(c => c[1]?.method === "POST")).toHaveLength(0);
  fireEvent.click(screen.getByRole("checkbox", { name: "包含原始 PDF 与批注恢复文件（推荐）" }));
  expect(screen.getByText(/只保存工作台数据库/)).toBeVisible();
  const button = screen.getByRole("button", { name: "创建并核验备份" });
  fireEvent.click(button); fireEvent.click(button);
  await screen.findByText("文件校验与数据库完整性检查通过。");
  const calls = fetcher.mock.calls.filter(c => c[1]?.method === "POST");
  expect(calls).toHaveLength(1);
  expect(JSON.parse(calls[0]![1]!.body as string)).toEqual({ include_pdfs: false });
  expect(calls[0]![1]!.headers).toMatchObject({ "X-LW-CSRF": "csrf" });
  expect(screen.getByRole("link", { name: "下载备份" })).toHaveAttribute("href", "/api/data/backups/new-backup/download");
});

it("restores progress when reopening and reports the isolated recovery candidate honestly", async () => {
  const fetcher = vi.fn(async (url: RequestInfo | URL) => String(url).startsWith("/api/data/jobs/") ? response({ ...job, kind: "restore_preview", status: "completed", stage: "completed", restore_preview: "D:/ResearchData/backups/workspace/restore-previews/candidate" }) : response({ ...overview, active_job: { ...job, kind: "restore_preview" } }));
  render(<DataCenter fetcher={fetcher} onClose={vi.fn()} />);
  await screen.findByText(/当前工作台和原始 PDF 均未被替换/);
  expect(screen.getByText(/尚未切换为可用工作台/)).toBeVisible();
  expect(fetcher.mock.calls.some(c => String(c[0]).endsWith("/restore-preview"))).toBe(false);
});

it("keeps an action failure visible after the automatic state refresh", async () => {
  const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => url === "/api/session/current" ? response({ csrf_token: "csrf" }) : init?.method === "POST" ? response({ code: "backup_unsafe_path" }, 503) : response(overview));
  render(<DataCenter fetcher={fetcher} onClose={vi.fn()} />);
  await screen.findByText("D:/ResearchData");
  fireEvent.click(screen.getByRole("button", { name: "创建并核验备份" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("备份路径存在链接"));
  await waitFor(() => expect(fetcher.mock.calls.filter(c => c[0] === "/api/data/overview").length).toBeGreaterThan(1));
  expect(screen.getByRole("alert")).toHaveTextContent("备份路径存在链接");
});

it("shows retry when status cannot load instead of inventing data counts", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response({}, 503)).mockResolvedValueOnce(response(overview));
  render(<DataCenter fetcher={fetcher} onClose={vi.fn()} />);
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "创建并核验备份" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
  await screen.findByText("D:/ResearchData");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("verification uses the selected backup identity and blocks further tasks while working", async () => {
  let running = false;
  const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    if (url === "/api/session/current") return response({ csrf_token: "csrf" });
    if (init?.method === "POST") { running = true; return response({ ...job, id: "verify", kind: "verify", status: "running", stage: "verify" }, 202); }
    return response({ ...overview, backups: [backup], backups_total: 1, active_job: running ? { ...job, id: "verify", kind: "verify", status: "running", stage: "verify" } : null });
  });
  render(<DataCenter fetcher={fetcher} onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "重新核验" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "生成恢复副本" })).toBeDisabled());
  expect(fetcher.mock.calls.filter(c => c[1]?.method === "POST")[0]![0]).toBe("/api/data/backups/new-backup/verify");
});

it("explains unavailable backup storage while making clear that reading remains usable", async () => {
  const fetcher = vi.fn().mockResolvedValue(response({ code: "backup_storage_unavailable" }, 503));
  render(<DataCenter fetcher={fetcher} onClose={vi.fn()} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("阅读功能仍可使用");
  expect(screen.getByRole("alert")).toHaveTextContent("检查备份目录权限或链接后重启网站");
});

it("keeps complete workspace recovery separate from raw preview and never auto-opens it", async () => {
  const recovery = { ...job, id: "restored", kind: "recover_workspace", status: "completed", stage: "completed", recovery_ready: true, recovered_pdfs: 2, workspace_root: "D:/Recovery/data" };
  let done = false;
  const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    if (url === "/api/session/current") return response({ csrf_token: "csrf" });
    if (String(url).endsWith("/open")) return response({ requested: true }, 202);
    if (init?.method === "POST") { done = true; return response(recovery, 202); }
    return response({ ...overview, backups: [backup], backups_total: 1, recoveries: done ? [recovery] : [] });
  });
  render(<DataCenter fetcher={fetcher} onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "恢复为独立工作台" }));
  await screen.findByText(/独立工作台已就绪/);
  const open = await screen.findByRole("button", { name: "打开恢复工作台" });
  expect(fetcher.mock.calls.filter(call => String(call[0]).endsWith("/open"))).toHaveLength(0);
  fireEvent.click(open); fireEvent.click(open);
  await screen.findByText(/已请求打开独立恢复工作台/);
  expect(fetcher.mock.calls.filter(call => String(call[0]).endsWith("/open"))).toHaveLength(1);
  expect(fetcher.mock.calls.find(call => String(call[0]).endsWith("/open"))?.[1]?.headers).toMatchObject({ "X-LW-CSRF": "csrf" });
});

it("does not offer a complete workspace from a database-only backup", async () => {
  render(<DataCenter fetcher={vi.fn(async () => response({ ...overview, backups: [{ ...backup, include_pdfs: false }], backups_total: 1 }))} onClose={vi.fn()} />);
  await screen.findByRole("button", { name: "生成恢复副本" });
  expect(screen.queryByRole("button", { name: "恢复为独立工作台" })).not.toBeInTheDocument();
});

it("keeps a failed open request distinct from a successful recovery", async () => {
  const fetcher = vi.fn(async (url: RequestInfo | URL) => url === "/api/session/current" ? response({ csrf_token: "csrf" }) : String(url).endsWith("/open") ? response({ code: "recovery_invalid_workspace" }, 503) : response({ ...overview, recoveries: [{ ...job, id: "r", workspace_root: "D:/Recovery/data", recovered_pdfs: 2 }] }));
  render(<DataCenter fetcher={fetcher} onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "打开恢复工作台" }));
  expect(await screen.findByText(/恢复工作台尚未就绪、位置已变化/)).toBeVisible();
  expect(screen.queryByText(/已请求打开/)).not.toBeInTheDocument();
});

it("stops only the explicitly chosen recovered workspace and reports the response honestly", async () => {
  const fetcher = vi.fn(async (url: RequestInfo | URL) => url === "/api/session/current" ? response({ csrf_token: "csrf" }) : String(url).endsWith("/stop") ? response({ stopped: true }) : response({ ...overview, recoveries: [{ ...job, id: "specific-recovery", workspace_root: "D:/Recovery/data", recovered_pdfs: 2, unavailable_documents: 3 }] }));
  render(<DataCenter fetcher={fetcher} onClose={vi.fn()} />);
  await screen.findByText(/3 条历史或不可用条目未包含原文件/);
  expect(fetcher.mock.calls.some(call => String(call[0]).endsWith("/stop"))).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "关闭恢复工作台服务" }));
  await screen.findByText(/恢复工作台服务已关闭/);
  expect(fetcher.mock.calls.some(call => call[0] === "/api/data/recoveries/specific-recovery/stop")).toBe(true);
});

it("shows imported provenance and unverified coverage without offering recovery or changing current counts", async () => {
  const imported = { ...job, id: "external", kind: "import", status: "failed", stage: "failed", source_name: "外部备份.lwb.zip", error: "backup_schema_unsupported" };
  const fetcher = vi.fn(async (_url: RequestInfo | URL) => response({ ...overview, backups: [imported], backups_total: 1 }));
  render(<DataCenter fetcher={fetcher} onClose={vi.fn()} />);
  await screen.findByText(/导入：外部备份/);
  expect(screen.getByText(/覆盖范围待核验/)).toBeVisible();
  expect(screen.getByText(/数据库结构与当前版本不兼容或被改动/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "恢复为独立工作台" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "下载备份" })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.map(call => call[0]).sort()).toEqual(["/api/data/context", "/api/data/overview"]);
});

it("cancels the selected background import with CSRF and waits for its terminal state", async () => {
  const importing = { ...job, id: "external-import", kind: "import", status: "running", stage: "verify" };
  let cancelled = false;
  const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    if (url === "/api/session/current") return response({ csrf_token: "csrf" });
    if (init?.method === "POST") { cancelled = true; return response({ requested: true }); }
    if (String(url).startsWith("/api/data/jobs/")) return response(cancelled ? { ...importing, status: "failed", stage: "failed", error: "backup_cancelled" } : importing);
    return response({ ...overview, active_job: cancelled ? null : importing });
  });
  render(<DataCenter fetcher={fetcher} onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "取消当前数据任务" }));
  await screen.findByText(/任务已取消，当前工作台未被替换/);
  expect(fetcher.mock.calls.find(c => c[1]?.method === "POST")).toEqual([
    "/api/data/jobs/external-import/cancel", expect.objectContaining({ headers: { "X-LW-CSRF": "csrf" } }),
  ]);
  expect(screen.getByRole("button", { name: "创建并核验备份" })).toBeEnabled();
});
