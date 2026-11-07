import { AnimatePresence, motion } from "motion/react";
import { HsEvent } from "../api";

type Line = { icon: string; tone: string; title: string; detail?: string } | null;

function describe(e: HsEvent): Line {
  const d = e.data;
  switch (e.kind) {
    case "task_started":
      return { icon: "▶", tone: "hot", title: `Task started: ${d.title}`, detail: `mode ${d.mode}${d.fault ? `, builder will fail at ${d.fault.replace(/_/g, " ")}` : ""}. Requirements ${d.req_version}.` };
    case "tests_written":
      return { icon: "T", tone: "ok", title: `Verifier wrote ${d.count} acceptance test file before any code`, detail: `tests commit ${d.sha}` };
    case "checkpoint":
      return { icon: "●", tone: d.agent === "standby" ? "cool" : "hot", title: `Checkpoint pushed by ${d.agent}`, detail: `step ${d.step ?? "-"} at ${d.sha}, lease ${d.lease}` };
    case "pr_opened":
      return { icon: "PR", tone: "hot", title: `Pull request #${d.number} opened by ${d.agent}`, detail: d.head };
    case "pr_reused":
      return { icon: "PR", tone: "ok", title: `Existing PR #${d.number} reused`, detail: "GitHub allows one open PR per branch, so no duplicate was created" };
    case "watchdog_alert":
      return { icon: "!", tone: "warn", title: `Watchdog: ${d.agent} ${d.reason}` };
    case "failover_started":
      return { icon: "⇄", tone: "bad", title: `Failover: lease ${d.old} revoked, recovering as lease ${d.new}`, detail: `${d.failed} ${d.reason}` };
    case "drained":
      return { icon: "⏸", tone: "warn", title: "Writes suspended and drained", detail: d.unknown?.length ? `still in flight, marked unknown: ${d.unknown.join(", ")}` : "nothing was left in flight" };
    case "agent_stopped":
      return { icon: "✕", tone: "bad", title: `${d.name} process ended`, detail: "its Switch connection drops, which frees the builder role" };
    case "role_released":
      return { icon: "↺", tone: "warn", title: "Switch released the builder role", detail: `${d.agent} no longer holds it` };
    case "claim_confirmed":
      return { icon: "✓", tone: "cool", title: `${d.agent} holds the builder role`, detail: `confirmed with Switch, lease ${d.lease}` };
    case "fenced":
      if (!d.sha) return { icon: "▮", tone: "cool", title: "Nothing to fence", detail: "the old builder never pushed, so the standby starts from the plan" };
      return { icon: "▮", tone: "cool", title: `Fence commit ${d.sha} pushed`, detail: `branch moved from ${d.previous}; any late write from the old builder now fails` };
    case "reconciled":
      return { icon: "≡", tone: "cool", title: "Reconciled what the old builder was doing", detail: (d.items || []).map((i: any) => `${i.op}: ${i.status.replace("_", " ")}`).join(", ") || "nothing uncertain" };
    case "step_adopted":
      return { icon: "↳", tone: "ok", title: `Step ${d.step} found on the branch, not repeated`, detail: d.sha };
    case "step_redone":
      return { icon: "↻", tone: "warn", title: `Step ${d.step} redone once`, detail: "its push never landed" };
    case "tests_ran":
      if (d.run !== "inherited") return null;
      return { icon: "T", tone: d.regressions?.length ? "bad" : "ok", title: `Inherited work checked: ${d.regressions?.length ?? 0} regressions`, detail: `${d.passed} passing, ${d.pending?.length ?? 0} pending because their steps are not built yet` };
    case "stale_write_rejected":
      return { icon: "⛔", tone: "bad", title: `Stale write from ${d.agent} rejected`, detail: `lease ${d.lease} vs current ${d.current}, stopped at the ${d.layer}` };
    case "stale_write_accepted":
      return { icon: "⚠", tone: "bad", title: `Stale write from ${d.agent} accepted`, detail: "without Hot Swap, the old builder overwrote the branch" };
    case "push_delayed":
      return { icon: "…", tone: "warn", title: `A push from ${d.agent} is stuck in flight`, detail: `lease ${d.lease}` };
    case "build_complete":
      return { icon: "■", tone: "ok", title: `All steps built, head ${d.head}` };
    case "scrub_applied":
      return { icon: "⌫", tone: "ok", title: d.applied?.length ? `Scrubber removed ${d.applied.length} pieces of debris` : "Scrubber found no debris", detail: (d.applied || []).map((p: any) => p.reason).join(", ") };
    case "attested":
      return { icon: "✓", tone: "ok", title: `Verified ${d.code}: ${d.passed} acceptance tests, mutation score ${d.mutation?.score ?? "-"}%`, detail: `by ${d.attested_by}, tests ${d.tests}. Waiting for the owner to accept.` };
    case "verification_failed":
      return { icon: "✕", tone: "bad", title: "Verification failed", detail: (d.failed || []).join(", ") };
    case "acceptance_refused":
      return { icon: "⛔", tone: "bad", title: "Acceptance refused", detail: d.reason };
    case "acceptance_blocked":
      return { icon: "⛔", tone: "warn", title: "Final acceptance blocked", detail: d.reason };
    case "accepted":
      return { icon: "★", tone: "ok", title: `Owner accepted ${d.code}`, detail: "the recovery receipt is posted in the room" };
    case "escalated":
      return { icon: "?", tone: "warn", title: "Needs a person", detail: d.reason };
    case "human_message":
      return { icon: "@", tone: "cool", title: `${d.who} in chat`, detail: d.body };
    case "manual_kill":
      return { icon: "✕", tone: "bad", title: `${d.agent} killed from the console` };
    case "agent_revived":
      return { icon: "↑", tone: d.zombie ? "warn" : "ok", title: d.zombie ? `${d.agent} came back with its old lease` : `${d.agent} restarted` };
    case "agent":
      if (d.error) return { icon: "!", tone: "warn", title: `${d.name}: ${d.error}`, detail: d.note };
      return null;
    default:
      return null;
  }
}

export function Timeline({ events }: { events: HsEvent[] }) {
  const items = events
    .map((e) => ({ e, line: describe(e) }))
    .filter((x) => x.line)
    .reverse();
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Room activity</h3>
        <span>{items.length} events</span>
      </div>
      <div className="timeline">
        {items.length === 0 && (
          <div className="empty">
            <b>Nothing has happened yet</b>
            Start the task from the dock below, or ask in the Switch room: <code>@hs-gateway start</code>
          </div>
        )}
        <AnimatePresence initial={false}>
          {items.map(({ e, line }) => (
            <motion.div
              key={e.seq}
              className={`tl-item tone-${line!.tone}`}
              initial={{ opacity: 0, x: -14, height: 0 }}
              animate={{ opacity: 1, x: 0, height: "auto" }}
              transition={{ duration: 0.32, ease: [0.2, 0.8, 0.2, 1] }}
            >
              <div className="tl-time">{new Date(e.at * 1000).toLocaleTimeString([], { hour12: false })}</div>
              <div className="tl-icon">{line!.icon}</div>
              <div>
                <div className="tl-title">{line!.title}</div>
                {line!.detail && <div className="tl-detail">{line!.detail}</div>}
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  );
}
