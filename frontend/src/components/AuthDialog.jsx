import { useEffect, useRef, useState } from 'react'
import { signIn, signUp } from '../api.js'

// One dialog, two modes. Signing in is optional everywhere in this app, so the
// copy leads with what an account is *for* rather than demanding one.
export default function AuthDialog({ mode: initialMode, onClose, onSignedIn }) {
  const [mode, setMode] = useState(initialMode ?? 'in')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const emailRef = useRef(null)

  const isSignup = mode === 'up'

  useEffect(() => {
    emailRef.current?.focus()
  }, [])

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function submit(e) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const user = isSignup
        ? await signUp(email, password, name)
        : await signIn(email, password)
      onSignedIn(user)
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <div className="modal-scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={isSignup ? 'Create account' : 'Sign in'}>
        <div className="modal-tabs">
          <button
            type="button"
            aria-pressed={!isSignup}
            onClick={() => { setMode('in'); setError('') }}
          >
            Sign in
          </button>
          <button
            type="button"
            aria-pressed={isSignup}
            onClick={() => { setMode('up'); setError('') }}
          >
            Create account
          </button>
        </div>

        <p className="modal-note">
          {isSignup
            ? 'An account keeps your conversations and raises your question limit. You can keep reading without one.'
            : 'Welcome back.'}
        </p>

        <form onSubmit={submit}>
          {isSignup && (
            <label className="field">
              <span>Name <em>optional</em></span>
              <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
            </label>
          )}

          <label className="field">
            <span>Email</span>
            <input
              ref={emailRef}
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
            />
          </label>

          <label className="field">
            <span>Password</span>
            <input
              type="password"
              required
              minLength={isSignup ? 8 : undefined}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={isSignup ? 'new-password' : 'current-password'}
            />
            {isSignup && <em className="hint">At least 8 characters.</em>}
          </label>

          {error && <div className="field-error">{error}</div>}

          <button className="primary-btn" type="submit" disabled={busy}>
            {busy ? 'One moment…' : isSignup ? 'Create account' : 'Sign in'}
          </button>
        </form>

        <button className="modal-close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
    </div>
  )
}
