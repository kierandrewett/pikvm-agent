import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/app/",
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
