import { useCallback, useEffect, useRef, useState } from "react";

type Balance = { currency: string; total_balance: string; granted_balance: string; topped_up_balance: string };
export type BillingStatus = {
  available: boolean; balances: Balance[];
  site_usage: { currency: string; today: string; month: string; total: string; started_at: string | null; pricing_unknown_requests: number };
  refreshed_at: string | null; stale: boolean; error_code: string | null;
};
type DeepSeekBillingProps = { fetcher?: typeof fetch; pollMs?: number };

function money(currency: string, value: string): string {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return `${currency} ${value}`;
  return new Intl.NumberFormat("zh-CN", { style: "currency", currency, minimumFractionDigits: 2, maximumFractionDigits: amount > 0 && amount < 0.01 ? 6 : 2 }).format(amount);
}

function billingError(code: string | null): string {
  const messages: Record<string, string> = {
    deepseek_key_unavailable: "尚未配置 DeepSeek API Key",
    credential_store_failed: "Windows 凭据暂时无法读取",
    deepseek_authorization_failed: "DeepSeek API Key 未获授权",
    deepseek_balance_unavailable: "DeepSeek 账户余额不足或不可用",
    deepseek_balance_transport_failed: "暂时无法连接 DeepSeek",
    invalid_deepseek_balance_response: "DeepSeek 返回了无法识别的余额数据",
  };
  return messages[code ?? ""] ?? "余额暂时无法刷新";
}

export function DeepSeekBilling({ fetcher = fetch, pollMs = 60_000 }: DeepSeekBillingProps) {
  const [status, setStatus] = useState<BillingStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const alive = useRef(true);
  const refresh = useCallback(async (force = false) => {
    setLoading(true);
    try {
      const response = await fetcher(`/api/settings/deepseek-billing${force ? "?refresh=true" : ""}`, { credentials: "same-origin" });
      if (!response.ok) throw new Error("billing_unavailable");
      const value = await response.json() as BillingStatus;
      if (alive.current && value.site_usage !== undefined && Array.isArray(value.balances)) setStatus(value);
    } finally { if (alive.current) setLoading(false); }
  }, [fetcher]);
  useEffect(() => {
    alive.current = true;
    void refresh().catch(() => undefined);
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void refresh().catch(() => undefined); }, pollMs);
    const updated = () => void refresh().catch(() => undefined);
    const visibility = () => { if (document.visibilityState === "visible") void refresh().catch(() => undefined); };
    window.addEventListener("deepseek-usage-updated", updated);
    window.addEventListener("deepseek-credentials-updated", updated);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      alive.current = false; window.clearInterval(timer);
      window.removeEventListener("deepseek-usage-updated", updated);
      window.removeEventListener("deepseek-credentials-updated", updated);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [pollMs, refresh]);
  const balance = status?.balances.find((item) => item.currency === "CNY") ?? status?.balances[0];
  const compact = balance === undefined ? status?.error_code === "deepseek_key_unavailable" ? "DeepSeek 未配置" : "DeepSeek --" : `DeepSeek ${money(balance.currency, balance.total_balance)}`;
  return (
    <div className="billing">
      <button aria-expanded={open} className="billing__trigger" onClick={() => { setOpen((value) => !value); if (!open) void refresh().catch(() => undefined); }} type="button">
        <span className={`status-dot${status?.available ? " status-dot--ok" : ""}`} />{compact}
      </button>
      {open && <section aria-label="DeepSeek 用量" className="billing__popover">
        <div className="billing__header"><div><strong>DeepSeek 账户</strong><small>余额由官方接口返回</small></div><button aria-label="刷新 DeepSeek 余额" className="icon-button" disabled={loading} onClick={() => void refresh(true).catch(() => undefined)} type="button">↻</button></div>
        {balance !== undefined ? <><div className="billing__balance">{money(balance.currency, balance.total_balance)}</div><dl className="billing__grid"><div><dt>充值余额</dt><dd>{money(balance.currency, balance.topped_up_balance)}</dd></div><div><dt>赠送余额</dt><dd>{money(balance.currency, balance.granted_balance)}</dd></div></dl></> : <p className="notice notice--warning">{billingError(status?.error_code ?? null)}</p>}
        {status !== null && <div className="billing__usage"><strong>本站估算消费</strong><small>自启用统计起，不含其他应用及此前消费</small><dl className="billing__grid"><div><dt>今日</dt><dd>{money("CNY", status.site_usage.today)}</dd></div><div><dt>本月</dt><dd>{money("CNY", status.site_usage.month)}</dd></div><div><dt>累计</dt><dd>{money("CNY", status.site_usage.total)}</dd></div></dl>{status.site_usage.pricing_unknown_requests > 0 && <p className="notice notice--warning">有 {status.site_usage.pricing_unknown_requests} 次未知模型请求未估价</p>}</div>}
        <small className="billing__time">{status?.stale ? "数据可能已过期 · " : ""}{status?.refreshed_at ? `更新于 ${new Date(status.refreshed_at).toLocaleTimeString("zh-CN")}` : "尚未成功刷新"}</small>
      </section>}
    </div>
  );
}
