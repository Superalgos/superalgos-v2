import type { Proposal } from "../api/types";

export interface TallyRow {
  option: string;
  weight: number;
  share: number; // 0..1
  leading: boolean;
}

export function tallyRows(p: Pick<Proposal, "options" | "tally">): TallyRow[] {
  const names = [...new Set([...p.options, ...Object.keys(p.tally)])];
  const total = names.reduce((s, o) => s + (p.tally[o] ?? 0), 0);
  const max = Math.max(0, ...names.map((o) => p.tally[o] ?? 0));
  return names.map((option) => {
    const weight = p.tally[option] ?? 0;
    return {
      option,
      weight,
      share: total > 0 ? weight / total : 0,
      leading: max > 0 && weight === max,
    };
  });
}

export function isOpen(p: Pick<Proposal, "deadline">, now: number): boolean {
  return Date.parse(p.deadline) > now;
}
