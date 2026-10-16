import { contractDump } from "./contract";

// bun run mock/dump.ts: rewrite mock/contract.json, which apps/sa-api's contract test reads.
await Bun.write(
  new URL("./contract.json", import.meta.url),
  `${JSON.stringify(contractDump(), null, 2)}\n`,
);
console.log("wrote mock/contract.json");
