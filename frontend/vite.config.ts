import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { computeUiBuildId } from "./scripts/ui-build-id.mjs";

const frontendRoot = fileURLToPath(new URL(".", import.meta.url));
const buildId = await computeUiBuildId(frontendRoot);

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/app/",
  define: {
    __PIKVM_UI_BUILD__: JSON.stringify(buildId),
  },
  resolve: {
    alias: {
      "@": resolve(frontendRoot, "src"),
    },
  },
  build: {
    outDir: "../pikvm_agent/harness_ui",
    emptyOutDir: true,
    cssCodeSplit: false,
    minify: "oxc",
    chunkSizeWarningLimit: 1_024,
    rollupOptions: {
      output: {
        entryFileNames: "app.js",
        assetFileNames: (assetInfo) =>
          assetInfo.name?.endsWith(".css") ? "styles.css" : "assets/[name][extname]",
        chunkFileNames: "assets/[name]-[hash].js",
      },
    },
  },
});
