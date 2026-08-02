import { useEffect, useRef, useState } from 'react'
import Sidebar from './components/Sidebar.jsx'
import WelcomeScreen from './components/WelcomeScreen.jsx'
import ChatMessage from './components/ChatMessage.jsx'
import Composer from './components/Composer.jsx'
import { ArrowDownIcon, MenuIcon, MoonIcon, SunIcon } from './components/Icons.jsx'
import { useStickToBottom } from './hooks/useStickToBottom.js'
import { useTheme } from './hooks/useTheme.js'
import { createStreamPacer } from './lib/streamPacer.js'
import { loadChats, saveChats, nextChatId } from './lib/chatStore.js'
import { sendChat } from './api.js'

const newChat = () => ({ id: nextChatId(), title: 'New chat', messages: [] })

export default function App() {
  const [restored] = useState(loadChats)
  const [chats, setChats] = useState(() =>
    restored.chats.length ? restored.chats : [newChat()],
  )
  const [activeChatId, setActiveChatId] = useState(
    () => restored.activeChatId ?? chats?.[0]?.id ?? 1,
  )
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth > 720)
  const [theme, toggleTheme] = useTheme()
  const abortRef = useRef(null)

  const { scrollRef, contentRef, atBottom, scrollToBottom } = useStickToBottom()

  const activeChat = chats.find((c) => c.id === activeChatId) ?? chats[0]

  // Conversations outlive the tab. Debounced, so streaming does not thrash it.
  useEffect(() => {
    saveChats(chats, activeChatId)
  }, [chats, activeChatId])

  // Switching conversations should land at the newest message, not animate there.
  useEffect(() => {
    scrollToBottom(false)
  }, [activeChatId, scrollToBottom])

  function updateChat(chatId, updater) {
    setChats((prev) => prev.map((c) => (c.id === chatId ? updater(c) : c)))
  }

  function stopStreaming() {
    abortRef.current?.abort()
  }

  function handleNewChat() {
    if (busy) stopStreaming()
    const chat = newChat()
    setChats((prev) => [chat, ...prev])
    setActiveChatId(chat.id)
    setInput('')
    if (window.innerWidth <= 720) setSidebarOpen(false)
  }

  function handleSelectChat(id) {
    if (busy) stopStreaming()
    setActiveChatId(id)
    setInput('')
    if (window.innerWidth <= 720) setSidebarOpen(false)
  }

  async function handleSend(text) {
    const question = (text ?? input).trim()
    if (!question || busy) return

    const chatId = activeChat.id
    setInput('')
    setBusy(true)

    const userMsg = { role: 'user', content: question }
    const assistantMsg = { role: 'assistant', content: '', citations: [], status: 'thinking' }

    updateChat(chatId, (c) => ({
      ...c,
      title: c.messages.length === 0 ? question.slice(0, 60) : c.title,
      messages: [...c.messages, userMsg, assistantMsg],
    }))

    // A brand new question should always pull the view down, even if the reader
    // had scrolled up in the previous answer.
    requestAnimationFrame(() => scrollToBottom(true))

    const controller = new AbortController()
    abortRef.current = controller

    // An answer that errored, or was stopped before producing a word, sits in
    // the thread with no content. Replaying it as history says nothing and the
    // API rejects it (422, content must be non-empty) — which would otherwise
    // brick every later question in this conversation, not just the failed one.
    const history = [...activeChat.messages, userMsg]
      .filter((m) => m.content.trim())
      .map(({ role, content }) => ({ role, content }))

    const patchLast = (patch) =>
      updateChat(chatId, (c) => {
        const msgs = [...c.messages]
        const last = msgs[msgs.length - 1]
        msgs[msgs.length - 1] = { ...last, ...patch(last) }
        return { ...c, messages: msgs }
      })

    // Text goes through the pacer so bursty backends still read smoothly.
    const pacer = createStreamPacer((chunk) =>
      patchLast((m) => ({ content: m.content + chunk, status: 'streaming' })),
    )

    // Held back until the text has finished rendering, so the chips land last.
    let citations = []

    try {
      await sendChat(
        history,
        (delta) => pacer.push(delta),
        (list) => {
          citations = list
        },
        controller.signal,
      )
      await pacer.end()
      patchLast(() => ({ citations, status: 'done' }))
    } catch (err) {
      if (err.name === 'AbortError') {
        // Show everything that already arrived rather than discarding it.
        pacer.flush()
        patchLast(() => ({ status: 'stopped' }))
      } else {
        pacer.cancel()
        patchLast(() => ({
          status: 'done',
          error: err.message || "That didn't go through. Ask again in a moment.",
        }))
      }
    } finally {
      setBusy(false)
      abortRef.current = null
    }
  }

  const hasMessages = activeChat.messages.length > 0

  return (
    <div className="app">
      <Sidebar
        open={sidebarOpen}
        chats={chats.filter((c) => c.messages.length > 0)}
        activeChatId={activeChatId}
        onNewChat={handleNewChat}
        onSelectChat={handleSelectChat}
        onClose={() => setSidebarOpen(false)}
      />

      <div className="main">
        <div className="topbar">
          <button
            className="icon-btn"
            onClick={() => setSidebarOpen((v) => !v)}
            title="Toggle sidebar"
            aria-label="Toggle sidebar"
          >
            <MenuIcon />
          </button>
          <span className="topbar-title">Ask the Book</span>
          <span className="topbar-badge">AI for Founders</span>

          <span className="topbar-spacer" />

          <button
            className="icon-btn"
            onClick={toggleTheme}
            title={theme === 'dark' ? 'Switch to light' : 'Switch to dark'}
            aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          >
            <span key={theme} className="btn-icon">
              {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
            </span>
          </button>
        </div>

        <div className="thread-area">
          {hasMessages ? (
            <div className="thread" ref={scrollRef}>
              <div className="thread-inner" ref={contentRef}>
                {activeChat.messages.map((m, i) => (
                  <ChatMessage key={i} message={m} />
                ))}
              </div>
            </div>
          ) : (
            <WelcomeScreen onPick={(q) => handleSend(q)} />
          )}

          <button
            className={`jump-btn${hasMessages && !atBottom ? ' show' : ''}`}
            onClick={() => scrollToBottom(true)}
            title="Jump to latest"
            aria-label="Jump to latest"
            tabIndex={hasMessages && !atBottom ? 0 : -1}
          >
            <ArrowDownIcon />
          </button>
        </div>

        <Composer
          value={input}
          onChange={setInput}
          onSend={() => handleSend()}
          onStop={stopStreaming}
          busy={busy}
        />
      </div>
    </div>
  )
}
