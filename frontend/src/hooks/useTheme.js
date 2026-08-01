import { useCallback, useEffect, useState } from 'react'

// ---------------------------------------------------------------------------
// Light / dark theme.
//
// index.html resolves the theme before first paint, so this hook only takes
// over from there: it mirrors state onto <html data-theme>, follows the OS
// setting until the reader makes an explicit choice, and remembers that choice.
// ---------------------------------------------------------------------------

const KEY = 'afb-theme'

const stored = () => {
  try {
    return localStorage.getItem(KEY)
  } catch {
    return null
  }
}

export function useTheme() {
  const [theme, setTheme] = useState(
    () => document.documentElement.getAttribute('data-theme') || 'light',
  )

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
  }, [theme])

  // Keep following the OS until the reader picks a side.
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => {
      if (!stored()) setTheme(mq.matches ? 'dark' : 'light')
    }
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  const toggle = useCallback(() => {
    // Colours cross-fade only during the switch, so nothing lags in normal use.
    const root = document.documentElement
    root.classList.add('theme-switching')
    setTimeout(() => root.classList.remove('theme-switching'), 320)

    setTheme((current) => {
      const next = current === 'dark' ? 'light' : 'dark'
      try {
        localStorage.setItem(KEY, next)
      } catch {
        /* private mode — the choice just won't persist */
      }
      return next
    })
  }, [])

  return [theme, toggle]
}
