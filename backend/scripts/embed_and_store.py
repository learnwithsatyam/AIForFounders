"""embed_and_store.py — chunks.jsonl -> Neon Postgres + pgvector.
 
Run once after chunking, and again whenever chunks.jsonl changes:
 
    python scripts/embed_and_store.py --init     # first run: extension + table
    python scripts/embed_and_store.py            # later runs: only new/changed
    python scripts/embed_and_store.py --force    # re-embed everything
 
Safe to re-run and safe to interrupt: every batch is committed as it lands,
and rows already carrying an embedding are skipped unless --force is given.
"""
 
from __future__ import annotations
 
import argparse
import json
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final
 
import psycopg
from google import genai
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
 
# Anchor every relative path to backend/, never the shell's cwd.
BACKEND_DIR: Final[Path] = Path(__file__).resolve().parents[1]
 
 
# --- the prefix contract ---------------------------------------------------
# gemini-embedding-2 is asymmetric: a document and a question about that
# document are formatted DIFFERENTLY, which is what makes a question land near
# its answer instead of near other questions. Both sides must be applied, and
# they must never drift apart — retrieve.py imports query_embed_text() from
# this module rather than rewriting the string.
 
DOC_TEMPLATE: Final[str] = "title: {title} | text: {text}"
QUERY_TEMPLATE: Final[str] = "task: question answering | query: {query}"
 
 
def doc_embed_text(chapter: str, section: str, content: str) -> str:
    """Index-side format. The title carries the structural context, so a
    passage saying 'this fails at scale' is still findable as being about
    whatever its section is about."""
    return DOC_TEMPLATE.format(title=f"{chapter} - {section}", text=content)
 
 
def query_embed_text(question: str) -> str:
    """Query-side format. Import this from retrieve.py and from the API."""
    return QUERY_TEMPLATE.format(query=question)
 
 
# --- config ----------------------------------------------------------------
 
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")
 
    database_url: str
    gemini_api_key: str
 
    chunks_out: Path = Path("data/chunks.jsonl")
    embed_model: str = "gemini-embedding-2"
    embed_dim: int = Field(default=1536, gt=0, le=2000)
    batch_size: int = Field(default=50, gt=0, le=100)
    max_retries: int = Field(default=5, ge=0)
 
    @field_validator("chunks_out", mode="after")
    @classmethod
    def _anchor(cls, v: Path) -> Path:
        return v if v.is_absolute() else (BACKEND_DIR / v).resolve()
 
 
class ChunkRecord(BaseModel):
    """One line of chunks.jsonl.
 
    embed_text and word_count were computed_fields on the writing side, so they
    appear in the JSON. word_count we keep; embed_text we deliberately ignore,
    because the embedding format is this module's business now, not the
    chunker's.
    """
 
    model_config = ConfigDict(frozen=True, extra="ignore")
 
    id: int
    source_file: str
    chapter: str = Field(min_length=1)
    section: str = Field(min_length=1)
    kind: str = "prose"
    content: str = Field(min_length=1)
    word_count: int = Field(gt=0)
 
 
# --- schema ----------------------------------------------------------------
 
DDL: Final[str] = """
CREATE EXTENSION IF NOT EXISTS vector;
 
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY,
    source_file TEXT    NOT NULL,
    chapter     TEXT    NOT NULL,
    section     TEXT    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'prose',
    content     TEXT    NOT NULL,
    word_count  INTEGER NOT NULL,
    embedding   VECTOR(%(dim)s)
);
"""
 
# Built only after a full load — HNSW over a populated table is both faster to
# build and better structured than one grown row by row.
INDEX_DDL: Final[str] = """
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw
    ON chunks USING hnsw (embedding vector_cosine_ops);
"""
 
UPSERT: Final[str] = """
INSERT INTO chunks
    (id, source_file, chapter, section, kind, content, word_count, embedding)
VALUES
    (%s, %s, %s, %s, %s, %s, %s, %s::vector)
ON CONFLICT (id) DO UPDATE SET
    source_file = EXCLUDED.source_file,
    chapter     = EXCLUDED.chapter,
    section     = EXCLUDED.section,
    kind        = EXCLUDED.kind,
    content     = EXCLUDED.content,
    word_count  = EXCLUDED.word_count,
    embedding   = EXCLUDED.embedding;
"""
 
 
# --- io --------------------------------------------------------------------
 
def read_chunks(path: Path) -> list[ChunkRecord]:
    with path.open(encoding="utf-8") as f:
        return [ChunkRecord.model_validate_json(line) for line in f if line.strip()]
 
 
