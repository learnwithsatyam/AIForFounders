import { useCallback, useEffect, useRef, useState } from 'react'

// ---------------------------------------------------------------------------
// Keeps a scroll container pinned to the newest content — but only while the
// reader is already at the bottom. Scroll up mid-answer and the view stays put
// instead of yanking you back down.
//
// Growth is detected with a ResizeObserver rather than a state dependency, so
// it also tracks reflow after images, code blocks and markdown re-layout.
// ---------------------------------------------------------------------------

const NEAR_BOTTOM_PX = 90

export function useStickToBottom() {
  const scrollRef = useRef(null)
  const contentRef = useRef(null)
  const stuck = useRef(true)
  const [atBottom, setAtBottom] = useState(true)

  const stickNow = useCallback(() => {
    const el = scrollRef.current
    if (el && stuck.current) el.scrollTop = el.scrollHeight
  }, [])

  const scrollToBottom = useCallback((smooth = true) => {
    const el = scrollRef.current
    if (!el) return
    stuck.current = true
    setAtBottom(true)
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  }, [])

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return

    const onScroll = () => {
      const gap = el.scrollHeight - el.scrollTop - el.clientHeight
      const near = gap < NEAR_BOTTOM_PX
      stuck.current = near
      setAtBottom(near)
    }

    el.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    const content = contentRef.current
    if (!content || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(stickNow)
    ro.observe(content)
    return () => ro.disconnect()
  }, [stickNow])

  return { scrollRef, contentRef, atBottom, scrollToBottom, stickNow }
}
