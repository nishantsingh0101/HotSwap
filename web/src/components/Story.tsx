import { motion } from "motion/react";
import { useState } from "react";
import { HsEvent, State, short } from "../api";

type Stage = { key: string; label: string; hint: string };

const STAGES: Stage[] = [
  { key: "tests", label: "Tests first", hint: "Verifier writes acceptance tests from the request" },
  { key: "build", label: "Building", hint: "Builder checkpoints every step" },
  { key: "failure", label: "Failure", hint: "Watchdog detects the dead builder" },
  { key: "takeover", label: "Takeover", hint: "Lease, role, fence, reconcile" },
  { key: "finish", label: "Finish", hint: "Standby builds the remaining steps" },
  { key: "scrub", label: "Clean up", hint: "Scrubber removes leftover debris" },
  { key: "verify", label: "Verify", hint: "Tests and mutation on the final commit" },
  { key: "accept", label: "Accept", hint: "Owner accepts in the Switch room" },
];

export function stageState(state: State, events: HsEvent[]) {
  const kinds = new Set(events.map((e) => e.kind));
  const status = state.run.status as string | undefined;
  const failed = kinds.has("failover_started") || !!state.run.failover;
  const done: Record<string, boolean> = {
    tests: kinds.has("tests_written") || !!state.run.test_sha,
    build: failed || kinds.has("build_complete"),
    failure: failed,
    takeover: kinds.has("fenced") || (failed && ["scrubbing", "verifying", "awaiting_acceptance", "accepted"].includes(status ?? "")),
    finish: kinds.has("build_complete") || ["scrubbing", "verifying", "awaiting_acceptance", "accepted"].includes(status ?? ""),
    scrub: kinds.has("scrub_applied") || ["verifying", "awaiting_acceptance", "accepted"].includes(status ?? ""),
    verify: kinds.has("attested") || ["awaiting_acceptance", "accepted"].includes(status ?? ""),
    accept: status === "accepted",
  };
  const skipped: Record<string, boolean> = { failure: !failed && done.finish, takeover: !failed && done.finish };
  const current = status ? STAGES.find((s) => !done[s.key] && !skipped[s.key])?.key : undefined;
  return { done, skipped, current };
}

export function StoryRail({ state, events }: { state: State; events: HsEvent[] }) {
  const { done, skipped, current } = stageState(state, events);
  const bad = ["escalated", "blocked", "rejected"].includes(state.run.status);
  return (
    <div className="story">
      {STAGES.map((stage, i) => {
        const isDone = done[stage.key];
        const isSkip = skipped[stage.key];
        const isNow = current === stage.key;
        const tone = stage.key === "failure" && isDone ? "warn" : isDone ? "ok" : isNow ? (bad ? "bad" : "now") : isSkip ? "skip" : "";
        return (
          <div key={stage.key} className={`story-step ${tone}`}>
            <div className="story-line" aria-hidden>
              <motion.span
                className="story-fill"
                initial={false}
                animate={{ scaleX: isDone || isSkip ? 1 : 0 }}
                transition={{ duration: 0.5, ease: [0.2, 0.8, 0.2, 1] }}
              />
            </div>
            <div className="story-node">
              {isDone ? (stage.key === "failure" ? "!" : "✓") : isSkip ? "–" : i + 1}
              {isNow && !bad && <span className="story-ping" />}
            </div>
            <div className="story-label">{stage.label}</div>
            <div className="story-hint">{isSkip ? "not needed" : stage.hint}</div>
          </div>
        );
      })}
    </div>
  );
}

export function GettingStarted() {
  const steps = [
    ["Pick a failure", "Choose where the builder should die from the menu in the dock below."],
    ["Start the task", "The verifier writes tests first, then the builder starts checkpointing."],
    ["Watch the recovery", "The watchdog notices, the standby takes over, nothing is repeated or overwritten."],
    ["Accept in chat", "The owner types accept with the commit in the Switch room. Only the owner counts."],
  ];
  return (
    <div className="start-card">
      {steps.map(([title, text], i) => (
        <motion.div key={title} className="start-step" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.06 }}>
          <span>{i + 1}</span>
          <div>
            <b>{title}</b>
            <p>{text}</p>
          </div>
        </motion.div>
      ))}
    </div>
  );
}

export function AcceptCallout({ head, room }: { head?: string; room: string }) {
  const [copied, setCopied] = useState(false);
  const command = `accept ${short(head)}`;
  return (
    <motion.div className="callout" initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }}>
      <div className="callout-dot" />
      <div>
        <b>Waiting for the owner.</b> Verified and ready. In the Switch room <code>#{room}</code>, the owner types:
      </div>
      <button
        className="callout-cmd"
        onClick={() => {
          navigator.clipboard?.writeText(command).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          });
        }}
        title="Copy"
      >
        {command}
        <small>{copied ? "copied" : "copy"}</small>
      </button>
    </motion.div>
  );
}

export function RequestText({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="request">
      <p className={open ? "" : "clamp"}>{text.split("`").map((part, i) => (i % 2 ? <code key={i}>{part}</code> : part))}</p>
      <button className="link" onClick={() => setOpen(!open)}>{open ? "Show less" : "Show the full request"}</button>
    </div>
  );
}
