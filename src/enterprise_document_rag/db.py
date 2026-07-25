import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from .config import Settings

# SQLite permits many readers but only one writer.  A short bounded wait makes
# routine hand-offs between the API process and the ingestion worker reliable
# without hiding a genuinely stuck writer forever.
SQLITE_BUSY_TIMEOUT_MS = 15_000
SQLITE_LOCK_RETRY_DELAYS_SECONDS = (0.05, 0.15, 0.35)

_T = TypeVar("_T")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_bases (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    embedding_model TEXT NOT NULL,
    vector_backend TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    root_path TEXT NOT NULL,
    include_patterns TEXT NOT NULL DEFAULT '[]',
    exclude_patterns TEXT NOT NULL DEFAULT '[]',
    scan_state TEXT NOT NULL DEFAULT 'idle',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (knowledge_base_id, root_path)
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    canonical_path TEXT NOT NULL,
    active_version_id TEXT,
    visibility_state TEXT NOT NULL DEFAULT 'visible',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (knowledge_base_id, canonical_path)
);

CREATE TABLE IF NOT EXISTS document_versions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    mtime_ns INTEGER NOT NULL CHECK (mtime_ns >= 0),
    sha256 TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    layout_version TEXT,
    document_type TEXT,
    document_structure_tree TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (document_id, sha256)
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    document_version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    text TEXT NOT NULL,
    page_no INTEGER,
    page_range TEXT,
    section_path TEXT,
    bbox TEXT,
    bbox_list TEXT NOT NULL DEFAULT '[]',
    content_type TEXT NOT NULL DEFAULT 'text',
    source_type TEXT NOT NULL DEFAULT 'native_text',
    ocr_confidence REAL,
    block_types TEXT NOT NULL DEFAULT '[]',
    table_markdown TEXT,
    image_path TEXT,
    caption TEXT,
    image_metadata TEXT,
    token_count INTEGER NOT NULL CHECK (token_count >= 0),
    text_hash TEXT NOT NULL,
    previous_chunk_id TEXT,
    next_chunk_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (document_version_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS index_records (
    id TEXT PRIMARY KEY,
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    index_kind TEXT NOT NULL,
    external_id TEXT NOT NULL,
    index_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (chunk_id, index_kind, index_version)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    document_version_id UNINDEXED,
    text
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    job_key TEXT NOT NULL UNIQUE,
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    operation TEXT NOT NULL,
    path TEXT NOT NULL,
    expected_sha256 TEXT,
    state TEXT NOT NULL DEFAULT 'queued',
    payload TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_owner TEXT,
    lease_expires_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    external_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS roles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS permissions (
    id TEXT PRIMARY KEY,
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    permission TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (role_id, knowledge_base_id, permission)
);

CREATE TABLE IF NOT EXISTS query_audits (
    id TEXT PRIMARY KEY,
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    query TEXT NOT NULL,
    filters TEXT NOT NULL DEFAULT '{}',
    cited_chunk_ids TEXT NOT NULL DEFAULT '[]',
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_document_versions_document_id
    ON document_versions(document_id);

CREATE INDEX IF NOT EXISTS idx_chunks_document_version_id
    ON chunks(document_version_id);

CREATE INDEX IF NOT EXISTS idx_jobs_state
    ON jobs(state);
"""


def _is_locked_error(error: sqlite3.OperationalError) -> bool:
    """Return whether SQLite rejected an operation because another writer owns it."""
    error_code = getattr(error, "sqlite_errorcode", None)
    if isinstance(error_code, int) and error_code & 0xFF in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        return True
    message = str(error).lower()
    return "database is locked" in message or "database schema is locked" in message


def _retry_locked(operation: Callable[[], _T]) -> _T:
    """Retry only operations SQLite confirms were blocked before they ran."""
    for delay in SQLITE_LOCK_RETRY_DELAYS_SECONDS:
        try:
            return operation()
        except sqlite3.OperationalError as error:
            if not _is_locked_error(error):
                raise
            time.sleep(delay)
    return operation()


class _ResilientSQLiteConnection(sqlite3.Connection):
    """Connection with bounded retries for single SQL statements and commits.

    Retrying ``executemany`` generically is unsafe because a batch can be partly
    applied before an error.  It still benefits from SQLite's busy timeout.
    """

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        return _retry_locked(
            lambda: sqlite3.Connection.execute(self, sql, parameters)  # type: ignore[arg-type]
        )

    def commit(self) -> None:
        _retry_locked(lambda: sqlite3.Connection.commit(self))


def _connect(path: Path) -> sqlite3.Connection:
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        path,
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
        factory=_ResilientSQLiteConnection,
    )
    connection.row_factory = sqlite3.Row
    # These are connection-local.  Configure them as soon as the connection is
    # opened so every API request and worker transaction gets the same policy.
    connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_sqlite(settings: Settings) -> None:
    with sqlite_connection(settings) as connection:
        _ensure_wal_mode(connection, database_path=settings.database_path)

        def initialize() -> None:
            connection.executescript(SCHEMA_SQL)
            _upgrade_pdf_metadata_schema(connection)
            connection.execute(
                "INSERT OR REPLACE INTO app_metadata (key, value) VALUES (?, ?)",
                ("schema_version", "pdf-intelligence-v1"),
            )
            connection.commit()

        try:
            _retry_locked(initialize)
        except Exception:
            # End a failed startup transaction promptly; otherwise a new
            # process can inherit the appearance of a persistent writer lock.
            if connection.in_transaction:
                connection.rollback()
            raise


def _ensure_wal_mode(connection: sqlite3.Connection, *, database_path: Path) -> None:
    """Enable WAL once for file-backed databases instead of rewriting it on every boot."""
    if database_path == Path(":memory:"):
        return
    row = connection.execute("PRAGMA journal_mode").fetchone()
    journal_mode = str(row[0]).lower() if row is not None else ""
    if journal_mode != "wal":
        connection.execute("PRAGMA journal_mode=WAL").fetchone()


def _upgrade_pdf_metadata_schema(connection: sqlite3.Connection) -> None:
    """Add PDF intelligence metadata to databases created by older releases."""
    additions = {
        "document_versions": {
            "layout_version": "TEXT",
            "document_type": "TEXT",
            "document_structure_tree": "TEXT NOT NULL DEFAULT '[]'",
        },
        "chunks": {
            "page_range": "TEXT",
            "bbox_list": "TEXT NOT NULL DEFAULT '[]'",
            "content_type": "TEXT NOT NULL DEFAULT 'text'",
            "source_type": "TEXT NOT NULL DEFAULT 'native_text'",
            "ocr_confidence": "REAL",
            "block_types": "TEXT NOT NULL DEFAULT '[]'",
            "table_markdown": "TEXT",
            "image_path": "TEXT",
            "caption": "TEXT",
            "image_metadata": "TEXT",
        },
    }
    for table, columns in additions.items():
        existing = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column, definition in columns.items():
            if column not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@contextmanager
def sqlite_connection(settings: Settings) -> Iterator[sqlite3.Connection]:
    connection = _connect(settings.database_path)
    try:
        yield connection
    finally:
        # Repositories commit their short write units themselves.  Roll back
        # anything accidentally left open before closing so it cannot hold a
        # write lock longer than the request/worker operation that created it.
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def sqlite_health(settings: Settings) -> dict[str, bool | str]:
    with sqlite_connection(settings) as connection:
        one = connection.execute("SELECT 1").fetchone()[0]
        compile_options = {
            row[0] for row in connection.execute("PRAGMA compile_options").fetchall()
        }
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    return {
        "ok": one == 1,
        "journal_mode": journal_mode,
        "fts5_enabled": "ENABLE_FTS5" in compile_options,
    }
