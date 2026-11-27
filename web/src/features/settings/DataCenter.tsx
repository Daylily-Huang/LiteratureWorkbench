import { useEffect, useRef, useState } from "react";
import { ModalDialog } from "../ui/ModalDialog";
import { BackupImport } from "./BackupImport";
import { LaunchAddressDetails } from "./LaunchAddress";

export type Job = { id: string; kind: "create" | "import" | "verify" | "restore_preview" | "recover_workspace"; status: "uploading" | "queued" | "running" | "completed" | "failed";
  stage: string; processed_files: number; error?: string; restore_preview?: string; workspace_root?: string; recovery_ready?: boolean; created_at?: string; recovered_pdfs?: number; unavailable_documents?: number; duplicate_of?: string; received_bytes?: number; expected_bytes?: number; source_name?: string; source_created_at?: string };
type Backup = Job & { created_at: string; include_pdfs?: boolean; size_bytes?: number; pdf_count?: number;
  file_count?: number; verified_at?: string; verification_status?: string; archive_available?: boolean; archive_sha256?: string };
type Overview = { data_root: string; backup_directory: string; active_pdf_count: number; counts: Record<string, number>;
  backups: Backup[]; backups_total: number; active_job: Job | null; recoveries?: Job[] };
const errors: Record<string, string> = {
  backup_busy: "已有数据任务正在进行，请等待完成。", backup_interrupted: "上次任务因关闭服务而中断，原数据未被替换。可以重新创建备份。",
  backup_source_changed: "备份时原文发生变化，请等待批注或文件同步完成后重试。", backup_hash_mismatch: "备份内容与校验记录不一致，请保留此文件并从原数据重新创建备份。",
  backup_manifest_invalid: "备份格式或文件清单核验未通过。", backup_database_invalid: "备份数据库完整性检查未通过。",
  backup_unsafe_path: "备份路径存在链接或不受支持的目录，请检查本地数据位置。", backup_not_found: "此备份文件已不存在，请刷新状态。",
  backup_too_large: "备份超过本次大小或文件数量上限，请先备份工作台记录，并另外备份原始 PDF。",
  backup_io_error: "文件读取或写入未完成，请检查来源是否可用、磁盘空间是否充足后重试。",
  backup_recovery_missing: "原数据中的批注恢复文件缺失或不一致，无法创建完整备份。可先备份工作台数据库，再检查批注恢复文件。",
  backup_storage_unavailable: "备份目录暂不可用，阅读功能仍可使用。请检查备份目录权限或链接后重启网站。",
  recovery_full_backup_required: "独立恢复需要包含 PDF 的完整备份；数据库备份只能生成供核对的恢复副本。",
  recovery_invalid_workspace: "恢复工作台尚未就绪、位置已变化或文件路径核对未通过。请从完整备份重新恢复。",
  recovery_not_ready: "这份恢复尚未完成，请等待任务完成后再打开。",
  recovery_launch_failed: "打开恢复工作台未成功，请检查文件权限后重试。",
  recovery_stop_failed: "未能关闭恢复工作台，请保存其中的编辑后重试。原工作台仍可使用。",
  backup_file_type_invalid: "请选择本站导出的 .lwb.zip 备份文件。",
  backup_upload_incomplete: "备份传输未完成或已中断。当前库未被替换，请重新选择文件重试。",
  backup_cancelled: "任务已取消，当前工作台未被替换。可重新导入或执行恢复。",
  backup_schema_unsupported: "备份数据库结构与当前版本不兼容或被改动，未导入为可用备份。请使用匹配的工作台版本重新导出；保留原文件。",
  session_required: "登录已过期，请用桌面图标重新打开工作台后再试。",
};
function message(code: string) { return errors[code] ?? "数据状态暂时无法读取，请重试。"; }
function size(bytes = 0) { return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MB` : `${Math.max(0.1, bytes / 1024).toFixed(1)} KB`; }
const stages: Record<string, string> = { uploading: "传输备份文件", queued: "等待开始", snapshot: "保存一致数据快照", copying: "复制文献与工作台文件", verify: "核对文件与数据库", reconnecting: "连接副本文献与批注资料", completed: "已完成", failed: "未完成" };

export function DataCenter({ onClose, fetcher = fetch }: { onClose(): void; fetcher?: typeof fetch }) {
  const [data, setData] = useState<Overview | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [includePdfs, setIncludePdfs] = useState(true);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelNotice, setCancelNotice] = useState("");
  useEffect(() => { setCancelNotice(""); }, [job?.id]);
  const [error, setError] = useState("");
  const [operationError, setOperationError] = useState("");
  const [copyNotice, setCopyNotice] = useState("");
  const [retry, setRetry] = useState(0);
  const [opening, setOpening] = useState("");
  const [openNotice, setOpenNotice] = useState("");
  const mounted = useRef(true);
  const actionLock = useRef(false);
  const active = starting || uploading || job?.status === "queued" || job?.status === "running" || job?.status === "uploading";
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    const controller = new AbortController(); let valid = true;
    setLoading(true);
    fetcher("/api/data/overview", { credentials: "same-origin", signal: controller.signal }).then(async response => {
      if (!response.ok) { const problem = await response.json().catch(() => ({})) as { code?: string }; throw new Error(problem.code ?? "data_unavailable"); }
      const value = await response.json() as Overview;
      if (valid) {
        setData(value);
        if (value.active_job) setJob(current => current?.id === value.active_job!.id || current?.status === "queued" || current?.status === "running" ? current : value.active_job);
        setError("");
      }
    }).catch(reason => { if (valid) setError(reason instanceof Error && errors[reason.message] ? message(reason.message) : "数据状态暂时无法读取，请刷新重试；正在运行的后台任务不会因此取消。"); })
      .finally(() => { if (valid) setLoading(false); });
    return () => { valid = false; controller.abort(); };
  }, [fetcher, retry]);
  useEffect(() => {
    if (!job || !["queued", "running", "uploading"].includes(job.status)) return;
    const controller = new AbortController(); let valid = true; let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const response = await fetcher(`/api/data/jobs/${job!.id}`, { credentials: "same-origin", signal: controller.signal });
        if (!response.ok) throw new Error("job_unavailable");
        const value = await response.json() as Job;
        if (!valid) return;
        setJob(value);
        if (value.status === "completed" || value.status === "failed") { setRetry(n => n + 1); return; }
        setError(""); timer = setTimeout(() => void poll(), 1000);
      } catch {
        if (valid) { setError("暂时无法读取进度，任务可能仍在后台运行。正在重试，也可以刷新状态。"); timer = setTimeout(() => void poll(), 3000); }
      }
    }
    timer = setTimeout(() => void poll(), 300);
    return () => { valid = false; controller.abort(); clearTimeout(timer); };
  }, [job?.id, job?.status === "queued" || job?.status === "running" || job?.status === "uploading", fetcher, retry]);

  async function cancelJob() {
    if (!job || cancelling || actionLock.current) return;
    actionLock.current = true; setCancelling(true);
    try {
      const response = await fetcher("/api/session/current", { credentials: "same-origin" });
      if (!response.ok) throw new Error("session_required");
      const { csrf_token } = await response.json() as { csrf_token: string };
      const stopped = await fetcher(`/api/data/jobs/${job.id}/cancel`, { method: "POST", credentials: "same-origin", headers: { "X-LW-CSRF": csrf_token } });
      if (!stopped.ok) throw new Error("backup_io_error");
      if (mounted.current) { setCancelNotice("已请求取消，正在等待当前文件操作结束。"); setRetry(n => n + 1); }
    } catch (reason) { if (mounted.current) setOperationError(message(reason instanceof Error ? reason.message : "")); }
    finally { actionLock.current = false; if (mounted.current) setCancelling(false); }
  }

  async function start(path: string, body: unknown = {}) {
    if (actionLock.current || active) return;
    actionLock.current = true; setStarting(true); setError(""); setOperationError("");
    try {
      const current = await fetcher("/api/session/current", { credentials: "same-origin" });
      if (!current.ok) throw new Error("session_required");
      const { csrf_token } = await current.json() as { csrf_token: string };
      const response = await fetcher(path, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-LW-CSRF": csrf_token }, body: JSON.stringify(body) });
      const value = await response.json() as Job & { code?: string };
      if (!response.ok) throw new Error(value.code ?? "data_unavailable");
      if (mounted.current) { setJob(value); setRetry(n => n + 1); }
    } catch (reason) { if (mounted.current) { setOperationError(message(reason instanceof Error ? reason.message : "")); setRetry(n => n + 1); } }
    finally { actionLock.current = false; if (mounted.current) setStarting(false); }
  }
  async function copyPath(path: string) {
    try { await navigator.clipboard.writeText(path); setCopyNotice("位置已复制"); }
    catch { setCopyNotice("复制未成功，请选中路径复制。"); }
  }
  async function openRecovery(id: string, action: "open" | "stop" = "open") {
    if (actionLock.current) return;
    actionLock.current = true; setOpening(id); setOpenNotice("");
    try {
      const session = await fetcher("/api/session/current", { credentials: "same-origin" });
      if (!session.ok) throw new Error("session_required");
      const { csrf_token } = await session.json() as { csrf_token: string };
      const response = await fetcher(`/api/data/recoveries/${id}/${action}`, { method: "POST", credentials: "same-origin", headers: { "X-LW-CSRF": csrf_token } });
      const result = await response.json() as { code?: string; requested?: boolean; stopped?: boolean };
      if (!response.ok || (action === "open" && !result.requested) || (action === "stop" && typeof result.stopped !== "boolean")) throw new Error(result.code ?? (action === "open" ? "recovery_launch_failed" : "recovery_stop_failed"));
      if (mounted.current) setOpenNotice(action === "stop" ? result.stopped ? "恢复工作台服务已关闭。原工作台仍可使用，之后可再次打开该副本。" : "当前没有可验证为正在运行的恢复工作台，未停止其他进程。" : "已请求打开独立恢复工作台，请稍候查看新窗口。若没有打开，可重试；启动日志位于该恢复目录的 runtime/launcher/recovery-start.log。");
    } catch (reason) { if (mounted.current) setOpenNotice(message(reason instanceof Error ? reason.message : "")); }
    finally { actionLock.current = false; if (mounted.current) setOpening(""); }
  }
  return <ModalDialog label="数据与备份" onClose={onClose}><section className="data-center">
    <header className="notebook-heading"><div><span className="eyebrow">本地数据</span><h2>数据与备份</h2><p>保存研究积累，并确认备份可以读取。</p></div><button className="button button--ghost" type="button" onClick={onClose}>关闭</button></header>
    <div className="data-center__body">
      <div className="data-center__toolbar"><button className="button button--ghost" type="button" disabled={loading} onClick={() => setRetry(n => n + 1)}>刷新状态</button><small>{uploading ? "传输中请保留窗口；关闭会中止传输，原数据不受影响。" : "关闭此窗口后，后台数据任务仍会继续。"}</small></div>
      {(operationError || error) && <p className="notice notice--danger" role="alert">{operationError || error}</p>}
      {loading && !data && <p role="status">正在读取本地数据…</p>}
      {data && <>
        <LaunchAddressDetails fetcher={fetcher} />
        <BackupImport disabled={active || loading} fetcher={fetcher} message={message} onBusy={value => { setUploading(value); if (!value) setRetry(n => n + 1); }} onJob={value => { setJob(value); setRetry(n => n + 1); }} />
        <div className="data-center__stats">{[["文献", "documents"], ["研究记录", "research_entries"], ["确认关系", "document_relations"], ["研读缓存", "full_text_study_cache"], ["翻译缓存", "translation_cache"]].map(([label, key]) => <div key={key}><strong>{data.counts[key!] ?? 0}</strong><span>{label}</span></div>)}</div>
        <section className="data-center__section"><h3>数据保存位置</h3><p><code>{data.data_root}</code> <button type="button" onClick={() => void copyPath(data.data_root)}>复制位置</button></p><small>笔记、研读缓存和图谱保存在本地数据库；PDF 批注写入对应原文件。关联文件夹中的 PDF 仍在原文件夹。</small>{copyNotice && <p role="status">{copyNotice}</p>}</section>
        <section className="data-center__section"><h3>创建备份</h3><label className="data-center__option"><input type="checkbox" checked={includePdfs} disabled={active} onChange={e => setIncludePdfs(e.target.checked)} /> 包含原始 PDF 与批注恢复文件（推荐）</label><p>{includePdfs ? `保存工作台数据库、导入文件、解析资料、批注恢复文件，并核对 ${data.active_pdf_count} 篇活动文献的原始 PDF。来源变化或不可读取时会停止并提示。` : "只保存工作台数据库：包含笔记、图谱、阅读状态与 AI 缓存，不包含 PDF 文件、批注恢复文件或解析附件。"}</p><small>备份在本机完成，不调用 AI，不包含 API 密钥、模型文件、运行会话或旧备份。完成后建议下载一份到其他磁盘。</small><div className="data-center__toolbar"><button className="button button--primary" type="button" disabled={active || loading} onClick={() => void start("/api/data/backups", { include_pdfs: includePdfs })}>{starting ? "正在开始…" : "创建并核验备份"}</button><small>备份位置：{data.backup_directory}</small></div></section>
      </>}
      {job && <div className="data-center__job" role="status"><strong>{job.kind === "create" ? "备份" : job.kind === "import" ? "导入备份" : job.kind === "verify" ? "核验" : job.kind === "recover_workspace" ? "独立工作台恢复" : "恢复演练"}：{stages[job.stage] ?? "处理中"}</strong>{job.status === "uploading" && <p>{job.source_name} · 已接收 {size(job.received_bytes)} / {size(job.expected_bytes)}</p>}{job.status === "running" && <p>已处理 {job.processed_files} 个文件。可以继续阅读文献。</p>}{job.status === "failed" && <p>{message(job.error ?? "")}</p>}{job.status === "completed" && <p>{job.kind === "import" ? <>{job.duplicate_of ? "同一份备份已存在，核验通过后复用现有文件，未重复保存。" : "备份已导入并通过文件与数据库核验。"}请在下方核对覆盖范围，再手动恢复。</> : job.recovery_ready ? <>独立工作台已就绪，{job.recovered_pdfs} 篇 PDF 已连接到副本。原工作台未被替换；请在下方打开并核对。</> : job.restore_preview ? <>恢复副本已生成并核验：<code>{job.restore_preview}</code>。当前工作台和原始 PDF 均未被替换；该副本保留原路径引用，尚未切换为可用工作台。</> : "文件校验与数据库完整性检查通过。"}</p>}{cancelNotice && <p>{cancelNotice}</p>}{!["completed", "failed"].includes(job.status) && !uploading && <button type="button" disabled={cancelling} onClick={() => void cancelJob()}>{cancelling ? "正在请求取消…" : "取消当前数据任务"}</button>}</div>}
      {!!data?.recoveries?.length && <section className="data-center__section"><h3>已恢复的独立工作台</h3><p>每份都有自己的 PDF、批注、笔记和缓存。打开后确认顶部“恢复工作台”标记；编辑只作用于该副本。API 密钥不在备份中，在新机器上需重新配置。</p><ul className="data-center__list">{data.recoveries.map(item => <li key={item.id}><div><strong>{item.created_at ? new Date(item.created_at).toLocaleString() : "恢复工作台"} · {item.recovered_pdfs} 篇 PDF</strong><code>{item.workspace_root}</code>{!!item.unavailable_documents && <small>{item.unavailable_documents} 条历史或不可用条目未包含原文件，保留记录但不能直接阅读。</small>}</div><div className="data-center__backup-actions"><button type="button" disabled={!!opening || active} onClick={() => void openRecovery(item.id)}>{opening === item.id ? "正在处理…" : "打开恢复工作台"}</button><button type="button" onClick={() => void copyPath(item.workspace_root ?? "")}>复制位置</button><button type="button" disabled={!!opening || active} onClick={() => void openRecovery(item.id, "stop")}>关闭恢复工作台服务</button></div></li>)}</ul>{openNotice && <p role="status">{openNotice}</p>}</section>}
      {data && <section className="data-center__section"><h3>备份记录</h3><p className="data-center__muted">共 {data.backups_total} 条，显示最近 50 条。包括在本站创建或导入核验的备份。导入不会自动恢复当前库。</p>{!data.backups.length && <p>还没有在此处创建备份。创建后可下载、重新核验，并在独立目录生成恢复副本。</p>}<ul className="data-center__list">{data.backups.map(backup => <li key={backup.id}><div><strong>{new Date(backup.created_at).toLocaleString()}</strong>{backup.kind === "import" && <small>导入：{backup.source_name} · 备份内时间：{backup.source_created_at ? new Date(backup.source_created_at).toLocaleString() : "未知"}</small>}<small>{backup.include_pdfs === true ? "工作台与 PDF" : backup.include_pdfs === false ? "仅工作台数据库" : "覆盖范围待核验"} · {backup.status === "completed" ? `${size(backup.size_bytes)} · ${backup.pdf_count ?? 0} 篇 PDF` : stages[backup.stage]}</small><small>{backup.verification_status === "failed" ? "最近核验未通过，请检查备份文件" : backup.verified_at ? `核验通过：${new Date(backup.verified_at).toLocaleString()}` : backup.error ? message(backup.error) : "完成后自动核验"}{backup.status === "completed" && !backup.archive_available && " · 备份文件已不可用"}</small></div><div className="data-center__backup-actions">{backup.status === "completed" && backup.archive_available && <><a className="button button--ghost" href={`/api/data/backups/${backup.id}/download`} download>下载备份</a><button type="button" disabled={active} onClick={() => void start(`/api/data/backups/${backup.id}/verify`)}>重新核验</button><button type="button" disabled={active} onClick={() => void start(`/api/data/backups/${backup.id}/restore-preview`)}>生成恢复副本</button>{backup.include_pdfs && <button type="button" disabled={active} onClick={() => void start(`/api/data/backups/${backup.id}/recover-workspace`)}>恢复为独立工作台</button>}</>}</div></li>)}</ul></section>}
    </div>
  </section></ModalDialog>;
}
