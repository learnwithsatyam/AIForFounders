import { memo } from 'react'
import ReactMarkdown from 'react-markdown'
import rehypeWordSpans from '../lib/rehypeWordSpans.js'

const PLUGINS = [rehypeWordSpans]

// Markdown is re-parsed on every paced frame while an answer streams, so this
// is memoised: only the message that is actually growing does the work.
// `live` drives the per-word fade-in and the trailing caret; once the answer is
// finished the class comes off and the DOM stops animating entirely.
function AnswerBody({ content, live }) {
  return (
    <div className={`msg-content${live ? ' live' : ''}`}>
      <ReactMarkdown rehypePlugins={PLUGINS}>{content}</ReactMarkdown>
    </div>
  )
}

export default memo(AnswerBody)
