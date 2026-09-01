import { defineConfig } from "vitest/config";

// jsdom so the API client's `withLoading` (touches `document`) and the SSE helper run under
// test; only *.test.ts files are collected so the Vite build stays untouched.
export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts"],
  },
});
