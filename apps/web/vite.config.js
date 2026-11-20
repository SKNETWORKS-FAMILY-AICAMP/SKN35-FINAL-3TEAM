import { defineConfig } from 'vite';

// /api 요청을 FastAPI 서버로 넘긴다 (같은 주소처럼 보여서 CORS 설정이 필요 없다).
// 서버 주소를 바꾸려면: VITE_API_TARGET=http://localhost:8001 npm run dev
export default defineConfig({
  server: {
    proxy: {
      '/api': { target: process.env.VITE_API_TARGET || 'http://localhost:8000', changeOrigin: true },
    },
  },
});
