CREATE TABLE IF NOT EXISTS reading_records (
    document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    page INTEGER NOT NULL DEFAULT 1 CHECK(page > 0),
    total_pages INTEGER NOT NULL DEFAULT 0 CHECK(total_pages >= 0),
    last_opened INTEGER NOT NULL DEFAULT 0,
    favorite INTEGER NOT NULL DEFAULT 0,
    reading_status TEXT NOT NULL DEFAULT 'unread',
    tags_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS research_entries (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    answer_json TEXT NOT NULL DEFAULT '{}',
    scope_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    starred INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
