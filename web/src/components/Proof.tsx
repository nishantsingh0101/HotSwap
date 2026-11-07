import { motion } from "motion/react";
import { MatrixRow, State } from "../api";

function Verdict({ row }: { row: MatrixRow }) {
  if (row.recovered && row.stale_writes_accepted === 0 && row.duplicate_prs === 0) return <span className="verdict ok">recovered</span>;
  if (row.recovered) return <span className="verdict warn">finished with damage</span>;
  return <span className="verdict bad">{row.status || "did not finish"}</span>;
}

function Bars({ rows, field, title, max }: { rows: MatrixRow[]; field: keyof MatrixRow; title: string; max: number }) {
  return (
    <div className="bar-card">
      <h5>{title}</h5>
      {rows.map((row) => {
        const value = Number(row[field] ?? 0);
        const color = row.mode === "hotswap" ? "var(--ok)" : row.mode === "plain" ? "var(--warn)" : "var(--bad)";
        return (
          <div className="bar-line" key={row.mode}>
            <span>{row.label}</span>
            <div className="bar-track">
              <motion.div
                className="bar-fill"
                style={{ background: color }}
                initial={{ width: 0 }}
                animate={{ width: `${Math.max(value === 0 ? 2 : 6, (value / Math.max(max, 1)) * 100)}%` }}
                transition={{ duration: 0.9, ease: [0.2, 0.8, 0.2, 1] }}
              />
            </div>
            <b className="mono">{value}</b>
          </div>
        );
      })}
    </div>
  );
}

export function Proof({ state }: { state: State }) {
  const m = state.matrix;
  if (!m || (!m.crash_points.length && !m.comparison.length)) {
    return (
      <div className="proof">
        <div className="panel">
          <div className="empty">
            <b>No crash matrix results yet</b>
            Run <code>hotswap matrix</code> while the gateway is running. It kills the builder at every risky point and saves what really happened.
          </div>
        </div>
      </div>
    );
  }
  const points = m.crash_points;
  const recovered = points.filter((p) => p.recovered).length;
  const dupes = points.reduce((n, p) => n + p.duplicate_prs, 0);
  const staleAccepted = points.reduce((n, p) => n + p.stale_writes_accepted, 0);
  const staleRejected = points.reduce((n, p) => n + p.stale_writes_rejected, 0);
  const cmp = m.comparison;
  return (
    <div className="proof">
      <div className="proof-intro">
        {[
          ["Crash points recovered", `${recovered}/${points.length}`, "every run is a real kill of a real process"],
          ["Duplicate pull requests", String(dupes), "across all crash points"],
          ["Stale writes accepted", String(staleAccepted), "after a takeover"],
          ["Stale writes rejected", String(staleRejected), "by the gateway or the git server"],
        ].map(([label, value, hint], i) => (
          <motion.div className="big-stat" key={label} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.07 }}>
            <small>{label}</small>
            <b>{value}</b>
            <span>{hint}</span>
          </motion.div>
        ))}
      </div>

      <div className="panel">
        <div className="panel-head">
          <h3>Crash matrix</h3>
          <span>{points.length} scenarios</span>
        </div>
        <div style={{ overflowX: "auto" }}>
          <table className="matrix">
            <thead>
              <tr>
                <th>Where the builder failed</th>
                <th>Outcome</th>
                <th>Detected as</th>
                <th>PRs</th>
                <th>Stale writes</th>
                <th>Steps</th>
                <th>Tests</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody>
              {points.map((row, i) => (
                <tr key={i}>
                  <td>{row.label}</td>
                  <td><Verdict row={row} /></td>
                  <td style={{ color: "var(--ink-2)", fontSize: 12.5 }}>{row.failover_reason || "-"}</td>
                  <td className="num">{row.prs_opened}{row.pr_reused ? " (reused)" : ""}</td>
                  <td className="num">{row.stale_writes_rejected} rejected / {row.stale_writes_accepted} accepted</td>
                  <td className="num">{row.steps_adopted} kept / {row.steps_redone} redone</td>
                  <td className="num">{row.tests_passed ?? "-"}{row.mutation != null ? `, mut ${row.mutation}%` : ""}</td>
                  <td className="num">{row.seconds}s</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {cmp.length > 0 && (
        <>
          <div className="eyebrow" style={{ marginTop: 6 }}>Same crash, three setups: the builder dies after opening the PR, then comes back. The first two run this app with its protections switched off.</div>
          <div className="bars">
            <Bars rows={cmp} field="duplicate_prs" title="Duplicate pull requests" max={Math.max(...cmp.map((r) => r.duplicate_prs), 1)} />
            <Bars rows={cmp} field="stale_writes_accepted" title="Stale writes accepted" max={Math.max(...cmp.map((r) => r.stale_writes_accepted), 1)} />
            <Bars rows={cmp} field="steps_redone" title="Steps done twice" max={Math.max(...cmp.map((r) => r.steps_redone), 1)} />
          </div>
        </>
      )}
    </div>
  );
}
