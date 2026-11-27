import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { DocumentTree } from "./DocumentTree";
import type { DocumentListItem } from "./documents";

afterEach(cleanup);

function document(
  document_id: string,
  filename: string,
  relative_folder: string,
  authorization_source = "registered_library",
): DocumentListItem {
  return { document_id, filename, relative_folder, authorization_source };
}

test("groups local documents by nested folders and keeps direct imports separate", () => {
  render(
    <DocumentTree
      activeDocumentId={null}
      documents={[
        document("nested", "10-paper.pdf", "生态学/相机陷阱"),
        document("nested-2", "2-paper.pdf", "生态学/相机陷阱"),
        document("root", "root.pdf", ""),
        document("import", "uploaded.pdf", "ignored/internal", "confirmed_import"),
      ]}
      onOpenDocument={() => undefined}
    />,
  );

  expect(screen.getByText("生态学")).toBeInTheDocument();
  expect(screen.getByText("相机陷阱")).toBeInTheDocument();
  expect(screen.getByText("根目录")).toBeInTheDocument();
  expect(screen.getByText("直接导入")).toBeInTheDocument();
  expect(screen.queryByText("ignored")).not.toBeInTheDocument();

  const cameraFolder = screen.getByText("相机陷阱").closest("details");
  expect(cameraFolder).not.toBeNull();
  const buttons = within(cameraFolder as HTMLElement).getAllByRole("button", { hidden: true });
  expect(buttons.map((button) => button.textContent)).toEqual(["2-paper.pdf", "10-paper.pdf"]);
});

test("falls unsafe folder metadata back to the root group", () => {
  render(
    <DocumentTree
      activeDocumentId={null}
      documents={[
        document("parent", "parent.pdf", "../private"),
        document("slash", "slash.pdf", "private\\hidden"),
        document("empty", "empty.pdf", "safe//hidden"),
      ]}
      onOpenDocument={() => undefined}
    />,
  );

  const root = screen.getByText("根目录").closest("details");
  expect(root).not.toBeNull();
  expect(within(root as HTMLElement).getAllByRole("button", { hidden: true })).toHaveLength(3);
  expect(screen.queryByText("private")).not.toBeInTheDocument();
  expect(screen.queryByText("safe")).not.toBeInTheDocument();
});

test("opens the active path, marks the active PDF, and forwards clicks", async () => {
  const user = userEvent.setup();
  const onOpenDocument = vi.fn();
  render(
    <DocumentTree
      activeDocumentId="active"
      documents={[
        document("active", "active.pdf", "研究/当前"),
        document("next", "next.pdf", "研究/当前"),
        document("other", "other.pdf", "研究/其他"),
      ]}
      onOpenDocument={onOpenDocument}
    />,
  );

  expect(screen.getByText("研究").closest("details")).toHaveAttribute("open");
  expect(screen.getByText("当前").closest("details")).toHaveAttribute("open");
  expect(screen.getByText("其他").closest("details")).not.toHaveAttribute("open");
  expect(screen.getByRole("button", { name: "active.pdf" })).toHaveAttribute(
    "aria-current",
    "page",
  );

  await user.click(screen.getByRole("button", { name: "next.pdf" }));
  expect(onOpenDocument).toHaveBeenCalledWith("next");
});

test("counts all descendant papers rather than child folders", () => {
  render(<DocumentTree activeDocumentId={null} documents={[
    document("a", "a.pdf", "研究/方法"), document("b", "b.pdf", "研究/方法"),
    document("c", "c.pdf", "研究/数据"),
  ]} onOpenDocument={() => undefined} />);
  expect(screen.getByText("研究").querySelector("small")).toHaveTextContent("3");
});


test("keeps same-named source folders separate by library identity", () => {
  render(<DocumentTree activeDocumentId={null} expandAll documents={[
    {...document("first", "same.pdf", "methods"), library_id:"one",library_name:"papers"},
    {...document("second", "same.pdf", "methods"), library_id:"two",library_name:"papers"},
  ]} onOpenDocument={()=>{}} />);
  const roots=screen.getAllByText("papers");
  expect(roots).toHaveLength(2);
  for(const root of roots) expect(within(root.closest("details")!).getAllByRole("button")).toHaveLength(1);
});
