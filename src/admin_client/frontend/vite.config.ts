import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      '/api_proxy': {
        target: 'https://admin.fin-ai-agent.ru',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api_proxy/, '')
      }
    },
    watch: {
      ignored: ["**/src-tauri/**"]
    }
  }
})
