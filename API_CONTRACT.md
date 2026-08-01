# API Contract — what the frontend expects from your Python backend

The UI makes exactly **one** call. Implement this and everything works.

## `POST /api/chat`

In development the Vite dev server proxies `/api/*` to `http://localhost:8000`
(see `frontend/vite.config.js`), so run your backend on port 8000 — FastAPI,
Flask, anything.

### Request body

```json
{
  "messages": [
    { "role": "user", "content": "What does the book say about burn rate?" },
    { "role": "assistant", "content": "The book argues that..." },
    { "role": "user", "content": "Can you expand on that?" }
  ]
}
```

The full conversation is sent every time (oldest first, last item is the new
question), so your backend can stay stateless.

### Response: Server-Sent Events stream

Respond with `Content-Type: text/event-stream` and emit events as
`data: <json>\n\n` lines:

| Event | Payload | Meaning |
|---|---|---|
| delta | `{"type": "delta", "text": "chunk of answer"}` | Append text to the answer (send many of these). Markdown is rendered. |
| citations | `{"type": "citations", "citations": [{"chapter": "Chapter 4", "section": "Build Small"}]}` | Optional, once, shown as chips under the answer. `section` is optional. |
| done | `{"type": "done"}` | End of answer. Always send this last. |
| error | `{"type": "error", "message": "..."}` | Shown to the user as an error; stream ends. |

### Example stream

```
data: {"type": "delta", "text": "The book argues "}

data: {"type": "delta", "text": "that founders should..."}

data: {"type": "citations", "citations": [{"chapter": "Chapter 2", "section": "The Build vs Buy Decision"}]}

data: {"type": "done"}
```

### FastAPI sketch (for reference only — your implementation)

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import json

app = FastAPI()

@app.post("/api/chat")
async def chat(body: dict):
    async def stream():
        # 1. take body["messages"], last item is the new question
        # 2. retrieve relevant book chunks (or use full book in context)
        # 3. call your LLM with streaming, yield each token:
        for token in ["Hello ", "from ", "the ", "backend."]:
            yield f'data: {json.dumps({"type": "delta", "text": token})}\n\n'
        yield f'data: {json.dumps({"type": "citations", "citations": [{"chapter": "Chapter 1"}]})}\n\n'
        yield f'data: {json.dumps({"type": "done"})}\n\n'
    return StreamingResponse(stream(), media_type="text/event-stream")
```

### Notes

- **No backend running?** The UI detects the failed request and shows a mock
  streamed answer, so you can develop the frontend and backend independently.
- **CORS:** not needed in dev (the Vite proxy makes requests same-origin). In
  production, serve the built frontend (`frontend/dist/`) from your Python app
  as static files and it stays same-origin too.
- **Abort:** when the user hits Stop, the frontend closes the connection —
  handle client disconnects gracefully.
