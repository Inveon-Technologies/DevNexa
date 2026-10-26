/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      colors: {
        ink: { 950: "#070a13", 900: "#0b1020", 850: "#0f1529", 800: "#141b33", 700: "#1e2745", 600: "#2a3558" },
      },
      boxShadow: { glow: "0 0 0 1px rgba(129,140,248,.25), 0 8px 30px -12px rgba(99,102,241,.45)" },
      keyframes: { pulseDot: { "0%,100%": { opacity: 1 }, "50%": { opacity: 0.35 } } },
      animation: { pulseDot: "pulseDot 1.4s ease-in-out infinite" },
    },
  },
  plugins: [],
};
