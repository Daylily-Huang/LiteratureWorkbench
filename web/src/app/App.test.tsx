import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("../features/reader/PdfReader", () => ({
  PdfReader: () => null,
}));

import { App } from "./App";

describe("App", () => {
  it("renders the approved three-column workbench", () => {
    render(<App />);
    expect(screen.getByRole("complementary", { name: "文献库" })).toBeVisible();
    expect(screen.getByRole("main", { name: "PDF 阅读器" })).toBeVisible();
    expect(screen.getByRole("complementary", { name: "研究助手" })).toBeVisible();
  });
});
