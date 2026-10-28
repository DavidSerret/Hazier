// Preview the app with a real, fresh forecast: npm run preview:live (from web/).
//
// 1. Runs the pipeline (python -m hazier) with the virtual environment in pipeline/.venv.
// 2. Builds the web app (npm run build: type check, then vite build).
// 3. Copies pipeline/output/site/forecast.json into the built site (dist/).
// 4. Starts the preview server and prints its local URL.
//
// npm run preview:live -- --skip-pipeline   reuses the last forecast.json (quick restarts)
//
// If the pipeline fails, nothing is built or served. Pipeline runs from here pass --no-history,
// so previews do not add lines to the local run history.

import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const WEB_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");
const PIPELINE_DIR = join(WEB_DIR, "..", "pipeline");
const FORECAST = join(PIPELINE_DIR, "output", "site", "forecast.json");
const DIST = join(WEB_DIR, "dist");
const WINDOWS = process.platform === "win32";
const PYTHON = WINDOWS
  ? join(PIPELINE_DIR, ".venv", "Scripts", "python.exe")
  : join(PIPELINE_DIR, ".venv", "bin", "python");

const args = process.argv.slice(2);
const skipPipeline = args.includes("--skip-pipeline");
const unknown = args.filter((a) => a !== "--skip-pipeline");

const step = (n, text) => console.log(`\n[${n}/4] ${text}`);
function fail(message) {
  console.error(`\npreview:live stopped: ${message}`);
  process.exit(1);
}

if (unknown.length) fail(`unknown option ${unknown.join(" ")} (the only option is --skip-pipeline)`);

// 1. Pipeline
if (skipPipeline) {
  step(1, "Pipeline skipped (--skip-pipeline): reusing the last forecast.json");
} else {
  if (!existsSync(PYTHON)) {
    fail(
      `no Python found at ${relative(WEB_DIR, PYTHON)}.\n` +
        "Create the pipeline environment first (see the README, Run locally):\n" +
        (WINDOWS
          ? '  cd pipeline\n  py -3.12 -m venv .venv\n  .venv\\Scripts\\python.exe -m pip install -e ".[dev]"'
          : '  cd pipeline\n  python3 -m venv .venv\n  .venv/bin/python -m pip install -e ".[dev]"'),
    );
  }
  step(1, "Running the pipeline (python -m hazier), usually 3 to 5 minutes");
  const run = spawnSync(PYTHON, ["-m", "hazier", "--no-history"], {
    cwd: PIPELINE_DIR,
    stdio: "inherit",
  });
  if (run.error) fail(`could not start the pipeline (${run.error.message})`);
  if (run.status !== 0) {
    const why = {
      1: "no usable forecast could be made (FIRMS or the winds are unavailable, or a key is missing in pipeline/.env)",
      2: "the forecast failed validation",
    };
    fail(
      `the pipeline exited with code ${run.status}: ${why[run.status] ?? "unexpected error"}.\n` +
        "See its summary above. Nothing was built or served. To preview the previous forecast " +
        "anyway, run: npm run preview:live -- --skip-pipeline",
    );
  }
}

if (!existsSync(FORECAST)) {
  fail(
    `no forecast at ${relative(WEB_DIR, FORECAST)}. Run without --skip-pipeline first, ` +
      "or run the pipeline yourself (python -m hazier in pipeline/).",
  );
}
let generatedAt;
try {
  generatedAt = JSON.parse(readFileSync(FORECAST, "utf8")).generated_at;
} catch {
  fail(`${relative(WEB_DIR, FORECAST)} is not valid JSON. Run the pipeline again.`);
}
const ageHours = (Date.now() - Date.parse(generatedAt)) / 3_600_000;
console.log(
  `Forecast: generated_at ${generatedAt} (${ageHours.toFixed(1)} h ago)` +
    (ageHours > 9 ? ", older than 9 h: the app will flag it as stale" : ""),
);

// 2. Build. npm is a .cmd script on Windows, which Node only starts through a shell.
step(2, "Building the web app (npm run build)");
const build = spawnSync("npm run build", { cwd: WEB_DIR, stdio: "inherit", shell: true });
if (build.status !== 0) fail("the web build failed (see the errors above)");

// 3. Copy the forecast into the built site, where the production app looks for it.
step(3, "Copying forecast.json into dist/");
copyFileSync(FORECAST, join(DIST, "forecast.json"));

// 4. Preview, through Vite's API so the actual URL (the port may change) can be printed.
step(4, "Starting the preview server");
const { preview } = await import("vite");
const server = await preview({ root: WEB_DIR });
const urls = server.resolvedUrls?.local ?? [];
console.log(`\nHazier is running with the ${skipPipeline ? "last" : "fresh"} forecast:`);
for (const url of urls) console.log(`  ${url}`);
console.log("Press Ctrl+C to stop.");
