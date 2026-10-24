import { rmSync } from "node:fs";
import { resolve } from "node:path";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig, type Plugin } from "vite";

// The dev sample lives in public/ so the dev server can serve it, but it must not be published.
const dropSampleFromBuild = (): Plugin => ({
  name: "drop-sample-from-build",
  apply: "build",
  closeBundle() {
    rmSync(resolve(import.meta.dirname, "dist/forecast.sample.json"), { force: true });
  },
});

export default defineConfig({
  // Relative base so the build works under the GitHub Pages project path.
  base: "./",
  plugins: [tailwindcss(), dropSampleFromBuild()],
  optimizeDeps: {
    // MapLibre loads its web worker from a sibling file; pre-bundling would move the main
    // module and break that relative URL in dev.
    exclude: ["maplibre-gl"],
  },
});
