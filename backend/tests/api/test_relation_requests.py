import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

from literature_workbench.features.research.relation_requests import RelationRequests, KIND
from literature_workbench.features.research.service import LibraryAnswer
from literature_workbench.features.settings.repository import SettingsRepository
from test_reading_graph import headers, seed
from test_relation_review import prepare


def payload(docs, **extra):
    return {
        "question": "Do designs explain estimates?",
        "document_ids": [d["document_id"] for d in docs],
        "request_id": str(uuid4()),
        **extra,
    }


def test_attempt_and_version_bound_cache_reuse_preserve_candidates_without_new_calls(
    client, authenticated, tmp_path, monkeypatch
):
    docs, h, _, calls = prepare(client, authenticated, tmp_path, monkeypatch)
    with client.app.state.database.connect() as c:
        first = c.execute("SELECT * FROM jobs WHERE kind=?", (KIND,)).fetchone()
        request_id = first["id"]
    again = client.post(
        "/api/workspace/analyses", headers=h, json=payload(docs, request_id=request_id)
    ).json()
    assert again["status"] == "completed" and again["cache_hit"] is True and len(calls) == 1
    reused = client.post("/api/workspace/analyses", headers=h, json=payload(docs)).json()
    assert reused["id"] == again["id"] and reused["cache_hit"] is True and len(calls) == 1
    assert len(client.get("/api/workspace/analyses").json()["items"]) == 1
    receipt = client.get("/api/workspace/analyses/requests/" + request_id).json()
    assert receipt["readonly"] and receipt["status"] == "completed"
    assert (
        client.post(
            "/api/workspace/analyses",
            headers=h,
            json=payload(docs, request_id=request_id, question="Different"),
        ).status_code
        == 409
    )
    assert len(calls) == 1
    forced = client.post(
        "/api/workspace/analyses", headers=h, json=payload(docs, force_refresh=True)
    ).json()
    assert forced["id"] != again["id"] and not forced["cache_hit"] and len(calls) == 2
    settings = SettingsRepository(client.app.state.database)
    settings.update(deepseek_model=settings.read().deepseek_model)
    same_settings = client.post("/api/workspace/analyses", headers=h, json=payload(docs)).json()
    assert same_settings["id"] == forced["id"] and len(calls) == 2
    settings.update(deepseek_model="new-model")
    changed = client.post("/api/workspace/analyses", headers=h, json=payload(docs)).json()
    assert changed["id"] != forced["id"] and len(calls) == 3
    with client.app.state.database.transaction() as c:
        c.execute("UPDATE documents SET status='missing' WHERE id=?", (docs[0]["document_id"],))
    assert (
        client.get("/api/workspace/analyses/requests/" + request_id).json()["source_changed"]
        is True
    )
    assert (
        client.post(
            "/api/workspace/analyses", headers=h, json=payload(docs, request_id=request_id)
        ).json()["id"]
        == again["id"]
    )
    assert len(calls) == 3 and not any(
        e["kind"] == "manual" for e in client.get("/api/workspace/graph").json()["edges"]
    )


