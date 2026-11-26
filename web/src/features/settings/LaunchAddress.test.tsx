import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { addressNotice, LaunchAddressDetails, parseLaunchAddress } from "./LaunchAddress";
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const address = { current_url: "http://127.0.0.1:59978/", previous_url: null, remembered: true, address_changed: false, warning: "" };
const response = (value: unknown) => new Response(JSON.stringify(value));

test("shows remembered local address and copies it without writing or probing other ports", async () => {
  const copy = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: copy } });
  const fetcher = vi.fn(async (_url: RequestInfo | URL) => response({ launch_address: address }));
  render(<LaunchAddressDetails fetcher={fetcher} />);
  await screen.findByText(address.current_url); expect(screen.getByText(/已记住当前地址/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "复制网站地址" }));
  await screen.findByText("网站地址已复制"); expect(copy).toHaveBeenCalledWith(address.current_url);
  expect(fetcher).toHaveBeenCalledOnce(); expect(fetcher.mock.calls[0]?.[0]).toBe("/api/data/context");
});

test("honestly explains changed origins and failed address memory without promising draft migration", async () => {
  const changed = { ...address, previous_url: "http://127.0.0.1:50001/", address_changed: true };
  render(<LaunchAddressDetails fetcher={vi.fn(async () => response({ launch_address: changed }))} />);
  await screen.findByText(changed.previous_url);
  expect(screen.getByRole("note")).toHaveTextContent("不会自动迁移");
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
  expect(addressNotice({ ...address, remembered: false })).toContain("下次启动可能变化");
  expect(addressNotice({ ...address, warning: "address_record_reset" })).toContain("记录已重建");
  expect(addressNotice(address)).toBe("");
});

test("ignores untrusted or unmanaged metadata and late responses after closing", async () => {
  for (const current_url of ["https://example.org/", "http://0.0.0.0:50001/", "http://127.0.0.1:99999/", "http://127.0.0.1:80/", "http://127.0.0.1:50001/path"]) expect(parseLaunchAddress({ ...address, current_url })).toBeNull();
  expect(parseLaunchAddress(null)).toBeNull();
  let complete!: (response: Response) => void;
  const fetcher = vi.fn(() => new Promise<Response>(resolve => { complete = resolve; }));
  const view = render(<LaunchAddressDetails fetcher={fetcher} />);
  await waitFor(() => expect(complete).toBeDefined()); view.unmount();
  complete(response({ launch_address: address }));
  await Promise.resolve(); expect(screen.queryByText(address.current_url)).not.toBeInTheDocument();
});
