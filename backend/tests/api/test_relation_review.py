from uuid import uuid4
import json
from concurrent.futures import ThreadPoolExecutor

from test_reading_graph import headers, seed

from literature_workbench.features.research.service import LibraryAnswer, LibraryCitation


def prepare(client, authenticated, tmp_path, monkeypatch):
    docs, _ = seed(client, tmp_path)
    source = LibraryCitation(document_id=docs[0]["document_id"], display_name=docs[0]["filename"], physical_page=2,
                             content_id=docs[0]["content_id"], excerpt="Repeated sampling improves detection estimates under this design.")
    target = LibraryCitation(document_id=docs[1]["document_id"], display_name=docs[1]["filename"], physical_page=3,
                             content_id=docs[1]["content_id"], excerpt="Single visit sampling cannot separate detection from abundance.")
    proposal = {"source_ref": "[d1 r1 p2]", "target_ref": "[d2 r1 p3]", "source_quote": source.excerpt,
                "target_quote": target.excerpt, "relation": "method", "difference_basis": "method",
                "rationale": "Different sampling designs may explain the estimates.", "comparison_note": "Repeated versus single visit.",
                "caveats": ["Check populations and observation conditions."]}
    captured = []

    def analyze(**kwargs):
        captured.append(kwargs)
        return LibraryAnswer(answer=json.dumps({"summary": "A design comparison", "candidates": [proposal]}),
                             citations=[], provider="controlled-test", model="test", source_fragments={"[d1 r1 p2]": source, "[d2 r1 p3]": target})

    monkeypatch.setattr(client.app.state.research_service, "ask_library", analyze)
    h = headers(authenticated)
    result = client.post("/api/workspace/analyses", headers=h, json={"request_id": str(uuid4()), "question": "Do designs explain estimates?", "document_ids": [d["document_id"] for d in docs]})
    assert result.status_code == 200
    return docs, h, proposal, captured


def review_body(candidate):
    return {"revision": candidate["revision"], "confirmed": True, "relation": "related", "difference_basis": "context",
            "note": "User corrected: conditional association, not proven contradiction.", "comparison_note": "Compare detection conditions first."}


def test_candidates_stay_out_of_graph_until_explicit_atomic_confirmation(client, authenticated, tmp_path, monkeypatch):
    docs, h, original, captured = prepare(client, authenticated, tmp_path, monkeypatch)
    analysis = client.get("/api/workspace/analyses").json()["items"][0]
    candidate = analysis["candidates"][0]
    assert captured == [{"question": "Do designs explain estimates?", "document_ids": [d["document_id"] for d in docs], "relation_analysis": True}]
    assert not analysis["coverage"][2]["retrieved"] and not analysis["coverage"][2]["has_candidate"]
    assert not any(e["kind"] in ("manual", "question_link") for e in client.get("/api/workspace/graph").json()["edges"])
    path = f"/api/workspace/candidates/{candidate['id']}/accept"
    body = review_body(candidate)
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, headers=h, json={**body, "confirmed": False}).status_code == 422
    assert client.post(path, headers=h, json={**body, "confirmed": "true"}).status_code == 422
    assert client.post(path, headers=h, json={**body, "source_id": docs[2]["document_id"]}).status_code == 422
    assert client.post(path, headers=h, json={**body, "revision": 99}).status_code == 409
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post(path, headers=h, json=body), range(2)))
    assert all(r.status_code == 200 for r in results)
    assert len({r.json()["relation_id"] for r in results}) == 1
    graph = client.get("/api/workspace/graph").json()
    relations = [e for e in graph["edges"] if e["kind"] == "manual"]
    assert len(relations) == 1
    relation = relations[0]
    assert relation["origin"] == "ai_confirmed" and relation["relation"] == "related"
    assert relation["source_page"] == 2 and relation["target_page"] == 3
    assert relation["evidence"]["ai_proposal"]["rationale"] == original["rationale"]
    assert relation["evidence"]["review"]["note"] == body["note"]
    assert relation["evidence"]["source"]["excerpt"] == original["source_quote"]
    assert client.post(path.replace("/accept", "/reject"), headers=h, json={"revision": 1}).status_code == 409
    assert client.delete(f"/api/workspace/relations/{relation['relation_id']}", headers=h).status_code == 200
    retained = client.get("/api/workspace/analyses").json()["items"][0]["candidates"][0]
    assert retained["status"] == "accepted" and retained["relation_id"] is None
    assert retained["proposal"]["rationale"] == original["rationale"]
    assert client.get("/api/documents").json()["total"] == 3


def test_changed_sources_block_acceptance_and_rejection_never_adds_graph_edges(client, authenticated, tmp_path, monkeypatch):
    docs, h, _, _ = prepare(client, authenticated, tmp_path, monkeypatch)
    candidate = client.get("/api/workspace/analyses").json()["items"][0]["candidates"][0]
    path = f"/api/workspace/candidates/{candidate['id']}"
    with client.app.state.database.transaction() as c:
        c.execute("UPDATE documents SET status='missing' WHERE id=?", (docs[1]["document_id"],))
    assert client.get("/api/workspace/analyses").json()["items"][0]["candidates"][0]["source_changed"]
    assert client.post(path + "/accept", headers=h, json=review_body(candidate)).json()["code"] == "source_changed"
    assert client.post(path + "/reject", headers=h, json={"revision": 0}).status_code == 200
    assert client.post(path + "/reject", headers=h, json={"revision": 0}).status_code == 200
    assert client.post(path + "/accept", headers=h, json=review_body(candidate)).status_code == 409
    assert not any(e["kind"] == "manual" for e in client.get("/api/workspace/graph").json()["edges"])
    client.cookies.clear()
    assert client.get("/api/workspace/analyses").status_code == 403


def test_invalid_model_output_does_not_create_candidate_or_graph(client, authenticated, tmp_path, monkeypatch):
    docs, _ = seed(client, tmp_path)
    monkeypatch.setattr(client.app.state.research_service, "ask_library", lambda **_: LibraryAnswer(answer="not JSON", citations=[], provider="test", model="test"))
    h = headers(authenticated)
    body = {"request_id": str(uuid4()), "question": "Compare", "document_ids": [d["document_id"] for d in docs]}
    assert client.post("/api/workspace/analyses", headers=h, json=body).json()["code"] == "invalid_relation_analysis"
    assert client.get("/api/workspace/analyses").json()["items"] == []
    assert client.post("/api/workspace/analyses", headers=h, json={**body, "document_ids": [docs[0]["document_id"]] * 2}).status_code == 422
