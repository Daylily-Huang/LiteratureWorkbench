import { useEffect, useRef, useState } from "react";
import type { Job } from "./DataCenter";

export const MAX_BACKUP_UPLOAD = 100 * 1024 ** 3 + 512 * 1024 ** 2;
type Upload = (file: File, csrf: string, signal: AbortSignal, progress: (loaded: number) => void) => Promise<Job>;
export const uploadBackup: Upload = (file, csrf, signal, progress) => new Promise((resolve, reject) => {
  if (signal.aborted) { reject(new DOMException("Aborted", "AbortError")); return; }
  const request = new XMLHttpRequest();
  const abort = () => request.abort();
  const cleanup = () => signal.removeEventListener("abort", abort);
  request.open("POST", "/api/data/backups/import");
  request.responseType = "json"; request.withCredentials = true;
  request.setRequestHeader("Content-Type", "application/octet-stream");
  request.setRequestHeader("X-LW-CSRF", csrf);
  request.setRequestHeader("X-LW-Backup-Filename", encodeURIComponent(file.name));
  request.upload.onprogress = event => progress(Math.min(file.size, event.loaded));
  request.onerror = () => { cleanup(); reject(new Error("backup_upload_incomplete")); };
  request.onabort = () => { cleanup(); reject(new DOMException("Aborted", "AbortError")); };
  request.onload = () => {
    cleanup();
    const value = request.response as (Job & { code?: string }) | null;
    if (request.status !== 202 || !value?.id || value.kind !== "import") reject(new Error(value?.code ?? "backup_upload_incomplete"));
    else resolve(value);
  };
  signal.addEventListener("abort", abort, { once: true });
  request.send(file);
});

export function BackupImport({ disabled, fetcher, onJob, onBusy, message, upload = uploadBackup }: {
  disabled: boolean; fetcher: typeof fetch; onJob(job: Job): void; onBusy(busy: boolean): void;
  message(code: string): string; upload?: Upload;
}) {
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [progress, setProgress] = useState(0);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; controller.current?.abort(); }; }, []);
  async function select(files: FileList | null) {
    if (!files?.length || disabled || controller.current) return;
    setError(""); setNotice("");
    if (files.length !== 1) { setError("请一次选择一份备份文件。"); return; }
    const file = files[0]!;
    if (!file.name.toLowerCase().endsWith(".lwb.zip")) { setError("请选择本站导出的 .lwb.zip 备份文件。"); return; }
    if (!file.size || file.size > MAX_BACKUP_UPLOAD) { setError("备份文件为空或超过约 100 GB 的导入上限。"); return; }
    const request = new AbortController(); controller.current = request;
    setName(file.name); setProgress(0); setBusy(true); onBusy(true);
    try {
      const response = await fetcher("/api/session/current", { credentials: "same-origin", signal: request.signal });
      if (!response.ok) throw new Error("session_required");
      const { csrf_token } = await response.json() as { csrf_token: string };
      if (typeof csrf_token !== "string" || !csrf_token) throw new Error("session_required");
      if (request.signal.aborted) throw new DOMException("Aborted", "AbortError");
      const result = await upload(file, csrf_token, request.signal, loaded => {
        if (mounted.current && !request.signal.aborted) setProgress(Math.round(loaded / file.size * 100));
      });
      if (mounted.current && !request.signal.aborted) { setProgress(100); setNotice("文件已传输，后台核验结果见下方任务状态；关闭窗口后核验仍会继续。"); onJob(result); }
    } catch (reason) {
      if (mounted.current) {
        if (request.signal.aborted || (reason instanceof DOMException && reason.name === "AbortError")) setNotice("已中止本次传输请求。可刷新状态确认是否已完成接收；当前工作台未被替换。");
        else setError(message(reason instanceof Error ? reason.message : "backup_upload_incomplete"));
      }
    } finally {
      controller.current = null;
      if (mounted.current) { setBusy(false); onBusy(false); }
    }
  }
  return <section className="data-center__section backup-import"><h3>导入已有备份</h3>
    <p>选择你自己保存的本站备份（.lwb.zip）。先传输并核验，之后再手动恢复；不会自动替换当前库或调用 AI。</p>
    <div className="backup-import__drop" onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); void select(event.dataTransfer.files); }}>
      <input ref={input} type="file" accept=".lwb.zip,application/zip" aria-label="导入备份文件" hidden disabled={disabled || busy} onChange={event => { void select(event.target.files); event.target.value = ""; }} />
      <button type="button" disabled={disabled || busy} onClick={() => input.current?.click()}>选择备份文件</button><span>也可将一份备份文件拖到这里</span>
    </div>
    {name && <p className="backup-import__filename">{name}</p>}
    {busy && <div role="status"><label>传输 {progress}%<progress aria-label="备份传输进度" max={100} value={progress} /></label><button type="button" onClick={() => controller.current?.abort()}>取消传输</button><small>传输期间请保持窗口打开；传完后可继续阅读。</small></div>}
    {notice && <p role="status">{notice}</p>}{error && <p role="alert" className="notice notice--danger">{error}</p>}
  </section>;
}
