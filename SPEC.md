# Product Specification

## Goal

Provide a local-first document RAG application that indexes administrator-selected folders and answers only from retrieved evidence with source citations.

## Runtime

- Python 3.11/3.12, one FastAPI process and one ingestion worker thread.
- CPU is the default; GPU is optional.
- SQLite WAL stores metadata, jobs, ACL data and FTS5 text indexes.
- Qdrant Local stores vectors; source documents remain read-only.
- Models, paths, thresholds and backends are environment-configurable.

## Supported content

- PDF, DOCX, XLS/XLSX/XLSM, TXT, Markdown and ZIP.
- OCR for pages or documents with insufficient extracted text.
- Structure-aware chunks retain page, section, bounding box and adjacency metadata.
- Archive parsing enforces member, size and compression-ratio limits.

## Ingestion

1. Register a knowledge base and source folder.
2. Fingerprint files and enqueue add, update, move or delete jobs.
3. Parse one file at a time without loading the corpus into memory.
4. Chunk text, write SQLite/FTS5 records and batch local embeddings.
5. Atomically activate a version only after text and vector counts match.
6. Preserve job state across restarts and avoid duplicate active chunks.

## Retrieval and answers

- Apply knowledge-base and allowed-document filters before retrieval.
- Fuse FTS5, exact-contains and vector candidates.
- Optionally rerank candidates with a local cross-encoder.
- Generate from retrieved evidence only; refuse when evidence is insufficient.
- Validate every citation against the current visible document version.

## Security boundaries

- Never modify, move or delete source files.
- Do not commit business documents, indexes, models, secrets or generated evaluation data.
- The current MVP has no complete login or tenant layer.
- `AUTHORIZED_ROOTS` exists in configuration but is not yet enforced by the source-creation API; production deployments must restrict OS/container access and add server-side validation.
- Use one process with Qdrant Local. Multi-instance deployment requires Qdrant Server and coordinated jobs.

## Acceptance

- Incremental add/update/delete works after restart.
- Search and answers return valid source citations.
- Unsupported or insufficient evidence fails safely.
- The full automated test suite passes on a CPU environment.
