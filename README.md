# AIForFounders — Ask the Book

A Gemini-style chat interface where readers ask questions and get answers
grounded in the book *AI for Founders*. Domain candidates: `aiforfounders.ai`,
`aiforfounders.com`, `aiforfounders.chat` (verify on a registrar).

## Monorepo layout

```
aiforfounders/
├── frontend/          # React + Vite chat UI (complete, works today)
├── backend/           # Your Python backend (to be built — see backend/README.md)
├── API_CONTRACT.md    # The one endpoint the backend must implement
└── package.json       # Convenience scripts that delegate to frontend/
```

## Quick start

```bash
npm run install:frontend   # one-time: install frontend deps
npm run dev                # start the UI at http://localhost:5173
```

The UI is fully usable immediately — with no backend running it answers with a
built-in mock stream so you can feel the UX. Once your Python backend is live
on `localhost:8000` and implements `API_CONTRACT.md`, real answers stream in
with chapter citations, with zero frontend changes.

## What the frontend gives you

- Gemini-style welcome screen with suggested questions
- **Paced streaming**: bursty backend chunks are buffered and released on
  animation frames at a rate that adapts to the backlog, so answers read
  smoothly however unevenly the network delivers them
  (`src/lib/streamPacer.js`)
- **Word-by-word fade-in**: a small rehype plugin wraps each word in a span, so
  only the words that just arrived animate — settled text never re-renders its
  animation (`src/lib/rehypeWordSpans.js`)
- Shimmering "searching the book" state while waiting for the first token
- Chapter/section citation chips, revealed once the answer finishes writing
- Smart auto-scroll: sticks to the newest text, but stops fighting you the
  moment you scroll up, with a "jump to latest" pill
- Light/dark theme, following the OS until you pick a side, resolved before
  first paint so there's no flash
- Multi-chat sidebar (New chat + recent conversations, in-memory)
- Stop-generation button, auto-growing composer, Enter to send, copy answer
- Respects `prefers-reduced-motion` — all animation collapses to instant
- Dev proxy: `/api/*` → `http://localhost:8000` (no CORS needed)

## Production build

```bash
npm run build              # outputs frontend/dist/
```

Serve `frontend/dist/` as static files from your Python app so the UI and API
share one origin, one port, one deploy.

## Deploy (Fly.io)

The `Dockerfile` builds the UI and bakes it into the Python image, so the whole
app ships as one container. `fly.toml` pins it to a single always-on machine in
Singapore, next to the Neon project.

```bash
fly launch --no-deploy --copy-config          # creates the app from fly.toml
fly secrets set DATABASE_URL='postgresql://…' GEMINI_API_KEY='…'
fly deploy
fly logs                                      # watch the first boot
```

Those two secrets are the only environment the container needs — `backend/.env`
is excluded from the image by `.dockerignore`.

Two settings there are deliberate and worth not "optimising" away:

- **`auto_stop_machines = 'off'` / `min_machines_running = 1`.** Scale-to-zero
  costs double here: waking the container *and* waking a suspended Neon compute,
  in series, before the first question can be answered. It also keeps the pool's
  idle connection alive, which helps hold Neon awake.
- **512MB.** The app measures ~121MB just to import its dependencies, before
  serving anything. 256MB will OOM under load, and an OOM kill mid-answer looks
  like a streaming bug.

The rate limiter in `backend/app/limits.py` is per-process, so this must stay a
single machine with a single worker. Running two of either silently doubles the
daily cap that bounds your Gemini bill.
