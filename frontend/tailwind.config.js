/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // Light palette. Warm rather than white: the page is paper, the cards sit on it.
        // ⚠️ Charts cannot use these names — ECharts draws to canvas, where a Tailwind class
        //    means nothing — so the same values are written literally in the chart options.
        //    Change one here and the charts drift out of step with the page around them.
        bg:      "#faf8f5",   // paper
        card:    "#ffffff",
        card2:   "#f2efe9",   // a second surface, slightly recessed
        line:    "#e2ddd4",
        ink:     "#1a1815",   // warm near-black
        dim:     "#6b665e",   // ~5.5:1 on paper — readable, still clearly secondary
        brand:   "#d4400d",   // vermilion, darkened: #ff5a1f reads at 3:1 on white, too weak for text
        pos:     "#15803d",
        neg:     "#b91c1c",
        info:    "#1d4ed8",
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', "SFMono-Regular", "Menlo", "monospace"],
        sans: ['"Space Grotesk"', '"PingFang SC"', "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
}