def test_simultaneous_submissions_share_one_model_call_and_one_atomic_candidate_set(
    client, authenticated, tmp_path, monkeypatch
):
    docs, _ = seed(client, tmp_path)
    entered, release = Event(), Event()
    calls = []

    def model(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(8)
        return LibraryAnswer(
            answer=json.dumps({"summary": "Controlled empty analysis", "candidates": []}),
            citations=[],
            provider="test",
            model="test",
        )

    monkeypatch.setattr(client.app.state.research_service, "ask_library", model)
    h = headers(authenticated)
    first = payload(docs)
    second = payload(docs)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(client.post, "/api/workspace/analyses", headers=h, json=first)
        try:
            assert entered.wait(5)
            concurrent = client.post("/api/workspace/analyses", headers=h, json=second)
            assert concurrent.status_code == 202 and concurrent.json()["status"] == "running"
            same = client.post("/api/workspace/analyses", headers=h, json=first)
            assert same.status_code == 202 and len(calls) == 1
        finally:
            release.set()
        result = running.result(timeout=8).json()
    resolved = client.get("/api/workspace/analyses/requests/" + second["request_id"]).json()
    assert resolved["status"] == "completed" and resolved["id"] == result["id"]
    assert len(client.get("/api/workspace/analyses").json()["items"]) == 1 and len(calls) == 1


def test_failed_or_interrupted_attempt_is_never_silently_replayed(
    client, authenticated, tmp_path, monkeypatch
):
    docs, _ = seed(client, tmp_path)
    calls = []

    def bad(**kwargs):
        calls.append(kwargs)
        return LibraryAnswer(answer="bad JSON", citations=[], provider="test", model="test")

    monkeypatch.setattr(client.app.state.research_service, "ask_library", bad)
    h = headers(authenticated)
    body = payload(docs)
    assert client.post("/api/workspace/analyses", headers=h, json=body).status_code == 502
    assert client.post("/api/workspace/analyses", headers=h, json=body).json()["status"] == "failed"
    assert (
        client.post("/api/workspace/analyses", headers=h, json=payload(docs)).json()["status"]
        == "failed"
    )
    assert len(calls) == 1
    assert (
        client.post(
            "/api/workspace/analyses", headers=h, json=payload(docs, force_refresh=True)
        ).status_code
        == 502
    )
    assert len(calls) == 2
    interrupted = payload(docs, question="Interrupted question")
    ledger = RelationRequests(client.app.state.database, "old-process")
    assert ledger.reserve(
        interrupted["request_id"], interrupted["question"], interrupted["document_ids"], False
    )[0]
    check = client.get("/api/workspace/analyses/requests/" + interrupted["request_id"])
    assert check.json()["status"] == "unknown"
    fresh = payload(docs, question=interrupted["question"])
    assert (
        client.post("/api/workspace/analyses", headers=h, json=fresh).json()["status"] == "unknown"
        and len(calls) == 2
    )
    assert client.app.state.task_queue.requeue_expired() == 0
    for _ in range(10):
        leased = client.app.state.task_queue.lease_next(
            "ordinary-worker", 30, ["scan_library", "parse_pdf", "translate_selection"]
        )
        if leased is None:
            break
        assert leased.kind != KIND
        client.app.state.task_queue.complete(leased.id, "ordinary-worker")
    else:
        raise AssertionError("Unexpected unbounded ordinary work")
    assert (
        client.post(
            "/api/workspace/analyses",
            headers=h,
            json=payload(docs, question=interrupted["question"], force_refresh=True),
        ).status_code
        == 502
    )
    assert len(calls) == 3


def test_source_change_during_paid_analysis_prevents_storing_mixed_version_candidates(
    client, authenticated, tmp_path, monkeypatch
):
    docs, _ = seed(client, tmp_path)

    def changed(**kwargs):
        with client.app.state.database.transaction() as c:
            c.execute("UPDATE documents SET status='missing' WHERE id=?", (docs[0]["document_id"],))
        return LibraryAnswer(
            answer=json.dumps({"summary": "Do not store mixed versions", "candidates": []}),
            citations=[],
            provider="test",
            model="test",
        )

    monkeypatch.setattr(client.app.state.research_service, "ask_library", changed)
    body = payload(docs)
    result = client.post("/api/workspace/analyses", headers=headers(authenticated), json=body)
    assert result.status_code == 409 and result.json()["code"] == "analysis_source_changed"
    assert client.get("/api/workspace/analyses").json()["items"] == []
    assert (
        client.get("/api/workspace/analyses/requests/" + body["request_id"]).json()["status"]
        == "failed"
    )


def test_request_authorization_validation_and_old_analysis_detail(
    client, authenticated, tmp_path, monkeypatch
):
    docs, h, _, calls = prepare(client, authenticated, tmp_path, monkeypatch)
    body = payload(docs)
    assert (
        client.post(
            "/api/workspace/analyses", headers=h, json={**body, "request_id": "bad"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/workspace/analyses", headers=h, json={**body, "force_refresh": "true"}
        ).status_code
        == 422
    )
    del body["request_id"]
    assert client.post("/api/workspace/analyses", headers=h, json=body).status_code == 422
    value = client.get("/api/workspace/analyses").json()
    identifier = value["items"][0]["id"]
    assert len(value["workspace_id"]) == 64
    assert (
        client.get("/api/workspace/analyses/" + identifier).json()["items"][0]["id"] == identifier
    )
    assert client.get("/api/workspace/analyses/requests/" + str(uuid4())).status_code == 404
    client.cookies.clear()
    assert client.get("/api/workspace/analyses/requests/" + str(uuid4())).status_code == 403
    assert client.get("/api/workspace/analyses/" + identifier).status_code == 403
    assert len(calls) == 1


def test_selection_order_is_part_of_question_semantics(
    client, authenticated, tmp_path, monkeypatch
):
    docs, h, _, calls = prepare(client, authenticated, tmp_path, monkeypatch)
    with client.app.state.database.connect() as c:
        request_id = c.execute("SELECT id FROM jobs WHERE kind=?", (KIND,)).fetchone()["id"]
    original = client.get("/api/workspace/analyses/requests/" + request_id).json()
    reversed_docs = list(reversed(docs))
    conflict = client.post(
        "/api/workspace/analyses", headers=h, json=payload(reversed_docs, request_id=request_id)
    )
    assert conflict.status_code == 409 and len(calls) == 1
    reversed_result = client.post(
        "/api/workspace/analyses", headers=h, json=payload(reversed_docs)
    ).json()
    assert reversed_result["status"] == "completed" and not reversed_result["cache_hit"]
    assert reversed_result["id"] != original["id"] and len(calls) == 2
    assert not any(
        e["kind"] == "manual" for e in client.get("/api/workspace/graph").json()["edges"]
    )
