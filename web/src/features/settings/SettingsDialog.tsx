import { useEffect, useState } from "react";


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
    <div aria-label="翻译设置" aria-modal="true" className="settings-dialog" role="dialog">
      <div className="settings-dialog__header">
        <h2>翻译设置</h2>
        <button aria-label="关闭设置" onClick={onClose} type="button">×</button>
      </div>
      {settings === null && !error && <p role="status">正在读取设置…</p>}
      {error && <p role="alert">设置暂时无法保存</p>}
      {settings !== null && (
        <>
          <p>DeepSeek key：{settings.deepseek_key_configured ? "已配置" : "未配置"}</p>
          <label>
            替换 DeepSeek API key
            <input
              autoComplete="new-password"
              onChange={(event) => setKey(event.target.value)}
              type="password"
              value={key}
            />
          </label>
          <button disabled={key === ""} onClick={() => void replaceKey()} type="button">保存新 key</button>
          <label>
            DeepSeek 模型
            <input onChange={(event) => setModel(event.target.value)} value={model} />
          </label>
          <button disabled={model === settings.deepseek_model || model === ""} onClick={() => void patch({ deepseek_model: model })} type="button">
            保存模型
          </button>
          <label>
            <input
              checked={settings.qwen_fallback_enabled}
              onChange={(event) => void patch({ qwen_fallback_enabled: event.target.checked })}
              type="checkbox"
            />
            启用 Qwen 本地备用
          </label>
        </>
      )}
    </div>
  );
}
