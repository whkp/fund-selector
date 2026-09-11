/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * 后端 API 基址，例如 https://<你的服务>.onrender.com/api
   * 未设置时回退到同源 /api —— 注意空字符串同样会触发回退（见 fund-api.ts）。
   */
  readonly VITE_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
