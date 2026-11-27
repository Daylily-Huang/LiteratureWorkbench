CREATE TABLE reading_groups (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    document_ids_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE document_relations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES documents(id),
    target_id TEXT NOT NULL REFERENCES documents(id),
    relation TEXT NOT NULL CHECK(relation IN ('related','supports','contradicts','method')),
    note TEXT NOT NULL,
    source_page INTEGER NOT NULL CHECK(source_page > 0),
    target_page INTEGER NOT NULL CHECK(target_page > 0),
    source_content_id TEXT NOT NULL,
    target_content_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(source_id <> target_id)
);
