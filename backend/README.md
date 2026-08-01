# backend

FastAPI + pgvector RAG over the *AI for Founders* manuscript.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

`.env` (in this directory):

```
BOOK_SOURCE_PATH=../../ai-for-founders/ai-book-for-founders/final-manuscript
DATABASE_URL=postgresql://user:pass@host/db?sslmode=require
GEMINI_API_KEY=...
```

## Pipeline (run in order, once)

```bash
python scripts/book_chunking.py            # manuscript -> data/chunks.jsonl (347)
python scripts/embed_and_store.py --init   # chunks -> Postgres + pgvector
python scripts/retrieve.py "what is a moat?"   # sanity-check retrieval
```

## Run the API

```bash
fastapi dev app/main.py        # auto-reload, http://localhost:8000
fastapi run app/main.py        # production
```

With the frontend dev server running (`npm run dev` at the repo root) the Vite
proxy sends `/api/*` here. For a single-origin deploy, `npm run build` first —
the app serves `frontend/dist/` automatically when it exists.

`GET /api/health` reports chapter count, model, k, and questions used today.

## Layout

```
app/config.py   settings; inherits the loader's so query embedding always matches
app/rag.py      retrieval, follow-up condensing, prompt assembly, streaming
app/limits.py   in-memory per-IP and global rate limits
app/main.py     the one endpoint from API_CONTRACT.md, plus static serving
```
