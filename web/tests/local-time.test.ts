import { afterAll, beforeEach, describe, expect, test } from "bun:test";
import fc from "fast-check";
import { axisLabels } from "../src/lib/chart";
import {
  chartTickLabel,
  chartTimeLabel,
  fmtClock,
  fmtTime,
  localOffsetMin,
  utcLabel,
  zonedClock,
  zonedDate,
  zonedParts,
} from "../src/lib/format";

const setZone = (tz: string) => {
  process.env.TZ = tz;
};
beforeEach(() => setZone("UTC"));
afterAll(() => setZone("UTC"));

const T = Date.UTC(2026, 9, 13, 16, 35, 5); // 2026-10-13 16:35:05 UTC (Europe in daylight saving)

describe("times render in the viewer's zone with the offset labelled", () => {
  test("Lisbon, UTC+1 in October", () => {
    setZone("Europe/Lisbon");
    expect(fmtClock(T)).toBe("17:35 (UTC+1)");
    expect(fmtTime(T)).toBe("2026-10-13 17:35:05 (UTC+1)");
  });
  test("UTC is labelled UTC", () => {
    expect(fmtClock(T)).toBe("16:35 (UTC)");
    expect(fmtTime(T)).toBe("2026-10-13 16:35:05 (UTC)");
  });
  test("half-hour and negative offsets", () => {
    setZone("Asia/Kolkata");
    expect(fmtTime(T)).toBe("2026-10-13 22:05:05 (UTC+5:30)");
    setZone("America/St_Johns");
    expect(fmtClock(T)).toBe("14:05 (UTC-2:30)");
    setZone("America/Sao_Paulo");
    expect(fmtClock(T)).toBe("13:35 (UTC-3)");
  });
  test("the offset follows daylight saving: the same zone, a winter instant", () => {
    setZone("Europe/Lisbon");
    expect(fmtClock(Date.UTC(2026, 0, 13, 16, 35))).toBe("16:35 (UTC)");
    expect(fmtClock(Date.UTC(2026, 6, 13, 16, 35))).toBe("17:35 (UTC+1)");
  });
  test("the date moves with the zone", () => {
    setZone("Pacific/Auckland");
    expect(fmtTime(Date.UTC(2026, 9, 13, 23, 0))).toBe("2026-10-14 12:00:00 (UTC+13)");
  });
  test("not finite is n/a", () => {
    expect(fmtTime(Number.NaN)).toBe("n/a");
    expect(fmtClock(Number.POSITIVE_INFINITY)).toBe("n/a");
  });
  test("chart axis labels are local, the zone goes in the caption", () => {
    setZone("Europe/Lisbon");
    const out = axisLabels([T, T + 15 * 60_000, T + 30 * 60_000]);
    expect(out.labels).toEqual(["17:35", "17:50", "18:05"]);
    expect(out.context).toBe("2026-10-13 (UTC+1)");
    const multi = axisLabels([T, T + 36 * 3_600_000]);
    expect(multi.labels[0]).toBe("10-13 17:35");
    expect(multi.context).toBe("times in UTC+1");
  });
  test("a day boundary is judged in local time", () => {
    setZone("Pacific/Auckland");
    // 23:00 and 00:30 UTC fall on two UTC days but on one local day (12:00 and 13:30, UTC+13)
    const a = Date.UTC(2026, 9, 13, 23, 0);
    expect(axisLabels([a, a + 90 * 60_000]).labels).toEqual(["12:00", "13:30"]);
  });
  test("candle chart labels: crosshair and ticks", () => {
    setZone("Europe/Lisbon");
    expect(chartTimeLabel(T / 1000)).toBe("2026-10-13 17:35 (UTC+1)");
    expect(chartTickLabel(T / 1000, 3)).toBe("17:35");
    expect(chartTickLabel(T / 1000, 4)).toBe("17:35:05");
    expect(chartTickLabel(T / 1000, 2)).toBe("13");
    expect(chartTickLabel(T / 1000, 1)).toBe("Oct");
    expect(chartTickLabel(T / 1000, 0)).toBe("2026");
    expect(chartTimeLabel("2026-10-13")).toBe("");
    expect(chartTickLabel({ year: 2026 }, 0)).toBe("");
  });
});

describe("properties", () => {
  const ms = fc.integer({ min: Date.UTC(1971, 0, 1), max: Date.UTC(2100, 0, 1) });
  const offset = fc.integer({ min: -12 * 4, max: 14 * 4 }).map((q) => q * 15);

  test("local fields shifted back by the offset are the instant", () => {
    fc.assert(
      fc.property(ms, offset, (t, off) => {
        const p = zonedParts(t, off);
        const back = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second, p.millis);
        expect(back - off * 60_000).toBe(t);
      }),
    );
  });
  test("labels are well formed for any offset", () => {
    fc.assert(
      fc.property(ms, offset, (t, off) => {
        expect(utcLabel(off)).toMatch(/^UTC([+-]\d{1,2}(:\d{2})?)?$/);
        expect(utcLabel(off) === "UTC").toBe(off === 0);
        expect(zonedDate(t, off)).toMatch(/^\d{4}-\d{2}-\d{2}$/);
        expect(zonedClock(t, off, true)).toMatch(/^\d{2}:\d{2}:\d{2}$/);
      }),
    );
  });
  test("the label's offset parses back to the offset", () => {
    fc.assert(
      fc.property(offset, (off) => {
        const m = /^UTC(?:([+-])(\d+)(?::(\d+))?)?$/.exec(utcLabel(off));
        const back = m?.[1] ? (m[1] === "-" ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3] ?? 0)) : 0;
        expect(back).toBe(off);
      }),
    );
  });
  test("fmtTime shows the machine's offset at that instant, in every zone", () => {
    const zones = ["UTC", "Europe/Lisbon", "Asia/Kolkata", "America/St_Johns", "Pacific/Auckland"];
    fc.assert(
      fc.property(ms, fc.constantFrom(...zones), (t, tz) => {
        setZone(tz);
        const off = localOffsetMin(t);
        expect(fmtTime(t).endsWith(`(${utcLabel(off)})`)).toBe(true);
        const [d, c] = fmtTime(t).split(" ");
        const [y, mo, da] = (d ?? "").split("-").map(Number);
        const [h, mi, s] = (c ?? "").split(":").map(Number);
        expect(Date.UTC(y ?? 0, (mo ?? 1) - 1, da, h, mi, s) - off * 60_000).toBe(Math.floor(t / 1000) * 1000);
      }),
    );
  });
});
