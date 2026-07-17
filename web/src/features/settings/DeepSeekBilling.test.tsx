import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DeepSeekBilling } from "./DeepSeekBilling";


const billing = {
  available: true,
  balances: [{ currency: "CNY", total_balance: "12.50", granted_balance: "2.50", topped_up_balance: "10" }],
  site_usage: { currency: "CNY", today: "0.01", month: "0.12", total: "0.34", started_at: "2026-07-17T00:00:00Z", pricing_unknown_requests: 0 },
  refreshed_at: "2026-07-17T06:00:00Z",
  stale: false,
  error_code: null,
};


describe("DeepSeekBilling", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  });

  it("separates official balance from locally estimated usage", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(billing)));
    render(<DeepSeekBilling fetcher={fetcher} />);

    const trigger = await screen.findByRole("button", { name: /DeepSeek.*12\.50/ });
    fireEvent.click(trigger);

    expect(screen.getByRole("region", { name: "DeepSeek 用量" })).toHaveTextContent("余额由官方接口返回");
    expect(screen.getByRole("region", { name: "DeepSeek 用量" })).toHaveTextContent("本站估算消费");
    expect(screen.getByRole("region", { name: "DeepSeek 用量" })).toHaveTextContent("不含其他应用及此前消费");
  });

  it("uses the force-refresh endpoint from the popover", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(billing)));
    render(<DeepSeekBilling fetcher={fetcher} />);
    fireEvent.click(await screen.findByRole("button", { name: /DeepSeek.*12\.50/ }));
    const refresh = screen.getByRole("button", { name: "刷新 DeepSeek 余额" });
    await waitFor(() => expect(refresh).toBeEnabled());
    fireEvent.click(refresh);

    await waitFor(() => expect(fetcher).toHaveBeenCalledWith(
      "/api/settings/deepseek-billing?refresh=true",
      { credentials: "same-origin" },
    ));
  });

  it("does not poll while the page is hidden and refreshes when visible", async () => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(billing)));
    render(<DeepSeekBilling fetcher={fetcher} pollMs={5} />);
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => window.setTimeout(resolve, 20));
    expect(fetcher).toHaveBeenCalledTimes(1);

    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    fireEvent(document, new Event("visibilitychange"));
    await waitFor(() => expect(fetcher.mock.calls.length).toBeGreaterThan(1));
  });
});
