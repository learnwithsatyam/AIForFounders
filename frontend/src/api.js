// ---------------------------------------------------------------------------
// Chat API client.
//
// Talks to YOUR Python backend at POST /api/chat (see API_CONTRACT.md).
// If the backend isn't running yet, it falls back to a built-in mock stream
// so the UI is fully usable while you build the Python side.
// ---------------------------------------------------------------------------

/**
 * Send the conversation to the backend and stream the answer.
 *
 * @param {Array<{role: 'user'|'assistant', content: string}>} messages
 * @param {(text: string) => void} onDelta      called for each text chunk
 * @param {(citations: Array) => void} onCitations  called once if backend sends citations
 * @param {AbortSignal} signal                  abort to stop generation
 */
export async function sendChat(messages, onDelta, onCitations, signal) {
  let res
  try {
    res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages }),
      signal,
    })
  } catch (err) {
    if (err.name === 'AbortError') throw err
    // Backend unreachable — use the mock so the UI still works.
    return mockStream(onDelta, onCitations, signal)
  }

  // Only an unreachable backend falls back to the mock (handled above). A
  // backend that answered with an error must surface it — otherwise a real
  // failure is indistinguishable from the demo text.
  if (!res.ok) {
    let detail = ''
    try {
      detail = JSON.stringify(await res.json())
    } catch {
      /* body was not JSON */
    }
    throw new Error(`Backend returned ${res.status}. ${detail}`.trim())
  }
  if (!res.body) throw new Error('Backend sent an empty response.')

  // Parse the SSE stream: lines of `data: {json}\n\n`
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    const lines = buffer.split('\n')
    buffer = lines.pop() // keep the last (possibly incomplete) line

    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed.startsWith('data:')) continue
      const payload = trimmed.slice(5).trim()
      if (!payload) continue
      let event
      try {
        event = JSON.parse(payload)
      } catch {
        continue
      }
      if (event.type === 'delta' && event.text) onDelta(event.text)
      if (event.type === 'citations' && Array.isArray(event.citations)) {
        onCitations(event.citations)
      }
      if (event.type === 'done') return
      if (event.type === 'error') {
        throw new Error(event.message || 'The backend hit an error answering that.')
      }
    }
  }
}

// ------------------------------- accounts ----------------------------------
// Optional throughout: every one of these can fail or return null and the app
// carries on anonymously, which is the default state.

async function authCall(path, body) {
  const res = await fetch(`/api/auth/${path}`, {
    method: body ? 'POST' : 'GET',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    credentials: 'same-origin',
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || 'Something went wrong. Try again.')
  return data
}

/** Who is signed in, if anyone. Returns null rather than throwing. */
export async function fetchMe() {
  try {
    return (await authCall('me')).user
  } catch {
    return null
  }
}

export const signUp = (email, password, name) =>
  authCall('signup', { email, password, name }).then((d) => d.user)

export const signIn = (email, password) =>
  authCall('login', { email, password }).then((d) => d.user)

export const signOut = () => authCall('logout', {}).then(() => null)

// --------------------------- mock fallback ---------------------------------

const MOCK_ANSWER = `**No backend yet** — this is a sample answer so you can feel how the interface behaves.

Start your Python server on \`localhost:8000\`, implement the one endpoint in \`API_CONTRACT.md\`, and real answers from *AI for Founders* stream into this exact spot. Something like:

> Founders consistently overestimate how much AI infrastructure they need on day one. Build the smallest thing that answers the question, measure it, and add complexity only when the numbers ask for it.

Answers render as full markdown — **bold**, *italics*, \`code\`, quotes and lists:

1. Grounded in the book's actual text
2. Streamed word by word as the model writes
3. Cited back to the chapters it drew from`

const MOCK_CITATIONS = [
  { chapter: 'Chapter 1', section: 'The Real Cost of AI Tools' },
  { chapter: 'Chapter 4', section: 'Build Small, Measure, Repeat' },
]

// Deliberately uneven: real LLM backends go quiet for a moment and then hand
// over a whole clause at once. Emitting that shape here means the UI is tuned
// against realistic streaming rather than an artificially perfect one.
async function mockStream(onDelta, onCitations, signal) {
  const sleep = (ms) =>
    new Promise((resolve, reject) => {
      const id = setTimeout(resolve, ms)
      signal?.addEventListener(
        'abort',
        () => {
          clearTimeout(id)
          reject(new DOMException('Aborted', 'AbortError'))
        },
        { once: true },
      )
    })

  await sleep(700) // time-to-first-token

  let i = 0
  while (i < MOCK_ANSWER.length) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    const size = 4 + Math.floor(Math.random() * 34)
    onDelta(MOCK_ANSWER.slice(i, i + size))
    i += size
    await sleep(Math.random() < 0.12 ? 180 : 25 + Math.random() * 45)
  }

  onCitations(MOCK_CITATIONS)
}
