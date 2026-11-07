import { motion } from "motion/react";
import { HsEvent, State, short } from "../api";
import { ROLE_COLOR } from "./Bays";

const GATES = [
  { key: "failover_started", label: "Suspend", hint: "1" },
  { key: "drained", label: "Drain", hint: "2" },
  { key: "role_released", label: "Release", hint: "3" },
  { key: "claim_confirmed", label: "Claim", hint: "4" },
  { key: "fenced", label: "Fence", hint: "5" },
  { key: "reconciled", label: "Reconcile", hint: "6" },
];

export function Barrier({ events }: { events: HsEvent[] }) {
  const start = [...events].reverse().find((e) => e.kind === "failover_started");
  const seen = new Set(events.filter((e) => start && e.seq >= start.seq).map((e) => e.kind));
  const reached = GATES.filter((g) => seen.has(g.key)).length;
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Takeover barrier</h3>
        <span>{start ? `${reached} of ${GATES.length}` : "idle"}</span>
      </div>
      <div className="panel-body">
        <div className="barrier">
          {GATES.map((gate, i) => {
            const done = seen.has(gate.key);
            const lit = !done && i === reached && !!start;
            return (
              <motion.div
                key={gate.key}
                className={`gate ${done ? "done" : ""} ${lit ? "lit" : ""}`}
                animate={done ? { scale: [1, 1.06, 1] } : {}}
                transition={{ duration: 0.4 }}
              >
                <small>{gate.hint}</small>
                <b>{gate.label}</b>
              </motion.div>
            );
          })}
        </div>
        <div className="barrier-note">
          {!start && "When the builder fails: writes stop, in-flight requests drain, Switch frees the role, the standby claims it, a fence commit moves the branch, then uncertain actions are checked."}
          {start && reached < GATES.length && "Recovering. Each step is enforced by code, not by a prompt."}
          {start && reached === GATES.length && "Recovered. The old builder can no longer write anything."}
        </div>
      </div>
    </div>
  );
}

export function Plan({ state }: { state: State }) {
  const done = new Map<number, { agent: string; commit_sha: string | null; note: string | null }>();
  state.worklog.forEach((e) => {
    if (e.status === "step_done" && e.step != null) done.set(e.step, e);
  });
  const active = state.agents.find((a) => a.name === state.lease.holder)?.step;
  const pr = state.run.pr;
  const prs = state.prs;
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Plan and work log</h3>
        <span>{done.size} / {state.task.plan.length} steps</span>
      </div>
      <div className="panel-body">
        <div className="plan">
          {state.task.plan.map((step) => {
            const entry = done.get(step.step);
            const color = entry ? ROLE_COLOR[entry.agent] : undefined;
            return (
              <motion.div
                layout
                key={step.step}
                className={`plan-step ${entry ? "done" : ""} ${!entry && active === step.step ? "active" : ""}`}
                style={{ ["--accent" as any]: color }}
              >
                <div className="plan-n">{step.step}</div>
                <div>
                  <div className="plan-title">{step.title}</div>
                  <div style={{ fontFamily: "var(--mono)", fontSize: 10.5, color: "var(--ink-3)" }}>{step.files.join(", ")}</div>
                </div>
                <div className="plan-meta">
                  {entry ? (
                    <>
                      <b>{entry.agent}</b>
                      <br />
                      {short(entry.commit_sha)}
                    </>
                  ) : active === step.step ? (
                    "in progress"
                  ) : (
                    "pending"
                  )}
                </div>
              </motion.div>
            );
          })}
        </div>
        <div className="chain" style={{ marginTop: 14 }}>
          <div className={`chain-node ${pr ? "ok" : ""}`}>
            <small>Pull request</small>
            <b>{pr ? `#${pr}` : "not opened yet"}</b>
          </div>
          <div className="chain-node">
            <small>PRs in total</small>
            <b>{prs.length}</b>
          </div>
          <div className="chain-node">
            <small>Branch head</small>
            <b className="mono">{short(state.run.head) || "-"}</b>
          </div>
        </div>
      </div>
    </div>
  );
}

