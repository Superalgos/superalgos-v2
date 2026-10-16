export function fmtPct(n: number, digits = 1, signed = false): string {
  if (!Number.isFinite(n)) return "n/a";
  const s = n.toFixed(digits);
  return `${signed && n > 0 ? "+" : ""}${s}%`;
}

/** Format a 0..1 fraction as a percentage. */
export function fmtFraction(f: number, digits = 0): string {
  return fmtPct(f * 100, digits);
}

export function fmtNum(n: number, digits = 0): string {
  if (!Number.isFinite(n)) return "n/a";
  return n.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function fmtCompact(n: number): string {
  if (!Number.isFinite(n)) return "n/a";
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return n.toFixed(0);
}

export function shortKey(key: string, head = 6, tail = 4): string {
  if (key.length <= head + tail + 1) return key;
  return `${key.slice(0, head)}…${key.slice(-tail)}`;
}

const pad = (n: number, width = 2) => String(n).padStart(width, "0");

/** Offset label for a zone `offsetMin` minutes east of UTC: "UTC", "UTC+1", "UTC+5:30", "UTC-3:30". */
export function utcLabel(offsetMin: number): string {
  if (offsetMin === 0) return "UTC";
  const abs = Math.abs(offsetMin);
  const h = Math.floor(abs / 60);
  const m = abs % 60;
  return `UTC${offsetMin < 0 ? "-" : "+"}${h}${m ? `:${pad(m)}` : ""}`;
}

/** The viewer's UTC offset at instant `ms`, in minutes east of UTC (daylight saving included). */
export function localOffsetMin(ms: number): number {
  const off = -new Date(ms).getTimezoneOffset();
  return off === 0 ? 0 : off; // never -0
}

/** Calendar fields of `ms` in a zone `offsetMin` minutes east of UTC. */
export function zonedParts(ms: number, offsetMin: number) {
  const d = new Date(ms + offsetMin * 60_000);
  return {
    year: d.getUTCFullYear(),
    month: d.getUTCMonth() + 1,
    day: d.getUTCDate(),
    hour: d.getUTCHours(),
    minute: d.getUTCMinutes(),
    second: d.getUTCSeconds(),
    millis: d.getUTCMilliseconds(),
  };
}

/** "2026-10-09" in the zone `offsetMin`. */
export function zonedDate(ms: number, offsetMin: number): string {
  const p = zonedParts(ms, offsetMin);
  return `${pad(p.year, 4)}-${pad(p.month)}-${pad(p.day)}`;
}

/** "23:00" (or "23:00:05" with `seconds`) in the zone `offsetMin`, no label. */
export function zonedClock(ms: number, offsetMin: number, seconds = false): string {
  const p = zonedParts(ms, offsetMin);
  return `${pad(p.hour)}:${pad(p.minute)}${seconds ? `:${pad(p.second)}` : ""}`;
}

/** Timestamp in the viewer's local zone with its offset labelled: "2026-10-09 23:00:00 (UTC+1)". */
export function fmtTime(ms: number): string {
  if (!Number.isFinite(ms)) return "n/a";
  const off = localOffsetMin(ms);
  return `${zonedDate(ms, off)} ${zonedClock(ms, off, true)} (${utcLabel(off)})`;
}

/** Time of day in the viewer's local zone with its offset labelled: "23:00 (UTC+1)". */
export function fmtClock(ms: number): string {
  if (!Number.isFinite(ms)) return "n/a";
  const off = localOffsetMin(ms);
  return `${zonedClock(ms, off)} (${utcLabel(off)})`;
}

export function fmtDuration(ms: number): string {
  if (!Number.isFinite(ms)) return "n/a";
  const sign = ms < 0 ? "-" : "";
  let s = Math.round(Math.abs(ms) / 1000);
  if (s < 60) return `${sign}${s}s`;
  const m = Math.floor(s / 60);
  s -= m * 60;
  if (m < 60) return s ? `${sign}${m}m ${s}s` : `${sign}${m}m`;
  const h = Math.floor(m / 60);
  const rm = m - h * 60;
  if (h < 48) return rm ? `${sign}${h}h ${rm}m` : `${sign}${h}h`;
  return `${sign}${Math.floor(h / 24)}d`;
}

export function fmtTimeframe(seconds: number): string {
  return fmtDuration(seconds * 1000);
}

export function signClass(n: number): "pos" | "neg" | "flat" {
  if (n > 0) return "pos";
  if (n < 0) return "neg";
  return "flat";
}

/** Crosshair time label of a chart whose times are epoch seconds: "2026-10-09 23:00 (UTC+1)". */
export function chartTimeLabel(time: unknown): string {
  if (typeof time !== "number") return "";
  const ms = time * 1000;
  const off = localOffsetMin(ms);
  return `${zonedDate(ms, off)} ${zonedClock(ms, off)} (${utcLabel(off)})`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/**
 * Time-axis tick label of a chart whose times are epoch seconds, in the viewer's local zone.
 * `type` is Lightweight Charts' TickMarkType: 0 year, 1 month, 2 day of month, 3 time, 4 time with seconds.
 */
export function chartTickLabel(time: unknown, type: number): string {
  if (typeof time !== "number") return "";
  const ms = time * 1000;
  const p = zonedParts(ms, localOffsetMin(ms));
  if (type === 0) return String(p.year);
  if (type === 1) return MONTHS[p.month - 1] ?? "";
  if (type === 2) return String(p.day);
  return zonedClock(ms, localOffsetMin(ms), type === 4);
}
