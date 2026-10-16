import type { ReactNode } from "react";

export function Stat({
  label,
  children,
  tone,
}: {
  label: string;
  children: ReactNode;
  tone?: "pos" | "neg" | "flat";
}) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd className={tone}>{children}</dd>
    </div>
  );
}
