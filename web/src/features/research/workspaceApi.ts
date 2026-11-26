export async function workspaceRequest<T>(path: string, body?: unknown, method = "POST", signal?: AbortSignal): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) {
    const session = await fetch("/api/session/current", { credentials: "same-origin", signal });
    if (!session.ok) throw new Error("session_required");
    const value = await session.json() as { csrf_token: string };
    headers["X-LW-CSRF"] = value.csrf_token;
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(`/api/workspace/${path}`, {
    signal, credentials: "same-origin", method: body === undefined ? "GET" : method,
    headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({})) as { code?: string };
    throw new Error(error.code ?? "workspace_unavailable");
  }
  return response.json() as Promise<T>;
}
