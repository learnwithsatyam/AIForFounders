// ---------------------------------------------------------------------------
// Stream pacer — turns bursty network chunks into a smooth, readable stream.
//
// LLM backends deliver text in uneven bursts: nothing for 400ms, then 300
// characters at once. Feeding that straight into React makes the answer lurch.
// The pacer buffers everything the backend sends and releases it on animation
// frames at a rate that adapts to the backlog — so it never falls behind, but
// never dumps a paragraph in a single frame either.
//
// Text is always released on word boundaries, which is what lets the renderer
// fade in whole words instead of half-typed fragments.
// ---------------------------------------------------------------------------

/**
 * @param {(chunk: string) => void} onText  receives paced chunks of text
 */
export function createStreamPacer(onText) {
  let queue = ''
  let emitted = 0
  let raf = 0
  let lastFrame = 0
  let ended = false
  let settle = null

  // Re-parsing markdown gets more expensive as the answer grows, so back off
  // the commit rate (and release more per commit) for long answers.
  const frameInterval = () => (emitted > 6000 ? 50 : emitted > 2500 ? 34 : 22)

  function chunkSize() {
    const backlog = queue.length
    const scale = frameInterval() / 22
    // Once the backend is done there's no reason to hold text back, but drain
    // over several frames so the tail still reads as streaming.
    const cap = Math.round((ended ? Math.max(18, Math.ceil(backlog / 18)) : 12) * scale)
    let n = Math.min(backlog, Math.max(2, Math.min(Math.ceil(backlog / 10), cap)))

    // Extend to the end of the current word, then swallow the spaces after it.
    // The bound only matters for pathological tokens (a long URL, a base64
    // blob); splitting one of those just grows an existing word on screen
    // rather than mounting a new one, so nothing re-animates.
    const limit = Math.min(backlog, n + 60)
    while (n < limit && !/\s/.test(queue[n])) n++
    while (n < backlog && /[^\S\n]/.test(queue[n])) n++
    return n
  }

  function schedule() {
    if (!raf) raf = requestAnimationFrame(tick)
  }

  function tick(now) {
    raf = 0
    if (!queue.length) {
      if (ended) finish()
      return
    }
    if (now - lastFrame < frameInterval()) {
      schedule()
      return
    }
    lastFrame = now

    const n = chunkSize()
    const chunk = queue.slice(0, n)
    queue = queue.slice(n)
    emitted += chunk.length
    onText(chunk)

    if (queue.length) schedule()
    else if (ended) finish()
  }

  function finish() {
    const done = settle
    settle = null
    done?.()
  }

  return {
    /** Feed raw text in as it arrives from the network. */
    push(text) {
      if (!text) return
      queue += text
      schedule()
    },

    /** Signal the source is done; resolves once every buffered word is shown. */
    end() {
      ended = true
      if (!queue.length) return Promise.resolve()
      return new Promise((resolve) => {
        settle = resolve
        schedule()
      })
    },

    /** Show everything still buffered right now (used when the user hits Stop). */
    flush() {
      if (raf) cancelAnimationFrame(raf)
      raf = 0
      if (queue.length) {
        onText(queue)
        emitted += queue.length
        queue = ''
      }
      finish()
    },

    /** Drop anything still buffered and stop. */
    cancel() {
      if (raf) cancelAnimationFrame(raf)
      raf = 0
      queue = ''
      finish()
    },
  }
}
