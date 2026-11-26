import { ModalDialog } from "../ui/ModalDialog";
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

const errorLabels: Record<string, string> = {
  import_failed: "导入未完成，请重试",
  invalid_pdf_extension: "请选择 PDF 文件",
  content_length_mismatch: "文件大小不一致",
  upload_size_mismatch: "文件大小不一致",
  invalid_pdf_signature: "文件不是有效的 PDF",
  import_too_large: "PDF 超过 2 GiB 限制",
  unsupported_media_type: "请选择 PDF 文件",
  import_not_found: "导入授权已失效，请重新选择文件",
};

export function ImportConfirmDialog({ fileName, status, errorCode, onConfirm, onCancel }: Props) {
  if (status === "awaiting_confirmation") return (
    <ModalDialog label={`导入 ${fileName}`} onClose={onCancel}>
      <div className="modal-dialog__header"><h2>导入文献</h2><button type="button" className="icon-button" aria-label="关闭导入确认" onClick={onCancel}>×</button></div>
      <p className="import-filename">{fileName}</p>
      <p>登记后，这篇 PDF 可按需自动用于云端处理</p>
      <div className="modal-dialog__actions"><button className="button button--ghost" type="button" onClick={onCancel}>取消</button><button className="button button--primary" type="button" onClick={onConfirm}>确认导入</button></div>
    </ModalDialog>
  );
  return (
    <section role="status" aria-label={`导入 ${fileName}`}>
      <p>{fileName}</p>
      <p>{status === "failed" ? errorLabels[errorCode ?? ""] ?? errorLabels.import_failed : labels[status]}</p>
    </section>
  );
}
