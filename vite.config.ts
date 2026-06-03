import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  root: "dashboard_frontend",
  plugins: [react()],
  build: {
    outDir: "../src/maestro/dashboard/web",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8503",
        changeOrigin: true,
      },
    },
  },
});
