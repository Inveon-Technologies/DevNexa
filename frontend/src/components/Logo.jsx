/**
 * DevNexa mark: a blue tile with a white "N" drawn as a connected path, with nodes at each joint,
 * for the build -> monitor -> fix loop the agents run.
 */
export default function Logo({ className = "h-9 w-9" }) {
  return (
    <svg viewBox="0 0 40 40" className={className} role="img" aria-label="DevNexa">
      <defs>
        <linearGradient id="devnexa-logo" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#1e40af" />
          <stop offset="0.55" stopColor="#2563eb" />
          <stop offset="1" stopColor="#38bdf8" />
        </linearGradient>
      </defs>
      <rect width="40" height="40" rx="11" fill="url(#devnexa-logo)" />
      <path d="M12 28V12l16 16V12" fill="none" stroke="#fff" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="12" cy="12" r="3" fill="#fff" />
      <circle cx="28" cy="28" r="3" fill="#fff" />
      <circle cx="28" cy="12" r="2.2" fill="#bfdbfe" />
      <circle cx="12" cy="28" r="2.2" fill="#bfdbfe" />
    </svg>
  );
}
