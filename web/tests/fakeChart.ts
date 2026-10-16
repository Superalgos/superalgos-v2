// A fake Lightweight Charts chart that records calls and throws where the real library throws:
// removing an undefined, unknown or already-removed series, or touching a chart after remove().

export interface FakeSeries {
  id: number;
  kind: string;
  pane: number;
  data: unknown[];
  priceLines: number;
  setData(d: unknown[]): void;
  applyOptions(o: object): void;
  setDataCalls: number;
  priceScale(): { applyOptions(o: object): void };
  createPriceLine(o: object): object;
}

export class FakeChart {
  static all: FakeChart[] = [];
  static reset() {
    FakeChart.all = [];
  }
  static nextId = 1;

  calls: string[] = [];
  live = new Map<number, FakeSeries>();
  removed = false;
  removeCount = 0;
  crosshairHandlers = new Set<unknown>();
  maxPane = 0;

  constructor() {
    FakeChart.all.push(this);
  }

  private alive(op: string) {
    if (this.removed) throw new Error(`Object is disposed (${op} on a removed chart)`);
  }

  addSeries(def: { kind: string }, _opts: object, pane = 0): FakeSeries {
    this.alive("addSeries");
    const s: FakeSeries = {
      id: FakeChart.nextId++,
      kind: def.kind,
      pane,
      data: [],
      priceLines: 0,
      setDataCalls: 0,
      setData(d) {
        s.data = d;
        s.setDataCalls++;
      },
      applyOptions() {},
      priceScale: () => ({ applyOptions() {} }),
      createPriceLine() {
        s.priceLines++;
        return {};
      },
    };
    this.maxPane = Math.max(this.maxPane, pane);
    this.live.set(s.id, s);
    this.calls.push(`add:${def.kind}@${pane}`);
    return s;
  }

  removeSeries(s: FakeSeries | undefined) {
    this.alive("removeSeries");
    if (!s) throw new Error("Value is undefined");
    if (!this.live.delete(s.id)) throw new Error(`Value is undefined (series ${s.id} is not on this chart)`);
    this.calls.push(`remove:${s.kind}`);
  }

  panes() {
    this.alive("panes");
    return Array.from({ length: this.maxPane + 1 }, () => ({ setStretchFactor() {} }));
  }
  visible: { from: number; to: number } | null = null;
  fitCount = 0;
  timeScale() {
    this.alive("timeScale");
    return {
      fitContent: () => {
        this.fitCount++;
      },
      getVisibleRange: () => this.visible,
      setVisibleRange: (r: { from: number; to: number }) => {
        this.visible = r;
        this.calls.push(`range:${r.from}-${r.to}`);
      },
    };
  }
  applyOptions() {
    this.alive("applyOptions");
  }
  subscribeCrosshairMove(h: unknown) {
    this.alive("subscribeCrosshairMove");
    this.crosshairHandlers.add(h);
  }
  unsubscribeCrosshairMove(h: unknown) {
    this.alive("unsubscribeCrosshairMove");
    this.crosshairHandlers.delete(h);
  }
  remove() {
    if (this.removed) throw new Error("Object is disposed (chart.remove() called twice)");
    this.removed = true;
    this.removeCount++;
    this.calls.push("chart.remove");
  }
}

export const fakeDefs = {
  candle: { kind: "candle" },
  histogram: { kind: "histogram" },
  line: { kind: "line" },
};

export const fakeMarkers = {
  created: 0,
  detached: 0,
  sets: 0,
  last: [] as unknown[],
  create(_series: unknown, _marks: unknown[]) {
    fakeMarkers.created++;
    let done = false;
    return {
      setMarkers(m: unknown[]) {
        fakeMarkers.sets++;
        fakeMarkers.last = m;
      },
      detach() {
        if (done) throw new Error("markers plugin detached twice");
        done = true;
        fakeMarkers.detached++;
      },
    };
  },
};
