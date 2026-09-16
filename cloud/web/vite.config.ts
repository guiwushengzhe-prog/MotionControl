import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// The built site is served by FastAPI itself, from cloud/web/dist. That keeps
// the page and the API on one origin, which is why there is no CORS
// configuration anywhere in this project -- there is nothing cross-origin to
// allow. The dev server proxies /api to the same place so development matches.
export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // The API is small and so is this site; one chunk loads faster than four.
    chunkSizeWarningLimit: 700,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: false, // same-origin semantics, so the cookie behaves
      },
    },
  },
});
