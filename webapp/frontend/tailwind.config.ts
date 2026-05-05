import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Refererar CSS-variabler i index.css så de kan bytas via tema-toggle
        bg:        "rgb(var(--bg) / <alpha-value>)",
        surface:   "rgb(var(--surface) / <alpha-value>)",
        elevated:  "rgb(var(--elevated) / <alpha-value>)",
        border:    "rgb(var(--border) / <alpha-value>)",
        fg:        "rgb(var(--fg) / <alpha-value>)",
        "fg-muted":"rgb(var(--fg-muted) / <alpha-value>)",
        accent:    "rgb(var(--accent) / <alpha-value>)",
        positive:  "rgb(var(--positive) / <alpha-value>)",
        negative:  "rgb(var(--negative) / <alpha-value>)",
        warn:      "rgb(var(--warn) / <alpha-value>)",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      fontSize: {
        "2xs": "0.6875rem",
      },
      transitionDuration: {
        "180": "180ms",
      },
    },
  },
  plugins: [],
} satisfies Config;
