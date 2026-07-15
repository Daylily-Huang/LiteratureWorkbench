import { afterEach, expect, test, vi } from "vitest";

import { ImportApiError, browserImportApi } from "./dropFiles";

afterEach(() => vi.unstubAllGlobals());

test("API error allowlist maps known codes and hides unknown server details", async () => {
  const file = new File(["%PDF-1.7"], "paper.pdf", { type: "application/pdf" });
  const fetch = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ code: "import_too_large" }), { status: 413 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ code: "database stack trace" }), { status: 500 }));
  vi.stubGlobal("fetch", fetch);

  await expect(browserImportApi.createImportIntent(file)).rejects.toMatchObject({
    code: "import_too_large",
  } satisfies Partial<ImportApiError>);
  await expect(browserImportApi.createImportIntent(file)).rejects.toMatchObject({
    code: "import_failed",
  } satisfies Partial<ImportApiError>);
});
