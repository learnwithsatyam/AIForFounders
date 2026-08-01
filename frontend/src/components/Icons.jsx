// Small inline SVG icon set — sharper than the unicode glyphs and animatable.

const base = {
  width: 18,
  height: 18,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
}

export const SendIcon = () => (
  <svg {...base} aria-hidden="true">
    <path d="M5 12h13M12 5l7 7-7 7" />
  </svg>
)

export const StopIcon = () => (
  <svg {...base} aria-hidden="true">
    <rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none" />
  </svg>
)

export const MenuIcon = () => (
  <svg {...base} aria-hidden="true">
    <path d="M4 7h16M4 12h16M4 17h16" />
  </svg>
)

export const PlusIcon = () => (
  <svg {...base} width={16} height={16} aria-hidden="true">
    <path d="M12 5v14M5 12h14" />
  </svg>
)

export const CopyIcon = () => (
  <svg {...base} width={15} height={15} aria-hidden="true">
    <rect x="9" y="9" width="11" height="11" rx="2.5" />
    <path d="M5 15V6a2 2 0 0 1 2-2h9" />
  </svg>
)

export const CheckIcon = () => (
  <svg {...base} width={15} height={15} aria-hidden="true">
    <path d="M4 12.5l5 5 11-11" />
  </svg>
)

export const ArrowDownIcon = () => (
  <svg {...base} width={16} height={16} aria-hidden="true">
    <path d="M12 5v14M5 12l7 7 7-7" />
  </svg>
)

export const SunIcon = () => (
  <svg {...base} aria-hidden="true">
    <circle cx="12" cy="12" r="4.2" />
    <path d="M12 2v2.2M12 19.8V22M4.2 4.2l1.6 1.6M18.2 18.2l1.6 1.6M2 12h2.2M19.8 12H22M4.2 19.8l1.6-1.6M18.2 5.8l1.6-1.6" />
  </svg>
)

export const MoonIcon = () => (
  <svg {...base} aria-hidden="true">
    <path d="M20 14.2A8.2 8.2 0 0 1 9.8 4 8.2 8.2 0 1 0 20 14.2z" />
  </svg>
)

export const BookIcon = () => (
  <svg {...base} width={13} height={13} strokeWidth={2.2} aria-hidden="true">
    <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H19v15H6.5A2.5 2.5 0 0 0 4 20.5z" />
  </svg>
)
