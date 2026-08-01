import { useEffect, useState } from 'react'

// Shown between "question sent" and "first token arrived". The rotating labels
// make a slow backend feel like it is doing something rather than hanging.
const STAGES = ['Searching the book', 'Pulling the relevant passages', 'Writing your answer']

export default function ThinkingIndicator() {
  const [stage, setStage] = useState(0)

  useEffect(() => {
    const id = setInterval(() => setStage((s) => Math.min(s + 1, STAGES.length - 1)), 2200)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="thinking" role="status" aria-live="polite">
      <span key={stage} className="thinking-label">
        {STAGES[stage]}
      </span>
      <span className="thinking-dots" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
    </div>
  )
}
