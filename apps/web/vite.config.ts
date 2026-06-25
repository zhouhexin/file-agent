import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Vite 只负责前端开发服务器；后端 API 地址由 src/api/client.ts 统一配置。
export default defineConfig({
  plugins: [react()],
  server: {
    // 固定开发端口，避免自动漂移到后端 CORS 未放行的端口。
    port: 5173,
    strictPort: true,
  },
});
