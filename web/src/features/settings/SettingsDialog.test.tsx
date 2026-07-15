import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SettingsDialog } from "./SettingsDialog";


describe("SettingsDialog", () => {
  afterEach(cleanup);

  it("never echoes the stored key and updates only bounded settings", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        deepseek_key_configured: true,
        deepseek_base_url: "https://api.deepseek.com",
        deepseek_model: "deepseek-v4-flash",
        qwen_fallback_enabled: false,
        default_translation_mode: "online",
      })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        deepseek_key_configured: true, deepseek_model: "deepseek-v4-flash",
        qwen_fallback_enabled: true, default_translation_mode: "online",
      })));
    render(<SettingsDialog fetcher={fetcher} onClose={() => undefined} />);

    expect(await screen.findByText(/DeepSeek key：已配置/)).toBeVisible();
    const key = screen.getByLabelText("替换 DeepSeek API key");
    expect(key).toHaveValue("");
    fireEvent.change(key, { target: { value: "replacement-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "保存新 key" }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));
    expect(JSON.stringify(fetcher.mock.calls)).not.toContain("existing-secret");

    fireEvent.click(screen.getByRole("checkbox", { name: "启用 Qwen 本地备用" }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(5));
    const patchBody = JSON.parse(String((fetcher.mock.calls[4]?.[1] as RequestInit).body));
    expect(patchBody).toEqual({ qwen_fallback_enabled: true });
    expect(screen.getByLabelText("DeepSeek 模型")).toHaveValue("deepseek-v4-flash");
  });

  it("clears a stale save error after a later successful save", async () => {
    const settings = {
      deepseek_key_configured: false,
      deepseek_model: "deepseek-v4-flash",
      qwen_fallback_enabled: false,
      default_translation_mode: "online",
    };
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(settings)))
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(null, { status: 500 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    render(<SettingsDialog fetcher={fetcher} onClose={() => undefined} />);
    const key = await screen.findByLabelText("替换 DeepSeek API key");
    fireEvent.change(key, { target: { value: "replacement-secret" } });

    fireEvent.click(screen.getByRole("button", { name: "保存新 key" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "保存新 key" }));

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(5));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
