import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";

// `npm run build` -> web/dist, which ai_civ.web serves at "/". `npm run dev` proxies /api to the Python backend.
export default defineConfig({
  plugins: [tailwindcss(), svelte()],
  build: { outDir: "dist", emptyOutDir: true, target: "es2020" },
  server: { host: "127.0.0.1", port: 5173, proxy: { "/api": "http://127.0.0.1:8080" } },
});