def batched(items: Sequence[ChunkRecord], n: int) -> Iterator[Sequence[ChunkRecord]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]
 
 
def to_vector_literal(values: Sequence[float]) -> str:
    """pgvector parses '[0.1,0.2,...]'. json.dumps gives exactly that, so we
    avoid needing the pgvector-python package just for one INSERT."""
    return json.dumps(list(values))
 
 
# --- embedding -------------------------------------------------------------
 
def embed_batch(
    client: genai.Client, texts: Sequence[str], s: Settings
) -> list[list[float]]:
    """One API call, one vector per input.
 
    The trap: passing a plain list[str] to `contents` returns ONE aggregated
    embedding for the whole list, not one per item. Wrapping each string in a
    Content object is what makes them separate. The length assertion below is
    load-bearing — without it a silent aggregation would write 50 identical
    vectors and retrieval would be quietly garbage.
    """
    contents = [
        types.Content(parts=[types.Part.from_text(text=t)]) for t in texts
    ]
 
    for attempt in range(s.max_retries + 1):
        try:
            resp = client.models.embed_content(
                model=s.embed_model,
                contents=contents,
                config=types.EmbedContentConfig(
                    output_dimensionality=s.embed_dim
                ),
            )
            vectors = [list(e.values) for e in (resp.embeddings or [])]
 
            if len(vectors) != len(texts):
                raise RuntimeError(
                    f"asked for {len(texts)} embeddings, got {len(vectors)} — "
                    "inputs were aggregated; each must be a types.Content"
                )
            for v in vectors:
                if len(v) != s.embed_dim:
                    raise RuntimeError(
                        f"expected {s.embed_dim} dims, got {len(v)}"
                    )
            return vectors
 
        except Exception as exc:  # noqa: BLE001 — retry anything transient
            if attempt == s.max_retries:
                raise
            wait = 2**attempt
            print(f"  batch failed ({exc.__class__.__name__}), retry in {wait}s")
            time.sleep(wait)
 
    raise AssertionError("unreachable")
 
 
# --- pipeline --------------------------------------------------------------
 
def already_embedded(conn: psycopg.Connection) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM chunks WHERE embedding IS NOT NULL;")
        return {row[0] for row in cur.fetchall()}
 
 
def store(
    conn: psycopg.Connection,
    records: Sequence[ChunkRecord],
    vectors: Sequence[Sequence[float]],
) -> None:
    rows = [
        (
            r.id,
            r.source_file,
            r.chapter,
            r.section,
            r.kind,
            r.content,
            r.word_count,
            to_vector_literal(v),
        )
        for r, v in zip(records, vectors, strict=True)
    ]
    with conn.cursor() as cur:
        cur.executemany(UPSERT, rows)
    conn.commit()
 
 
def report(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(embedding), min(id), max(id) FROM chunks;"
        )
        total, embedded, lo, hi = cur.fetchone()  # type: ignore[misc]
        cur.execute("SELECT kind, count(*) FROM chunks GROUP BY kind ORDER BY 1;")
        kinds = dict(cur.fetchall())
    print(f"\nrows {total}  embedded {embedded}  ids {lo}..{hi}")
    print(f"kind {kinds}")
    if embedded != total:
        print(f"WARNING: {total - embedded} rows have no embedding")
 
 
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--init", action="store_true", help="create extension + table")
    ap.add_argument("--force", action="store_true", help="re-embed every chunk")
    args = ap.parse_args()
 
    s = Settings()  # type: ignore[call-arg]  # values come from .env
    records = read_chunks(s.chunks_out)
    print(f"{len(records)} chunks from {s.chunks_out}")
 
    client = genai.Client(api_key=s.gemini_api_key)
 
    with psycopg.connect(s.database_url) as conn:
        if args.init:
            with conn.cursor() as cur:
                cur.execute(DDL, {"dim": s.embed_dim})
            conn.commit()
            print(f"schema ready (vector({s.embed_dim}))")
 
        done: set[int] = set() if args.force else already_embedded(conn)
        todo = [r for r in records if r.id not in done]
 
        # Printed unconditionally and before the loop: a run that silently does
        # nothing is then impossible to miss.
        plan = list(batched(todo, s.batch_size))
        print(
            f"{len(done)} already embedded, "
            f"{len(todo)} to embed in {len(plan)} batches"
        )
 
        for i, batch in enumerate(plan, start=1):
            texts = [
                doc_embed_text(r.chapter, r.section, r.content) for r in batch
            ]
            vectors = embed_batch(client, texts, s)
            store(conn, batch, vectors)
            print(f"  batch {i}: {len(batch)} stored (through id {batch[-1].id})")
 
        if todo:
            with conn.cursor() as cur:
                cur.execute(INDEX_DDL)
            conn.commit()
            print("hnsw index ready")
 
        report(conn)
 
 
if __name__ == "__main__":
    main()