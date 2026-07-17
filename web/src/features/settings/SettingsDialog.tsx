import { useEffect, useState } from "react";

import { ModalDialog } from "../ui/ModalDialog";


type Fetcher = typeof fetch;
type PublicSettings = {
  deepseek_key_configured: boolean;
  deepseek_model: string;
  qwen_fallback_enabled: boolean;
  default_translation_mode: "online" | "local_only";
};

type SettingsDialogProps = {
  onClose(): void;
  fetcher?: Fetcher;
};


async function csrf(fetcher: Fetcher): Promise<string> {
  const response = await fetcher("/api/session/current", { credentials: "same-origin" });
  if (!response.ok) throw new Error("session_unavailable");
  return ((await response.json()) as { csrf_token: string }).csrf_token;
}


export function SettingsDialog({ onClose, fetcher = fetch }: SettingsDialogProps) {
  const [settings, setSettings] = useState<PublicSettings | null>(null);
  const [key, setKey] = useState("");
  const [model, setModel] = useState("");
  const [error, setError] = useState(false);

  useEffect(() => {
    let active = true;
    void fetcher("/api/settings", { credentials: "same-origin" })
      .then(async (response) => {
        if (!response.ok) throw new Error("settings_unavailable");
        const value = await response.json() as PublicSettings;
        if (active) {
          setSettings(value);
          setModel(value.deepseek_model);
          setError(false);
        }
      })
      .catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, [fetcher]);

  async function replaceKey(): Promise<void> {
    if (key === "") return;
    try {
      const token = await csrf(fetcher);
      const response = await fetcher("/api/settings/deepseek-key", {
        method: "PUT",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-LW-CSRF": token },
        body: JSON.stringify({ api_key: key }),
      });
      if (!response.ok) throw new Error("key_update_failed");
      setKey("");
      setSettings((current) => current && { ...current, deepseek_key_configured: true });
      setError(false);
      window.dispatchEvent(new Event("deepseek-credentials-updated"));
    } catch {
      setError(true);
    }
  }

  async function patch(update: Partial<Pick<PublicSettings, "deepseek_model" | "qwen_fallback_enabled">>): Promise<void> {
    try {
      const token = await csrf(fetcher);
      const response = await fetcher("/api/settings", {
        method: "PATCH",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-LW-CSRF": token },
        body: JSON.stringify(update),
      });
      if (!response.ok) throw new Error("settings_update_failed");
      const value = await response.json() as PublicSettings;
      setSettings(value);
      setModel(value.deepseek_model);
      setError(false);
    } catch {
      setError(true);
    }
  }

  return (
    <ModalDialog label="翻译设置" onClose={onClose}>
      <section className="settings-dialog">
        <div className="modal-dialog__header">
          <div><span className="eyebrow">模型与隐私</span><h2>翻译设置</h2></div>
          <button aria-label="关闭设置" className="icon-button" onClick={onClose} type="button">×</button>
        </div>
        {settings === null && !error && <p role="status">正在读取设置…</p>}
        {error && <p className="notice notice--danger" role="alert">设置暂时无法保存</p>}
        {settings !== null && <>
          <div className="settings-status"><span className={`status-dot${settings.deepseek_key_configured ? " status-dot--ok" : ""}`} /><span>DeepSeek key：{settings.deepseek_key_configured ? "已配置" : "未配置"}</span><strong>{settings.deepseek_key_configured ? "已安全配置" : "未配置"}</strong></div>
          <label className="field">替换 DeepSeek API key<input autoComplete="new-password" onChange={(event) => setKey(event.target.value)} placeholder="仅写入 Windows 凭据存储" type="password" value={key} /></label>
          <button className="button button--primary" disabled={key === ""} onClick={() => void replaceKey()} type="button">保存新 key</button>
          <label className="field">DeepSeek 模型<input onChange={(event) => setModel(event.target.value)} value={model} /></label>
          <button className="button button--secondary" disabled={model === settings.deepseek_model || model === ""} onClick={() => void patch({ deepseek_model: model })} type="button">保存模型</button>
          <label className="switch-field"><input checked={settings.qwen_fallback_enabled} onChange={(event) => void patch({ qwen_fallback_enabled: event.target.checked })} type="checkbox" /><span>启用 Qwen 本地备用</span></label>
          <p className="settings-dialog__privacy">PDF 仅在已登记文献库范围内按需云处理；API Key 不会发送到浏览器。</p>
        </>}
      </section>
    </ModalDialog>
  );
}
