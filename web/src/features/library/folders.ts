export type LibraryFolder = {
  library_id: string;
  display_path: string;
  recursive: boolean;
  document_count: number;
  scan_state: "idle" | "queued" | "running" | "completed" | "failed";
  last_synced_at: string | null;
  warnings: string[];
};
export type FolderSelection = { selection_token: string; display_path: string };
export type FolderApi = {
  list(signal?: AbortSignal): Promise<LibraryFolder[]>;
  select(): Promise<FolderSelection | null>;
  register(selection: FolderSelection, recursive: boolean): Promise<void>;
  scan(id: string): Promise<void>;
};

async function write(path: string, body?: unknown) {
  const session = await fetch("/api/session/current", { credentials: "same-origin" });
  if (!session.ok) throw new Error("session_required");
  const { csrf_token } = await session.json() as { csrf_token: string };
  const response = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-LW-CSRF": csrf_token },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({})) as { code?: string };
    throw new Error(data.code ?? "folder_request_failed");
  }
  return response;
}

export const browserFolderApi: FolderApi = {
  async list(signal) {
    const response = await fetch("/api/libraries", { credentials: "same-origin", signal });
    if (!response.ok) throw new Error("folder_list_failed");
    const data = await response.json() as { items: LibraryFolder[] };
    if (!Array.isArray(data.items)) throw new Error("folder_list_failed");
    return data.items;
  },
  async select() {
    const response = await write("/api/libraries/select-root");
    return response.status === 204 ? null : response.json() as Promise<FolderSelection>;
  },
  async register(selection, recursive) {
    await write("/api/libraries", { selection_token: selection.selection_token, recursive });
  },
  async scan(id) { await write(`/api/libraries/${encodeURIComponent(id)}/scan`); },
};

export function folderError(error: unknown): string {
  const code = error instanceof Error ? error.message : "";
  return ({
    overlapping_library_root: "这个目录与已关联目录重叠。如果已包含子文件夹，无需再次添加。",
    private_data_root_not_allowed: "不能关联网站自身的数据目录，请选择存放论文的文件夹。",
    invalid_selection_token: "选择已过期，请重新选择文件夹。",
    selection_identity_changed: "文件夹已变化或无法访问，请重新选择。",
    registered_root_identity_changed: "原文件夹已被替换，请核对目录后重试。",
    library_recursive_mismatch: "该文件夹已经关联，已有的子文件夹同步范围未改变。请返回列表查看设置。",
    unsafe_library_root: "无法安全读取该文件夹，请选择普通本地文件夹。",
    folder_dialog_failed: "文件夹选择窗口未能打开，请重试。",
    session_required: "会话已失效，请通过桌面入口重新打开网站。",
  } as Record<string, string>)[code] ?? "文件夹操作未完成，请重试。";
}
