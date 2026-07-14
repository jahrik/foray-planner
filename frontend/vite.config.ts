import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

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
  plugins: [
    VitePWA({
      // Installability only, deliberately no offline response/data caching: connectivity
      // isn't a real problem here (Starlink), so this just unlocks "Add to Home Screen"
      // on Android/Chrome (which requires a registered service worker to offer the
      // install prompt) rather than shipping a caching layer nobody needs.
      registerType: "prompt",
      injectRegister: "auto",
      workbox: {
        // An empty precache/runtime-caching config still satisfies the installability
        // requirement without caching any app data.
        globPatterns: [],
        runtimeCaching: [],
      },
      manifest: {
        name: "Foray Planner",
        short_name: "Foray",
        description: "Mushroom-hunting road-trip planner: destinations, camping, trails.",
        theme_color: "#12140f",
        background_color: "#12140f",
        display: "standalone",
        icons: [
          { src: "pwa-192.png", sizes: "192x192", type: "image/png" },
          { src: "pwa-512.png", sizes: "512x512", type: "image/png" },
        ],
      },
    }),
  ],
});
