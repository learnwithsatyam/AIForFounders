import { memo, useEffect, useState } from 'react'
import AnswerBody from './AnswerBody.jsx'
import ThinkingIndicator from './ThinkingIndicator.jsx'
import { BookIcon, CheckIcon, CopyIcon } from './Icons.jsx'

function ChatMessage({ message }) {
  const isUser = message.role === 'user'
  const status = message.status ?? 'done'
  const streaming = status === 'streaming'
  const thinking = status === 'thinking' && !message.content

  return (
    <div className={`msg ${isUser ? 'user' : 'assistant'}`}>
      {isUser ? (
        <div className="msg-avatar" aria-hidden="true">
          You
        </div>
      ) : (
        <img
          className={`msg-avatar${streaming || thinking ? ' live' : ''}`}
          src="/avatar.jpg"
          alt=""
          width="32"
          height="32"
        />
      )}

      <div className="msg-body">
        {isUser ? (
          message.content
        ) : message.error ? (
          <div className="msg-error">{message.error}</div>
        ) : thinking ? (
          <ThinkingIndicator />
        ) : !message.content && status === 'stopped' ? (
          <div className="msg-flag">You stopped this before the answer started.</div>
        ) : (
          <>
            <AnswerBody content={message.content} live={streaming} />

            {message.citations?.length > 0 && (
              <div className="citations">
                <span className="citations-label">From</span>
                {message.citations.map((c, i) => (
                  <span key={`${c.chapter}-${i}`} className="citation" style={{ '--i': i }}>
                    <BookIcon />
                    {c.chapter}
                    {c.section && (
                      <>
                        <span className="dot">·</span>
                        {c.section}
                      </>
                    )}
                  </span>
                ))}
              </div>
            )}

            {status !== 'streaming' && message.content && (
              <div className="msg-actions">
                <CopyButton text={message.content} />
                {status === 'stopped' && <span className="msg-flag">You stopped this answer</span>}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const id = setTimeout(() => setCopied(false), 1600)
    return () => clearTimeout(id)
  }, [copied])

  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
    } catch {
      /* clipboard blocked — nothing useful to show */
    }
  }

  return (
    <button className={`ghost-btn${copied ? ' ok' : ''}`} onClick={copy} title="Copy answer">
      <span key={copied ? 'y' : 'n'} className="ghost-btn-icon">
        {copied ? <CheckIcon /> : <CopyIcon />}
      </span>
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}

// Only the message currently being written needs to re-render as text arrives.
export default memo(ChatMessage)
