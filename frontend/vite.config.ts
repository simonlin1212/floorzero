import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5895,
    // 代理到本地后端：前端永远只连 localhost，
    // 这也是「用户自部署、数据不出本机」的一部分
    proxy: { '/api': { target: 'http://127.0.0.1:8920', changeOrigin: true } },
  },
})
