import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// The backend is a separate process; the client reads its address from
// VITE_API_URL and falls back to the local development port.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  test: { environment: 'jsdom' },
})
