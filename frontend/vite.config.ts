import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server proxies /api to the FastAPI backend. 127.0.0.1, not "localhost": Node 17+
// resolves localhost to ::1 (IPv6) first on macOS, and uvicorn listens on IPv4 only, so every
// call would be refused. Override with API_PORT or VITE_API_TARGET.
const target = process.env.VITE_API_TARGET ?? `http://127.0.0.1:${process.env.API_PORT ?? 8000}`

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target,
        configure: proxy => {
          proxy.on('error', (err: NodeJS.ErrnoException) => {
            console.error(`\n[ignite] Can't reach the API at ${target} (${err.code ?? err.message}). ` +
              'Is `make dev-api` running? If it uses another port, start the UI with API_PORT=<port> make dev-web.\n')
          })
        },
      },
    },
  },
})
