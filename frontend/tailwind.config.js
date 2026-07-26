/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // Vibe-Trading 同款视觉
        bg:      "#0a0a0a",
        card:    "#131316",
        card2:   "#1b1b20",
        line:    "#2a2a31",
        ink:     "#f2efe9",
        dim:     "#8e8a83",
        brand:   "#ff5a1f",   // 朱橙
        pos:     "#22c55e",
        neg:     "#ef4444",
        info:    "#3b82f6",
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', "SFMono-Regular", "Menlo", "monospace"],
        sans: ['"Space Grotesk"', '"PingFang SC"', "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
}
