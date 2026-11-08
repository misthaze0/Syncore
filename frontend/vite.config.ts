import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

/**
 * base 按 mode 区分：
 * - desktop（Electron 壳）：产物以 file:// 加载，`/assets/...` 这种绝对路径
 *   会解析到磁盘根目录而 404 —— 必须用相对路径 `./`；
 * - 其它（web / dev server）：保持默认的绝对路径。
 *
 * 多入口：主窗走 index.html，桌宠透明窗走 pet.html。两者独立打包，
 * 桌宠页只载入 Live2D + 极小 React，不拉主窗的 store/路由/终端等大依赖。
 */
export default defineConfig(({ mode }) => ({
  base: mode === 'desktop' ? './' : '/',
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // 开发环境把 /api 代理到后端，避免跨域
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        // 唤醒走 /api/voice/wake 的 WebSocket；不代理 upgrade 会握手失败
        ws: true,
      },
    },
  },
  build: {
    rollupOptions: {
      input: {
        index: path.resolve(__dirname, 'index.html'),
        pet: path.resolve(__dirname, 'pet.html'),
      },
    },
  },
}));
