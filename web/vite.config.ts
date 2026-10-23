import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  // Relative base so the build works under the GitHub Pages project path.
  base: "./",
  plugins: [tailwindcss()],
  optimizeDeps: {
    // MapLibre loads its web worker from a sibling file; pre-bundling would move the main
    // module and break that relative URL in dev.
    exclude: ["maplibre-gl"],
  },
});
