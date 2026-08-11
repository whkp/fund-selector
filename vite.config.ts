import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const runtimeGlobal = globalThis as typeof globalThis & {
  process?: { env?: Record<string, string | undefined> }
}
const isGitHubActions = Boolean(runtimeGlobal.process?.env?.GITHUB_ACTIONS)

export default defineConfig({
  base: isGitHubActions ? '/fund-selector/' : '/',
  plugins: [vue()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
      },
    },
  },
})
