import { Component, type ErrorInfo, type ReactNode } from "react";
import { checkBuild } from "../lib/build";

const RECHECK_MS = 15_000;

interface Props {
  /** what failed, shown in the message: "The chart", "This page" */
  label: string;
  /** when this value changes the boundary clears itself (e.g. the route) */
  resetKey?: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/** Keeps one faulty view from blanking the app: a short message and a retry button. */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: unknown): State {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  private timer: ReturnType<typeof setInterval> | undefined;

  override componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`${this.props.label} failed:`, error, info.componentStack);
  }

  /** A crash often means stale code met a newer API; the failed view has no polling, so look for a newer build. */
  private watch() {
    if (this.state.error && !this.timer) {
      void checkBuild();
      this.timer = setInterval(() => void checkBuild(), RECHECK_MS);
    } else if (!this.state.error && this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
  }

  override componentDidMount() {
    this.watch(); // a boundary whose first render already failed is mounted, not updated
  }

  override componentWillUnmount() {
    clearInterval(this.timer);
  }

  override componentDidUpdate(prev: Props) {
    this.watch();
    if (this.state.error && prev.resetKey !== this.props.resetKey) this.setState({ error: null });
  }

  override render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div role="alert" className="error">
        <p>
          {this.props.label} failed to display: {error.message}
        </p>
        <button type="button" onClick={() => this.setState({ error: null })}>
          Retry
        </button>
      </div>
    );
  }
}
