import type { Json } from "../../src/lib/designer/doc";

const event = (name: string, conds: string[]): Json => ({ any: [{ name, all: conds }, { name: `${name} b`, all: conds }] });

/** A schema-shaped strategy with about `orders * 22` nodes: each order has create and cancel events, a rate and a size formula. */
export function bigStrategy(orders: number): Json {
  const list: Json[] = [];
  for (let i = 0; i < orders; i++) {
    list.push({
      id: `level_${String(i + 1).padStart(2, "0")}`,
      type: "limit",
      rate: `close - ${i + 1} * atr(14)`,
      size: `0.05 / ${i + 1}`,
      create: event(`create ${i}`, ["rsi(close, 14) > 30", "close > ema(close, 50)"]),
      cancel: event(`cancel ${i}`, ["rsi(close, 14) < 20"]),
      rearm: true,
    });
  }
  return {
    schema_version: 2,
    name: "big",
    description: "",
    timeframe: "15m",
    stages: {
      trigger: { on: event("on", ["true"]) },
      open: { when: event("open", ["true"]), orders: list },
      close: { when: event("close", ["false"]) },
    },
  };
}
