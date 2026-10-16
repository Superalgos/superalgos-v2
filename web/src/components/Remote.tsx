import type { ReactNode } from "react";
import type { Remote } from "../useApi";

export function Show<T>({
  remote,
  children,
}: {
  remote: Remote<T>;
  children: (data: T) => ReactNode;
}) {
  if (remote.state === "loading")
    return (
      <p role="status" className="muted">
        Loading…
      </p>
    );
  if (remote.state === "error")
    return (
      <p role="alert" className="error">
        {remote.message}
      </p>
    );
  return <>{children(remote.data)}</>;
}
