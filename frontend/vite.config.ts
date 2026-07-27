import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5895,
    // Proxy to the local backend. The frontend only ever talks to localhost —
    // part of the same promise: you self-host, and the data never leaves your machine.
    proxy: { '/api': { target: 'http://127.0.0.1:8920', changeOrigin: true } },
  },
})
