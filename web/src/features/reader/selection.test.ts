import { describe, expect, it } from "vitest";

import { viewportPointToPdfPoint } from "./selection";


describe("viewportPointToPdfPoint", () => {
  it("inverts scaled and vertically flipped PDF.js coordinates", () => {
    expect(viewportPointToPdfPoint(
      { x: 30, y: 60 },
      [2, 0, 0, -2, 10, 100],
    )).toEqual({ x: 10, y: 20 });
  });

  it("inverts a rotated PDF.js viewport transform", () => {
    expect(viewportPointToPdfPoint(
      { x: 40, y: 20 },
      [0, 2, 2, 0, 0, 0],
    )).toEqual({ x: 10, y: 20 });
  });
});
