import { useEffect, useRef } from 'react'
import { SendIcon, StopIcon } from './Icons.jsx'

export default function Composer({ value, onChange, onSend, onStop, busy }) {
  const ref = useRef(null)

  // Auto-grow the textarea with its content.
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 180) + 'px'
  }, [value])

  // Focus the composer again the moment an answer finishes.
  useEffect(() => {
    if (!busy) ref.current?.focus()
  }, [busy])

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (!busy && value.trim()) onSend()
    }
  }

  return (
    <div className="composer-wrap">
      <div className={`composer${busy ? ' busy' : ''}`}>
        <textarea
          ref={ref}
          rows={1}
          value={value}
          placeholder="Ask anything about the book…"
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={handleKeyDown}
          aria-label="Your question"
        />
        {busy ? (
          <button className="send-btn stop" onClick={onStop} title="Stop generating">
            <span className="btn-icon">
              <StopIcon />
            </span>
          </button>
        ) : (
          <button
            className="send-btn"
            onClick={onSend}
            disabled={!value.trim()}
            title="Send"
            aria-label="Send"
          >
            <span className="btn-icon">
              <SendIcon />
            </span>
          </button>
        )}
      </div>
      <div className="composer-hint">
        Answers come from the book itself — open the cited chapters for the full argument.
      </div>
    </div>
  )
}