export function Verification({ state, events }: { state: State; events: HsEvent[] }) {
  const attested = [...events].reverse().find((e) => e.kind === "attested");
  const scrub = [...events].reverse().find((e) => e.kind === "scrub_applied");
  const inherited = state.run.inherited;
  const accepted = state.run.status === "accepted";
  const mutation = attested?.data.mutation;
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Verification and responsibility</h3>
        <span>tests {short(state.run.test_sha) || "-"}</span>
      </div>
      <div className="panel-body">
        <div className="stats">
          <div className="stat">
            <small>Acceptance</small>
            <b style={{ color: attested ? "var(--ok)" : undefined }}>{attested ? attested.data.passed : "-"}</b>
            <span>tests passing on the final commit</span>
          </div>
          <div className="stat">
            <small>Mutation</small>
            <b style={{ color: mutation ? "var(--ok)" : undefined }}>{mutation ? `${mutation.score}%` : "-"}</b>
            <span>{mutation ? `${mutation.killed} of ${mutation.total} planted bugs caught` : "planted bugs caught"}</span>
          </div>
          <div className="stat">
            <small>Inherited</small>
            <b style={{ color: inherited ? (inherited.regressions?.length ? "var(--bad)" : "var(--ok)") : undefined }}>
              {inherited ? inherited.regressions?.length ?? 0 : "-"}
            </b>
            <span>{inherited ? `regressions, ${inherited.pending?.length ?? 0} pending` : "regressions after takeover"}</span>
          </div>
        </div>
        {scrub && scrub.data.applied?.length > 0 && (
          <ul className="removals">
            {scrub.data.applied.map((p: any, i: number) => (
              <li key={i}>
                <span style={{ color: "var(--sand)" }}>⌫</span>
                <span><code>{p.file}</code> {p.reason}</span>
              </li>
            ))}
          </ul>
        )}
        <div className="chain">
          <div className={`chain-node ${attested ? "ok" : ""}`}>
            <small>Authored by</small>
            <b>{attested ? attested.data.authors.join(" + ") : "-"}</b>
          </div>
          <span className="chain-arrow">→</span>
          <div className={`chain-node ${attested ? "ok" : ""}`}>
            <small>Attested by</small>
            <b>{attested ? attested.data.attested_by : "-"}</b>
          </div>
          <span className="chain-arrow">→</span>
          <div className={`chain-node ${accepted ? "ok" : ""}`}>
            <small>Accepted by</small>
            <b>{accepted ? "owner" : state.run.status === "awaiting_acceptance" ? "waiting" : "-"}</b>
          </div>
        </div>
      </div>
    </div>
  );
}

export function Receipt({ receipt }: { receipt: any }) {
  if (!receipt) return null;
  const f = receipt.failure;
  const rows: [string, string][] = [];
  if (f) {
    rows.push(["Failure", `${f.failed} ${f.reason}`]);
    rows.push(["Lease", `revoked ${receipt.lease.from} → ${receipt.lease.to}`]);
    rows.push(["Code recovered", `checkpoint ${receipt.checkpoint || "-"}; redone: ${receipt.steps_redone?.length ? receipt.steps_redone.join(", ") : "none"}`]);
    rows.push(["Pull request", `#${receipt.pr} ${receipt.pr_reused ? "reused" : "opened"}, ${receipt.prs_opened} in total`]);
    rows.push(["Stale writes", `${receipt.stale_writes_rejected} rejected, ${receipt.stale_writes_accepted} accepted`]);
    rows.push(["Inherited", `${receipt.inherited?.regressions?.length ?? 0} regressions, ${receipt.inherited?.pending?.length ?? 0} pending`]);
    rows.push(["Recovery time", receipt.recovery_seconds != null ? `${receipt.recovery_seconds} s to first new checkpoint` : "-"]);
    rows.push(["Human messages", `${receipt.human_messages} during recovery`]);
  } else {
    rows.push(["Failure", "none"]);
  }
  rows.push(["Final commit", `${receipt.final_commit} (tests ${receipt.test_commit}, ${receipt.req_version})`]);
  rows.push(["Verified by", `${receipt.verified_by}: ${receipt.acceptance_tests} tests, mutation ${receipt.mutation?.score ?? "-"}%`]);
  rows.push(["Scrubbed", receipt.scrub?.length ? receipt.scrub.join(", ") : "nothing"]);
  rows.push(["Authored by", receipt.authors?.join(", ")]);
  rows.push(["Accepted by", receipt.accepted_by]);
  return (
    <div className="receipt-wrap">
      <motion.div
        className="receipt"
        initial={{ rotateX: -80, opacity: 0, y: -20 }}
        animate={{ rotateX: 0, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 120, damping: 16 }}
      >
        <div className="stamp">ACCEPTED</div>
        <h4>Recovery receipt</h4>
        <div className="sub">{receipt.task}</div>
        {rows.map(([k, v], i) => (
          <motion.div
            className="receipt-row"
            key={k}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.25 + i * 0.06 }}
          >
            <span>{k}</span>
            <span>{v}</span>
          </motion.div>
        ))}
      </motion.div>
    </div>
  );
}
