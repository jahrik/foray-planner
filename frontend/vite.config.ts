import { defineConfig } from "vite";

// The FastAPI backend serves the built bundle (see src/foray/api.py). In dev we run
// Vite's own server and proxy the JSON API + refresh endpoints through to uvicorn.
export default defineConfig({
  build: {
    // Emit straight into the Python package so the wheel and Docker image ship the
    // built client. Gitignored; rebuilt by `npm run build`.
    outDir: "../src/foray/web/dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
