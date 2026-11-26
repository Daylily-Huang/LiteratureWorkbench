import { useEffect, useRef, useState } from "react";

export type LaunchAddress = { current_url: string; previous_url: string | null; remembered: boolean; address_changed: boolean; warning: string };
function localUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const match = /^http:\/\/127\.0\.0\.1:(\d{1,5})\/$/.exec(value);
  if (!match) return false;
  const port = Number(match[1]);
  return port >= 1024 && port <= 65535;
}
export function parseLaunchAddress(value: unknown): LaunchAddress | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Partial<LaunchAddress>;
  if (!localUrl(item.current_url) || typeof item.remembered !== "boolean" || typeof item.address_changed !== "boolean") return null;
  const previous = localUrl(item.previous_url) ? item.previous_url : null;
  return { current_url: item.current_url, previous_url: previous, remembered: item.remembered,
    address_changed: item.address_changed && !!previous && previous !== item.current_url,
    warning: ["address_record_reset", "address_record_unavailable"].includes(item.warning ?? "") ? item.warning! : "" };
}
export function addressNotice(address: LaunchAddress): string {
  if (address.address_changed) return "上次地址暂不可用，本次已使用新地址。旧地址的未同步草稿不会自动迁移；如原标签页仍打开，请先保存或导出。";
  if (!address.remembered) return "本次地址暂时无法记住，下次启动可能变化。请从桌面图标进入，未同步草稿先保存或导出。";
  if (address.warning === "address_record_reset") return "启动地址记录已重建。旧地址的未同步草稿不会自动迁移，请先保存或导出仍打开的草稿。";
  return "";
}
export function LaunchAddressDetails({ fetcher = fetch }: { fetcher?: typeof fetch }) {
  const [address, setAddress] = useState<LaunchAddress | null>(null);
  const [notice, setNotice] = useState("");
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true; const controller = new AbortController();
    void Promise.resolve().then(() => fetcher("/api/data/context", { credentials: "same-origin", signal: controller.signal })).then(async response => {
      if (!response.ok) return;
      const value = await response.json() as { launch_address?: unknown };
      if (!controller.signal.aborted) setAddress(parseLaunchAddress(value.launch_address));
    }).catch(() => {});
    return () => { mounted.current = false; controller.abort(); };
  }, [fetcher]);
  if (!address) return null;
  return <section className="data-center__section"><h3>网站地址</h3><p><code>{address.current_url}</code> <button type="button" onClick={async () => {
    try { await navigator.clipboard.writeText(address.current_url); if (mounted.current) setNotice("网站地址已复制"); }
    catch { if (mounted.current) setNotice("复制未成功，请选中地址复制。"); }
  }}>复制网站地址</button></p><small>{address.remembered ? "已记住当前地址，正常重启会优先复用。浏览器草稿仍限同一浏览器和地址。" : "当前地址可正常阅读，记住地址的状态暂不可用。"}</small>{address.previous_url && <p>上次地址：<code>{address.previous_url}</code></p>}{addressNotice(address) && <p role="note">{addressNotice(address)}</p>}{notice && <p role="status">{notice}</p>}</section>;
}
