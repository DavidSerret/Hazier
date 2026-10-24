// Benchmark hook, loaded only with ?perf in the URL. window.__hazier.bench(frames) moves the
// forecast hour one step every animation frame and reports the frame interval and the time
// spent rebuilding the layers in JavaScript.

type Hooks = {
  setHour: (hour: number) => void;
  draw: () => void;
  /** Screen position [x, y] in CSS pixels of a zone centre, to click on it in tests. */
  zonePixel: (id: string) => [number, number] | null;
  selectedZone: () => string | null;
};

export type BenchResult = {
  frames: number;
  fps: number;
  frameMs: { median: number; p95: number };
  layerBuildMs: { mean: number; p95: number };
};

const percentile = (sorted: number[], p: number) =>
  sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))] ?? 0;

export function installPerf(hooks: Hooks): { record: (ms: number) => void } {
  const builds: number[] = [];
  const record = (ms: number) => builds.push(ms);

  const nextFrame = () => new Promise<number>((resolve) => requestAnimationFrame(resolve));

  (window as unknown as Record<string, unknown>).__hazier = {
    zonePixel: hooks.zonePixel,
    selectedZone: hooks.selectedZone,
    setHour: hooks.setHour,
    async bench(frames = 240, maxHour = 72): Promise<BenchResult> {
      builds.length = 0;
      const intervals: number[] = [];
      let last = await nextFrame();
      for (let i = 0; i < frames; i++) {
        hooks.setHour(i % (maxHour + 1)); // one hour per frame: every frame changes every layer
        const now = await nextFrame();
        intervals.push(now - last);
        last = now;
      }
      intervals.sort((a, b) => a - b);
      const sortedBuilds = [...builds].sort((a, b) => a - b);
      const total = intervals.reduce((s, v) => s + v, 0);
      return {
        frames,
        fps: (frames / total) * 1000,
        frameMs: { median: percentile(intervals, 0.5), p95: percentile(intervals, 0.95) },
        layerBuildMs: {
          mean: builds.reduce((s, v) => s + v, 0) / Math.max(1, builds.length),
          p95: percentile(sortedBuilds, 0.95),
        },
      };
    },
  };
  return { record };
}
