import { useState } from "react";
import { ModalDialog } from "./ModalDialog";

type ConfirmDialogProps = {
  confirmLabel?: string; description: string; expectedText?: string; label: string;
  onCancel(): void; onConfirm(): void;
};

export function ConfirmDialog({ confirmLabel = "确认", description, expectedText, label, onCancel, onConfirm }: ConfirmDialogProps) {
  const [value, setValue] = useState("");
  const enabled = expectedText === undefined || value === expectedText;
  return (
    <ModalDialog label={label} onClose={onCancel}>
      <div className="modal-dialog__header"><h2>{label}</h2><button aria-label={`关闭${label}`} className="icon-button" onClick={onCancel} type="button">×</button></div>
      <p>{description}</p>
      {expectedText !== undefined && <label className="field">输入文件名以确认<input onChange={(event) => setValue(event.target.value)} value={value} /></label>}
      <div className="dialog-actions">
        <button className="button button--ghost" onClick={onCancel} type="button">取消</button>
        <button className="button button--danger" disabled={!enabled} onClick={onConfirm} type="button">{confirmLabel}</button>
      </div>
    </ModalDialog>
  );
}
