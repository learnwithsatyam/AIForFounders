// Conversations survive a refresh, a restart, and closing the tab.
//
// Kept in localStorage rather than on the server: no accounts, no database
// rows, and a reader's questions never leave their own machine.

const KEY = 'aiforfounders.chats.v1'
const MAX_CHATS = 30
const SAVE_DELAY = 400

let seq = 1

export function nextChatId() {
  return seq++
}

// A message saved mid-stream would come back as a spinner that never resolves,
// so anything caught in flight is marked stopped on the way in.
function normalise(m) {
  if (!m || (m.role !== 'user' && m.role !== 'assistant')) return null

  const inFlight = m.status === 'thinking' || m.status === 'streaming'
  const content = String(m.content ?? '')

  // An assistant turn with no text is noise — and worse, replaying it as
  // history sends {content: ""}, which the API rejects with a 422. Any answer
  // that errored before producing a word lands here.
  if (m.role === 'assistant' && !content) return null

  return {
    role: m.role,
    content,
    citations: Array.isArray(m.citations) ? m.citations : [],
    status: inFlight ? 'stopped' : (m.status ?? 'done'),
    ...(m.error ? { error: String(m.error) } : {}),
  }
}

export function loadChats() {
  let raw = null
  try {
    raw = localStorage.getItem(KEY)
  } catch {
    return { chats: [], activeChatId: null } // private mode, storage disabled
  }
  if (!raw) return { chats: [], activeChatId: null }

  let data
  try {
    data = JSON.parse(raw)
  } catch {
    return { chats: [], activeChatId: null }
  }
  if (!data || !Array.isArray(data.chats)) return { chats: [], activeChatId: null }

  const chats = data.chats
    .map((c) => ({
      id: Number(c?.id),
      title: typeof c?.title === 'string' && c.title ? c.title : 'New chat',
      messages: Array.isArray(c?.messages) ? c.messages.map(normalise).filter(Boolean) : [],
    }))
    .filter((c) => Number.isFinite(c.id) && c.messages.length > 0)

  // Ids must not collide with the restored ones.
  seq = chats.reduce((max, c) => Math.max(max, c.id), 0) + 1

  const activeChatId = chats.some((c) => c.id === data.activeChatId)
    ? data.activeChatId
    : (chats[0]?.id ?? null)

  return { chats, activeChatId }
}

let timer = null
let pending = null

function write() {
  timer = null
  if (!pending) return

  const { chats, activeChatId } = pending
  pending = null

  const keep = chats.filter((c) => c.messages.length > 0).slice(0, MAX_CHATS)
  const body = (list) => JSON.stringify({ v: 1, activeChatId, chats: list })

  try {
    localStorage.setItem(KEY, body(keep))
  } catch {
    // Out of quota — halve the history and try once more.
    try {
      localStorage.setItem(KEY, body(keep.slice(0, Math.ceil(keep.length / 2))))
    } catch {
      // Give up quietly. Persistence is a convenience, not the product.
    }
  }
}

// Streaming updates state on every chunk, so writes are debounced rather than
// run per token.
export function saveChats(chats, activeChatId) {
  pending = { chats, activeChatId }
  if (!timer) timer = setTimeout(write, SAVE_DELAY)
}

export function flushChats() {
  if (timer) clearTimeout(timer)
  write()
}

export function clearChats() {
  try {
    localStorage.removeItem(KEY)
  } catch {
    /* nothing to do */
  }
}

// Closing the tab mid-debounce should not lose the last answer.
if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', flushChats)
}
