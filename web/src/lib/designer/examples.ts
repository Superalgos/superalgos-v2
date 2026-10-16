/** The strategies shipped with the engine, bundled: the designer opens them without any server. */
import ema from "../../../../packages/sa-engine/examples/ema_crossover.json";
import grid from "../../../../packages/sa-engine/examples/grid_10_level.json";
import ladder from "../../../../packages/sa-engine/examples/ladder_rearm.json";
import rsi from "../../../../packages/sa-engine/examples/rsi_mean_reversion.json";
import trend from "../../../../packages/sa-engine/examples/trend_phases.json";
import type { Json } from "./doc";

export interface Example {
  /** File name in `packages/sa-engine/examples/`. */
  file: string;
  doc: Json;
}

export const EXAMPLES: readonly Example[] = [
  { file: "ema_crossover.json", doc: ema as Json },
  { file: "rsi_mean_reversion.json", doc: rsi as Json },
  { file: "grid_10_level.json", doc: grid as Json },
  { file: "trend_phases.json", doc: trend as Json },
  { file: "ladder_rearm.json", doc: ladder as Json },
];

/** A blank, valid-looking starting point: the smallest document the schema's required fields describe. */
export const NEW_STRATEGY: Json = {
  schema_version: 2,
  name: "new-strategy",
  description: "",
  timeframe: "1h",
  stages: {
    trigger: { on: { any: [{ name: "always", all: ["true"] }] } },
    open: {
      when: {
        any: [{ name: "entry signal", all: ["cross_above(ema(close, 20), ema(close, 50))"] }],
      },
      orders: [{ id: "entry", type: "market", size_fraction: 0.95 }],
    },
  },
};
