// Written the way a founder would actually type the question, with a short
// label so the four cards scan in a glance rather than being read in full.
const SUGGESTIONS = [
  { label: 'Build vs buy', text: 'Should I build my own AI tool or just buy one?' },
  { label: 'Getting started', text: "I'm non-technical. Where do I actually start?" },
  { label: 'Hidden costs', text: 'Which AI costs does the book say founders underestimate?' },
  { label: 'The big idea', text: "Give me the book's core argument in three sentences." },
]

export default function WelcomeScreen({ onPick }) {
  return (
    <div className="welcome">
      <h1 style={{ '--i': 0 }}>Hello, founder.</h1>
      <h2 style={{ '--i': 1 }}>What are you trying to figure out?</h2>
      <div className="chips">
        {SUGGESTIONS.map((s, i) => (
          <button
            key={s.text}
            className="chip"
            style={{ '--i': i + 2 }}
            onClick={() => onPick(s.text)}
          >
            <span className="chip-head">
              <span className="chip-label">{s.label}</span>
              <span className="chip-arrow" aria-hidden="true">
                →
              </span>
            </span>
            <span className="chip-text">{s.text}</span>
          </button>
        ))}
      </div>
    </div>
  )
}
