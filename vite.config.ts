import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  root: "dashboard_frontend_next",
  plugins: [react()],
  build: {
    outDir: "../src/maestro/dashboard/web",
    emptyOutDir: true,
  },
});
