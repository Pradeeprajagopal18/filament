import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server proxies /api to the FastAPI backend on :8000.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { '/api': process.env.VITE_API_TARGET ?? 'http://localhost:8000' },
  },
})
