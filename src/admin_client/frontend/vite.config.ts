import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api_proxy': {
        target: 'https://admin.fin-ai-agent.ru',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api_proxy/, '')
      }
    }
  }
})
