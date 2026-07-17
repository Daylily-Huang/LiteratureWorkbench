import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Bootstrap } from "./Bootstrap";

describe("Bootstrap", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("clears the fragment before posting the one-use token in the body", async () => {
    const token = "one-use-bootstrap-token";
    window.history.replaceState(null, "", `/bootstrap#${token}`);
    const historySpy = vi.spyOn(window.history, "replaceState");
    const localStorageSpy = vi.spyOn(Storage.prototype, "setItem");
    const sessionStorageSpy = vi.spyOn(window.sessionStorage, "setItem");
    const fetchSpy = vi.spyOn(window, "fetch").mockResolvedValue(
      new Response(null, { status: 204 }),
    );
    const navigate = vi.fn();

    await act(async () => render(<Bootstrap navigate={navigate} />));

    expect(historySpy).toHaveBeenCalledWith(null, "", "/bootstrap");
    expect(window.location.hash).toBe("");
    expect(fetchSpy).toHaveBeenCalledWith("/api/session/bootstrap", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    expect(historySpy.mock.invocationCallOrder[0]).toBeLessThan(
      fetchSpy.mock.invocationCallOrder[0] ?? Number.MAX_SAFE_INTEGER,
    );
    expect(fetchSpy.mock.calls[0]?.[0]).not.toContain(token);
    expect(localStorageSpy).not.toHaveBeenCalled();
    expect(sessionStorageSpy).not.toHaveBeenCalled();
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/"));
    expect(document.body.textContent).not.toContain(token);
  });

  it("does not post when the fragment is absent", async () => {
    window.history.replaceState(null, "", "/bootstrap");
    const fetchSpy = vi.spyOn(window, "fetch");

    render(<Bootstrap />);

    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("shows an expired-link message when the server rejects the one-use token", async () => {
    window.history.replaceState(null, "", "/bootstrap#expired-token");
    vi.spyOn(window, "fetch").mockResolvedValue(new Response(null, { status: 403 }));

    render(<Bootstrap />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "启动链接已失效，请从启动器重新打开",
    );
    expect(screen.queryByRole("button", { name: "重试建立会话" })).not.toBeInTheDocument();
  });

  it("retries transient failures with the scrubbed token kept only in memory", async () => {
    const user = userEvent.setup();
    const token = "memory-only-bootstrap-token";
    window.history.replaceState(null, "", `/bootstrap#${token}`);
    const fetchSpy = vi.spyOn(window, "fetch")
      .mockRejectedValueOnce(new TypeError("network unavailable"))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const localStorageSpy = vi.spyOn(Storage.prototype, "setItem");
    const sessionStorageSpy = vi.spyOn(window.sessionStorage, "setItem");
    const navigate = vi.fn();

    render(<Bootstrap navigate={navigate} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法建立本机会话");
    expect(window.location.hash).toBe("");
    await user.click(screen.getByRole("button", { name: "重试建立会话" }));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/"));
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(fetchSpy.mock.calls[1]?.[1]).toMatchObject({
      body: JSON.stringify({ token }),
    });
    expect(localStorageSpy).not.toHaveBeenCalled();
    expect(sessionStorageSpy).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain(token);
  });
});
