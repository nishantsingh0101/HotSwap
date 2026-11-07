import { motion } from "motion/react";
import { Agent, Lease, State, ago, post } from "../api";

export const ROLE_COLOR: Record<string, string> = {
  builder: "var(--hot)",
  standby: "var(--cool)",
  verifier: "var(--ok)",
  scrubber: "var(--sand)",
};

const ROLE_JOB: Record<string, string> = {
  builder: "builds, checkpoints every step",
  standby: "takes over safely",
  verifier: "tests first, never writes code",
  scrubber: "proposes debris removal",
};

function ledClass(agent: Agent) {
  if (!agent.alive) return "bad";
  if (["refused", "error", "zombie", "fenced"].includes(agent.state)) return "warn";
  if (["working", "claiming"].includes(agent.state)) return "busy";
  return "ok";
}

function Pulse({ agent, now }: { agent: Agent; now: number }) {
  const width = 120;
  const alive = agent.alive;
  const stale = agent.last_progress ? now - agent.last_progress : 999;
  const amp = !alive ? 0 : stale > 20 ? 2 : agent.state === "working" ? 8 : 4;
  const points = Array.from({ length: 24 }, (_, i) => {
    const x = (i / 23) * width;
    const spike = i % 6 === 3 ? -amp : i % 6 === 4 ? amp * 0.6 : 0;
    return `${x.toFixed(1)},${(11 + spike).toFixed(1)}`;
  }).join(" ");
  return (
    <svg className="pulse" viewBox={`0 0 ${width} 22`} preserveAspectRatio="none" aria-hidden>
      <polyline points={points} fill="none" stroke={alive ? "currentColor" : "var(--bad)"} strokeWidth="1.4" opacity={alive ? 0.9 : 0.5}>
        {alive && <animate attributeName="stroke-dashoffset" from="240" to="0" dur="2.4s" repeatCount="indefinite" />}
      </polyline>
    </svg>
  );
}

export function Bay({ agent, lease, now, onToast }: { agent: Agent; lease: Lease; now: number; onToast: (m: string) => void }) {
  const holder = lease.holder === agent.name;
  const color = ROLE_COLOR[agent.role] ?? "var(--ink-3)";
  const kill = async () => {
    await post(`/api/demo/kill/${agent.name}`);
    onToast(`${agent.name} stopped. Watch the watchdog react.`);
  };
  const revive = async (zombie: boolean) => {
    await post(`/api/demo/revive/${agent.name}`, { zombie });
    onToast(zombie ? "The old builder is back and will try to push with its stale lease." : `${agent.name} restarted.`);
  };
  return (
    <motion.div
      layout
      className={`bay ${holder ? "holder" : ""} ${agent.alive ? "" : "dead"}`}
      style={{ ["--accent" as any]: color, color }}
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 260, damping: 26 }}
    >
      <div className="bay-head">
        <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
          <span className={`led ${ledClass(agent)}`} />
          <span className="bay-label" style={{ color: "var(--ink)" }}>{agent.name}</span>
        </div>
        <div className="screws"><i /><i /></div>
      </div>
      <div className="bay-provider">
        <span className="chip">{agent.provider}</span>
        <span className="chip">{agent.vendor}</span>
        {holder && <span className="chip" style={{ color, borderColor: color }}>lease {lease.number}</span>}
      </div>
      <div className="bay-state">{agent.alive ? agent.state : "offline"}{agent.step ? ` / step ${agent.step}` : ""}</div>
      <div className="bay-note">{!agent.note || /ready$/.test(agent.note) ? ROLE_JOB[agent.role] : agent.note}</div>
      <div className="bay-foot">
        <Pulse agent={agent} now={now} />
        {agent.alive ? (
          <button className="bay-btn" onClick={kill} title="Stop this agent's process">kill</button>
        ) : agent.role === "builder" ? (
          <button className="bay-btn revive" onClick={() => revive(true)} title="Bring the old builder back with its stale lease">return</button>
        ) : (
          <button className="bay-btn revive" onClick={() => revive(false)}>start</button>
        )}
      </div>
      <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--ink-3)", marginTop: 6 }}>
        heartbeat {ago(agent.last_heartbeat, now)} / progress {ago(agent.last_progress, now)}
      </div>
    </motion.div>
  );
}

export function Rack({ state, onToast }: { state: State; onToast: (m: string) => void }) {
  const { agents, lease, run, now } = state;
  const failover = run.failover;
  return (
    <div className="rack">
      {agents.map((agent) => (
        <Bay key={agent.name} agent={agent} lease={lease} now={now} onToast={onToast} />
      ))}
      <div className="watch">
        <h4>Watchdog</h4>
        <div className="watch-row"><span>dead after</span><b>no heartbeat</b></div>
        <div className="watch-row"><span>stalled after</span><b>no progress</b></div>
        <div className="watch-row"><span>looping after</span><b>3 same errors</b></div>
        <div style={{ borderTop: "1px solid var(--line)", paddingTop: 10, fontSize: 12.5, color: "var(--ink-2)" }}>
          {failover ? (
            <>
              <span style={{ color: "var(--warn)", fontWeight: 650 }}>Failover:</span> {failover.failed} {failover.reason}
            </>
          ) : (
            "Watching the lease holder. No failures so far."
          )}
        </div>
      </div>
    </div>
  );
}
