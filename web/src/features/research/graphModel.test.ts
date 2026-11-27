import { expect, test } from "vitest";
import { neighbors, scopeGraph, searchGraph, type GraphData } from "./graphModel";
const graph: GraphData = { history_truncated: false, nodes: [
  { id:"doc:a", kind:"document",label:"A" }, { id:"doc:b",kind:"document",label:"B" }, {id:"topic:t",kind:"topic",label:"T"}, {id:"question:q",kind:"question",label:"Q"},
],edges:[{id:"1",source:"doc:a",target:"topic:t",kind:"tag",label:"tag"},{id:"2",source:"doc:b",target:"topic:t",kind:"tag",label:"tag"},{id:"3",source:"question:q",target:"doc:a",kind:"citation",label:"citation"},{id:"4",source:"question:q",target:"doc:b",kind:"citation",label:"citation"}] };
test("scoping preserves connected questions and concepts without importing unselected papers",()=>{
  const scoped=scopeGraph(graph,["a"]);
  expect(scoped.nodes.map(n=>n.id).sort()).toEqual(["doc:a","question:q","topic:t"]);
  expect(scoped.edges.map(e=>e.id)).toEqual(["1","3"]);
  expect(scopeGraph(graph,["a"],"evidence").edges.map(e=>e.id)).toEqual(["3"]);
  expect([...neighbors(graph,"topic:t")].sort()).toEqual(["doc:a","doc:b","topic:t"]);
});

test("evidence view removes organizational-only nodes and scopes human question links",()=>{
  const data:GraphData={...graph,edges:[...graph.edges,{id:"manual-question",source:"question:q",target:"doc:a",kind:"question_link",label:"人工证据对照"}]};
  expect(scopeGraph(data,[],"evidence").nodes.some(n=>n.kind==="topic")).toBe(false);
  expect(scopeGraph(data,["a"],"evidence").nodes.map(n=>n.id).sort()).toEqual(["doc:a","question:q"]);
  expect(scopeGraph(data,["a"],"evidence").edges.some(e=>e.id==="manual-question")).toBe(true);
});

test("paper search keeps its direct evidence context without pulling in further papers", () => {
  const result = searchGraph({ ...graph, nodes: graph.nodes.map(n => n.id === "doc:a" ? { ...n, label: "Alpha Paper" } : n) }, "Alpha Paper");
  expect(result.nodes.map(n => n.id).sort()).toEqual(["doc:a", "question:q", "topic:t"]);
  expect(result.edges.map(e => e.id)).toEqual(["1", "3"]);
});

test("relation search matches combined fields and stays within the selected papers", () => {
  const data: GraphData = { ...graph, edges: [...graph.edges, { id: "pair", source: "doc:a", target: "doc:b", kind: "manual", relation: "contradicts", label: "存在分歧", question: "Sampling effort", comparison_note: "Adults versus juveniles", source_changed: true }] };
  const result = searchGraph(data, "SAMPLING juveniles", "contradicts", "changed");
  expect(result.edges.map(e => e.id)).toEqual(["pair"]);
  expect(result.nodes.map(n => n.id)).toEqual(["doc:a", "doc:b"]);
  expect(searchGraph(scopeGraph(data, ["a"]), "juveniles").nodes).toEqual([]);
  expect(searchGraph(data, "", "supports").edges).toEqual([]);
  expect(searchGraph(data, "", "all", "unchanged").edges.some(e => e.id === "pair")).toBe(false);
  expect(data.edges).toHaveLength(5);
});

test("original quotations are searchable while preserving evidence and version flags", () => {
  const citation = { document_id: "a", display_name: "A", physical_page: 3, excerpt: "Nonuniform encounter probability", rects: [], content_id: "historical-a", source_changed: true };
  const data: GraphData = { ...graph, edges: [{ id: "evidence", source: "doc:a", target: "doc:b", kind: "manual", label: "支持", relation: "supports", evidence: { source: citation } }] };
  const result = searchGraph(data, "encounter probability", "supports");
  expect(result.edges[0]?.evidence?.source).toEqual(citation);
  expect(result.nodes.map(n => n.id)).toEqual(["doc:a", "doc:b"]);
  expect(searchGraph(data, "unmatched").edges).toEqual([]);
});
