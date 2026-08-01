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
