import { useEffect, useRef, useState } from "react";
import { ModalDialog } from "../ui/ModalDialog";
import { browserFolderApi, folderError, type FolderApi, type FolderSelection, type LibraryFolder } from "./folders";

export function FolderManager({ onClose, onChanged, api = browserFolderApi }: {
  onClose(): void; onChanged(): void; api?: FolderApi;
}) {
  const [folders, setFolders] = useState<LibraryFolder[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [selection, setSelection] = useState<FolderSelection | null>(null);
  const [recursive, setRecursive] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const action = useRef(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    const refresh = async () => {
      try {
        const next = await api.list(controller.signal);
        if (!controller.signal.aborted) { setFolders(next); setLoadError(false); }
      } catch { if (!controller.signal.aborted) setLoadError(true); }
      finally {
        if (!controller.signal.aborted) { setLoading(false); timer = window.setTimeout(() => void refresh(), 3000); }
      }
    };
    void refresh();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [api, attempt]);

  async function run(label: string, task: () => Promise<void>) {
    if (action.current) return;
    action.current = true; setBusy(label); setError(""); setNotice("");
    try { await task(); }
    catch (reason) { if (alive.current) setError(folderError(reason)); }
    finally { action.current = false; if (alive.current) setBusy(""); }
  }

  function close() { if (!action.current) onClose(); }
  return <ModalDialog label="管理本地文件夹" onClose={close}>
    <div className="modal-dialog__header folder-manager__heading"><h2>本地文件夹</h2><button className="icon-button" type="button" aria-label="关闭文件夹管理" disabled={!!busy} onClick={close}>×</button></div>
    <p className="empty-copy">关联后自动收录 PDF，文件保留在原位置。网站运行时自动同步，重启后会补扫关闭期间的变化。</p>
    {loadError && <div role="alert">暂时无法更新文件夹状态。<button type="button" className="button button--ghost" onClick={() => setAttempt(a => a + 1)}>重试读取</button></div>}
    {loading ? <p role="status">正在读取文件夹…</p> : !folders.length && !loadError ? <p>还没有关联的文件夹，点击下方按钮添加。</p> : null}
    <div className="folder-manager__list">{folders.map(folder => <article className="folder-manager__item" key={folder.library_id}>
      <strong className="folder-manager__path">{folder.display_path}</strong>
      <p>{folder.document_count} 篇 PDF · {folder.recursive ? "包含子文件夹" : "仅当前文件夹"}</p>
      <p role="status">{folder.scan_state === "queued" ? "等待扫描…" : folder.scan_state === "running" ? "正在扫描…" : folder.scan_state === "failed" ? "扫描失败，请检查文件夹是否可访问后重试。" : folder.warnings.length ? "同步有遗漏，请检查文件访问权限后重新扫描。" : folder.last_synced_at ? "已同步 · 自动监听变化" : "等待首次同步"}</p>
      {folder.last_synced_at && <small>最近同步：{new Date(folder.last_synced_at).toLocaleString("zh-CN")}</small>}
      <button aria-label={`重新扫描 ${folder.display_path}`} className="button button--ghost" type="button" disabled={!!busy || folder.scan_state === "queued" || folder.scan_state === "running"} onClick={() => void run(folder.library_id, async () => {
        await api.scan(folder.library_id);
        if (alive.current) { setNotice("已安排重新扫描，文献列表会自动更新。"); setAttempt(a => a + 1); onChanged(); }
      })}>重新扫描</button>
    </article>)}</div>
    {selection && <div className="folder-manager__confirm">
      <h3>确认关联这个文件夹</h3><p className="folder-manager__path">{selection.display_path}</p>
      <label><input type="checkbox" checked={recursive} disabled={!!busy} onChange={event => setRecursive(event.target.checked)} /> 包含子文件夹</label>
      <p className="empty-copy">只关联你希望纳入文献库的目录。阅读批注仍会保存到原 PDF。</p>
      <div className="dialog-actions"><button className="button button--ghost" type="button" disabled={!!busy} onClick={() => { setSelection(null); setError(""); }}>取消关联</button><button className="button" type="button" disabled={!!busy} onClick={() => void run("register", async () => {
        try { await api.register(selection, recursive); }
        catch (reason) { if (alive.current) setSelection(null); throw reason; }
        if (alive.current) { setSelection(null); setNotice("文件夹已关联，文献将自动出现。"); setAttempt(a => a + 1); onChanged(); }
      })}>{busy === "register" ? "正在关联…" : "确认关联"}</button></div>
    </div>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {!selection && <button className="button folder-manager__add" type="button" disabled={!!busy} onClick={() => void run("select", async () => {
      const next = await api.select(); if (alive.current) { setSelection(next); setRecursive(true); }
    })}>{busy === "select" ? "请在系统窗口选择文件夹…" : "＋ 添加本地文件夹"}</button>}
  </ModalDialog>;
}
