import { api } from "../api/client";
import { EquityChart } from "../components/EquityChart";
import { Show } from "../components/Remote";
import { Stat } from "../components/Stat";
import { fmtFraction, fmtPct, signClass } from "../lib/format";
import { chartHref, marketSymbol } from "../lib/market";
import { backtestHref } from "../lib/route";
import { useApi } from "../useApi";

function Detail({ id }: { id: string }) {
  const remote = useApi(() => api.backtest(id), id);
  return (
    <Show remote={remote}>
      {(b) => (
        <section aria-labelledby="bt-title">
          <h2 id="bt-title">{b.strategy_name}</h2>
          <p className="muted small">
            {b.instrument} · {b.start} to {b.end}
          </p>
          <p className="small">
            <a
              href={chartHref({
                instrument: marketSymbol(b.instrument),
                tf: b.timeframe,
                bt: b.id,
              })}
            >
              Show fills on the chart ({b.fills.length} fills, {b.timeframe})
            </a>
          </p>
          <dl className="stats">
            <Stat label="PnL" tone={signClass(b.pnl_pct)}>
              {fmtPct(b.pnl_pct, 1, true)}
            </Stat>
            <Stat label="Win rate">{fmtFraction(b.win_rate)}</Stat>
            <Stat label="Max drawdown">{fmtPct(b.max_dd_pct)}</Stat>
            <Stat label="Trades">{b.trades}</Stat>
          </dl>
          <EquityChart points={b.equity} title={`${b.strategy_name} backtest equity`} />
        </section>
      )}
    </Show>
  );
}

export function BacktestsPage({ selected }: { selected?: string }) {
  const remote = useApi(api.backtests);
  return (
    <>
      <h1>Backtests</h1>
      <Show remote={remote}>
        {(rows) => {
          const id = selected ?? rows[0]?.id;
          return (
            <div className="split">
              <ul className="list" aria-label="Backtests">
                {rows.map((b) => (
                  <li key={b.id}>
                    <a href={backtestHref(b.id)} aria-current={b.id === id ? "true" : undefined}>
                      <b>{b.strategy_name}</b>
                      <span className="muted small">
                        {b.instrument} · {b.start} to {b.end}
                      </span>
                      <span className={`num ${signClass(b.pnl_pct)}`}>
                        {fmtPct(b.pnl_pct, 1, true)}
                      </span>
                    </a>
                  </li>
                ))}
              </ul>
              <div>
                {id ? (
                  <Detail id={id} />
                ) : (
                  <p className="muted">
                    No backtests yet. Run <code>sa backtest</code> to create one.
                  </p>
                )}
              </div>
            </div>
          );
        }}
      </Show>
    </>
  );
}
