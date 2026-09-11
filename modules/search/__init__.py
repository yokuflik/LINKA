"""Server-side message search (ADR 0040).

Keyword search over `messages.content` via PostgreSQL native FTS - a `content_tsv`
tsvector kept by a trigger and one composite `gin(chat_id, content_tsv)` partial
index (`btree_gin`). No external search engine.

- `ddl`      - the schema DDL (column + trigger + extension + index), applied by
               both `scripts/init_db.py` and `tests/conftest.py`.
- `service`  - query-string -> tsquery, cursor codec, snippet, permission, the
               SSE stream generator.
- `crud`     - the FTS queries (in-chat, global with a `participants` JOIN,
               server-side-cursor stream, `messages_around`).
- `router`   - the REST + SSE endpoints.
- `limits`   - the injectable `SearchLimits` (ADR 0033).
"""
