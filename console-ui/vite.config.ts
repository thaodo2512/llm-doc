import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev: `npm run dev` serves on :5173 and proxies the API to the host console (:8765).
// Prod: the host uvicorn serves the built dist/ + API same-origin (no proxy, no CORS).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/healthz": "http://127.0.0.1:8765",
    },
  },
});
