import { describe, expect, it } from "vitest";
import { advance, nowHour, relativeLabel, timelineTicks } from "./timeline";
import type { ZoneForecast } from "./types";
import { etaText, originRows, originsToShow } from "./zone";

const zone = (over: Partial<ZoneForecast>): ZoneForecast => ({
  id: "z",
  name: "Zone",
  lat: 0,
  lon: 0,
  radius_km: 50,
  smoke_eta_hours: null,
  smoke_eta_earliest_hours: null,
  smoke_eta_latest_hours: null,
  smoke_sources: [],
  cams_now: null,
  cams_peak: null,
  cams_trend: null,
  cams_pm25: [],
  agreement: null,
  ...over,
});

describe("etaText", () => {
  it("says no smoke when there is no ETA", () => {
    expect(etaText(zone({}))).toBe("No smoke expected in the next 72 h");
  });

  it("says already present for ETA 0", () => {
    expect(etaText(zone({ smoke_eta_hours: 0 }))).toBe("Smoke already present");
  });

  it("gives a range", () => {
    const z = zone({ smoke_eta_hours: 5, smoke_eta_earliest_hours: 4, smoke_eta_latest_hours: 6 });
    expect(etaText(z)).toBe("Smoke expected in 4 to 6 h");
  });

  it("takes the age of the forecast off", () => {
    const z = zone({ smoke_eta_hours: 12, smoke_eta_earliest_hours: 11, smoke_eta_latest_hours: 13 });
    expect(etaText(z, 5)).toBe("Smoke expected in 6 to 8 h");
  });

  it("says already present once the ETA has passed", () => {
    const z = zone({ smoke_eta_hours: 4, smoke_eta_earliest_hours: 3, smoke_eta_latest_hours: 5 });
    expect(etaText(z, 4)).toBe("Smoke already present");
    expect(etaText(z, 5)).toBe("Smoke already present");
  });

  it("collapses a range that rounds to one value", () => {
    const z = zone({ smoke_eta_hours: 7, smoke_eta_earliest_hours: 7, smoke_eta_latest_hours: 7.2 });
    expect(etaText(z)).toBe("Smoke expected in about 7 h");
  });

  it("flags an open ended range", () => {
    const z = zone({ smoke_eta_hours: 70, smoke_eta_earliest_hours: 68, smoke_eta_latest_hours: null });
    expect(etaText(z)).toBe("Smoke expected in about 68 h, possibly later");
  });

  it("never says in 0 h", () => {
    const z = zone({ smoke_eta_hours: 5.4, smoke_eta_earliest_hours: 5, smoke_eta_latest_hours: 6 });
    expect(etaText(z, 4.6)).toMatch(/in (about )?1/);
  });
});

describe("originsToShow", () => {
  const sources = [{ region: "West Sumatra", share: 1 }];

  it("hides the sources of a zone the smoke does not reach", () => {
    expect(originsToShow(zone({ smoke_sources: sources }))).toEqual([]);
  });

  it("shows them when smoke arrives", () => {
    const z = zone({ smoke_eta_hours: 0, smoke_sources: sources });
    expect(originsToShow(z)).toEqual([{ region: "West Sumatra", percent: 100 }]);
  });
});

describe("originRows", () => {
  it("adds up to 100", () => {
    const rows = originRows([
      { region: "A", share: 0.333 },
      { region: "B", share: 0.333 },
      { region: "C", share: 0.334 },
    ]);
    expect(rows.reduce((s, r) => s + r.percent, 0)).toBe(100);
  });

  it("folds tiny shares into Other", () => {
    const rows = originRows([
      { region: "Riau", share: 0.967 },
      { region: "Jambi", share: 0.031 },
      { region: "A", share: 0.001 },
      { region: "B", share: 0.001 },
    ]);
    expect(rows.map((r) => r.region)).toEqual(["Riau", "Jambi", "Other"]);
    expect(rows.reduce((s, r) => s + r.percent, 0)).toBe(100);
  });

  it("is empty without sources", () => {
    expect(originRows([])).toEqual([]);
  });

  it("keeps a single source at 100", () => {
    expect(originRows([{ region: "Riau", share: 1 }])).toEqual([{ region: "Riau", percent: 100 }]);
  });
});

describe("timeline", () => {
  // 2026-09-14 06:00 UTC = 14:00 MYT on a Monday.
  const t0 = "2026-09-14T06:00:00Z";

  it("puts day ticks at midnight Malaysia time", () => {
    const days = timelineTicks(t0, 72, 0).filter((t) => t.kind === "day");
    expect(days.map((t) => t.hour)).toEqual([10, 34, 58]);
    expect(days.map((t) => t.label)).toEqual(["Tue", "Wed", "Thu"]);
  });

  it("marks now at the age of the forecast", () => {
    const now = timelineTicks(t0, 72, 3).find((t) => t.kind === "now");
    expect(now?.hour).toBe(3);
  });

  it("keeps now inside the slider", () => {
    expect(nowHour(-1, 72)).toBe(0);
    expect(nowHour(100, 72)).toBe(72);
  });

  it("drops a day label that would crowd Now", () => {
    // Midnight is at hour 10; with now at 5 it is 5 h away, too close.
    const days = timelineTicks(t0, 72, 5).filter((t) => t.kind === "day");
    expect(days.map((t) => t.hour)).toEqual([34, 58]);
  });

  it("has no minor tick on top of a labelled one", () => {
    const hours = timelineTicks(t0, 72, 0).map((t) => t.hour);
    expect(hours.filter((h) => h === 0)).toHaveLength(1);
  });

  it("advances and loops", () => {
    expect(advance(10, 1, 72, 3)).toBe(13);
    expect(advance(71.5, 1, 72, 3)).toBeCloseTo(2.5);
  });

  it("has no Now tick on a forecast older than its horizon", () => {
    expect(timelineTicks(t0, 72, 100).some((t) => t.kind === "now")).toBe(false);
    expect(relativeLabel(5, 100, 72)).toBe("+5 h");
    expect(relativeLabel(0, 100, 72)).toBe("Forecast start");
  });

  it("labels the position relative to now", () => {
    expect(relativeLabel(3, 3, 72)).toBe("Now");
    expect(relativeLabel(21, 3, 72)).toBe("In 18 h");
    expect(relativeLabel(0, 3, 72)).toBe("3 h ago");
  });
});
