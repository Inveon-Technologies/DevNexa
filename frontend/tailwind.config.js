import colors from "tailwindcss/colors";

/*
 * Light "blue and white" theme.
 *
 * The components were written for a dark UI (light text shades such as text-slate-200 or text-rose-300,
 * white-alpha borders, ink surfaces). Instead of touching every class, the palette is remapped here:
 *   - ink    -> white / blue-tinted white surfaces
 *   - white  -> deep navy, so headings (text-white) and hairlines (border-white/[0.06]) stay readable on white
 *   - black  -> navy, so code panels (bg-black/40) become a soft blue-grey tint
 *   - slate and the status colours have their light shades swapped for dark ones, so text keeps its contrast
 *   - indigo -> blue (primary), violet/fuchsia -> sky blue (secondary accent)
 * `snow` is real white for text on coloured buttons.
 */
const NAVY = "#0b1b3f";

/** Swap the light end of a palette for its dark end, so "text-x-300" reads well on white. */
const forLight = (p) => ({
  ...p,
  50: p[950] || p[900],
  100: p[900],
  200: p[800],
  300: p[700],
  400: p[600],
  500: p[500],
  600: p[600],
});

export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      colors: {
        snow: "#ffffff",
        white: NAVY,
        black: NAVY,
        navy: NAVY,
        ink: { 950: "#f3f6fd", 900: "#ffffff", 850: "#f7f9ff", 800: "#eef2fb", 700: "#e1e7f5", 600: "#c9d3ea" },
        slate: {
          50: "#050d22", 100: NAVY, 200: "#1b2a4e", 300: "#2f3f66", 400: "#4e5c80", 500: "#68738f",
          600: "#97a1b9", 700: "#c7cee0", 800: "#e2e7f2", 900: "#eef2f9", 950: "#f6f8fc",
        },
        indigo: { ...forLight(colors.blue), 500: colors.blue[600], 600: colors.blue[700] },
        violet: forLight(colors.sky),
        fuchsia: forLight(colors.sky),
        sky: forLight(colors.sky),
        cyan: forLight(colors.blue),
        emerald: forLight(colors.emerald),
        amber: { ...forLight(colors.amber), 400: colors.amber[500] },
        rose: forLight(colors.rose),
      },
      backgroundImage: {
        brand: "linear-gradient(135deg, #1e40af 0%, #2563eb 55%, #38bdf8 100%)",
      },
      boxShadow: {
        glow: "0 0 0 1px rgba(37,99,235,.18), 0 10px 30px -14px rgba(37,99,235,.55)",
        card: "0 1px 2px rgba(11,27,63,.05), 0 8px 24px -18px rgba(37,99,235,.35)",
      },
      keyframes: { pulseDot: { "0%,100%": { opacity: 1 }, "50%": { opacity: 0.35 } } },
      animation: { pulseDot: "pulseDot 1.4s ease-in-out infinite" },
    },
  },
  plugins: [],
};
