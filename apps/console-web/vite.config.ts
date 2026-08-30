import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// API 地址在运行时通过 window.__RUNBOOKGUARD_API_BASE__ 注入（见 index.html），
// 不在构建期烧进 bundle：烧进去会让同一份产物无法部署到两个环境，
// 而 M7 Gate 要求「干净机器照 README 就能跑起来」。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
  test: {
    globals: true,
    environment: "node",
  },
});
