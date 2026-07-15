import type { ImportStatus } from "./dropFiles";

type Props = {
  fileName: string;
  status: "awaiting_confirmation" | ImportStatus;
  errorCode?: string | null;
  onConfirm(): void;
  onCancel(): void;
};

const labels: Record<Props["status"], string> = {
  awaiting_confirmation: "等待确认",
  receiving: "正在上传",
  uploaded: "上传完成",
  validating: "正在验证",
  ready_to_promote: "正在登记",
  completed: "导入完成",
  failed: "导入失败",
};

export function ImportConfirmDialog({ fileName, status, errorCode, onConfirm, onCancel }: Props) {
  return (
    <section role="dialog" aria-label={`导入 ${fileName}`}>
      <p>{fileName}</p>
      {status === "awaiting_confirmation" ? (
        <>
          <p>登记后，这篇 PDF 可按需自动用于云端处理</p>
          <button type="button" onClick={onConfirm}>确认导入</button>
          <button type="button" onClick={onCancel}>取消</button>
        </>
      ) : (
        <p>{status === "failed" ? `导入失败：${errorCode ?? "unknown_error"}` : labels[status]}</p>
      )}
    </section>
  );
}
