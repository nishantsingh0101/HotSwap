import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState } from "react";
import { post, short, useConsole, operatorHeaders } from "./api";
import { Rack } from "./components/Bays";
import { Proof } from "./components/Proof";
import { Barrier, Plan, Receipt, Verification } from "./components/Recovery";
import { AcceptCallout, GettingStarted, RequestText, StoryRail } from "./components/Story";
import { Timeline } from "./components/Timeline";

const FAULTS = [
  ["after_pr_before_report", "Crash after opening the PR"],
  ["crash_after_commit", "Crash after commit, before push"],
  ["delayed_push", "Crash with a push in flight"],
  ["stall", "Stall while connected"],
  ["loop", "Loop on one error"],
  ["crash_during_tests", "Crash during tests"],
  ["crash_before_first_commit", "Crash before first commit"],
  ["", "No failure"],
];

const STATUS_LABEL: Record<string, string> = {
  writing_tests: "writing tests",
  building: "building",
  recovering: "recovering",
  scrubbing: "scrubbing",
  verifying: "verifying",
  awaiting_acceptance: "awaiting owner",
  accepted: "accepted",
  escalated: "needs a person",
  blocked: "blocked",
  rejected: "rejected",
};

export default function App() {
  const { state, events, online } = useConsole();
  const [tab, setTab] = useState<"live" | "proof">("live");
  const [fault, setFault] = useState(FAULTS[0][0]);
  const [toast, setToast] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [demo, setDemo] = useState<{ command: string; output: string } | null>(null);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3800);
    return () => clearTimeout(t);
  }, [toast]);

  const act = async (fn: () => Promise<unknown>, message?: string) => {
    setBusy(true);
    try {
      await fn();
      if (message) setToast(message);
    } catch (error) {
      setToast((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!state) {
    return (
      <div className="shell">
        <div className="empty" style={{ marginTop: 120 }}>
          <b>{online ? "Loading" : "Waiting for the gateway"}</b>
          Start it with <code>hotswap run</code>
        </div>
      </div>
    );
  }

  const status = state.run.status as string | undefined;
  const head = state.run.head as string | undefined;
  const holder = state.lease.holder;

  return (
    <div className="shell">
      <header className="top">
        <div className="brand">
          <div className="brand-mark">
            <div>
              <span style={{ background: "var(--hot)" }} />
              <span style={{ background: "var(--cool)" }} />
            </div>
          </div>
          <div>
            <div className="wordmark">HOT<em>/</em>SWAP</div>
            <div className="tagline">the work survives when the agent does not</div>
          </div>
        </div>
        <div className="tabs" role="tablist">
          <button className={`tab ${tab === "live" ? "active" : ""}`} onClick={() => setTab("live")}>Live room</button>
          <button className={`tab ${tab === "proof" ? "active" : ""}`} onClick={() => setTab("proof")}>Proof</button>
        </div>
        <div className="top-meta">
          <span className="pill"><span className={`dot ${state.connected ? "on" : "off"}`} />Switch room <b>{state.connected ? "connected" : "offline"}</b></span>
          <span className="pill">repo <b>{state.remote_kind}</b></span>
          <span className="pill"><span className={`dot ${online ? "on" : "off"}`} />gateway</span>
        </div>
      </header>

      <section className="hero">
        <div>
          <div className="eyebrow">Task / {state.task.id}</div>
          <h1>{state.task.title}</h1>
          <RequestText text={state.task.request} />
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 10, alignItems: "flex-end" }}>
          <div className="lease">
            <div className="lease-num">
              <small>Lease</small>
              <AnimatePresence mode="popLayout">
                <motion.div
                  key={state.lease.number}
                  className="digits"
                  initial={{ y: -24, opacity: 0 }}
                  animate={{ y: 0, opacity: 1 }}
                  exit={{ y: 24, opacity: 0 }}
                  transition={{ type: "spring", stiffness: 300, damping: 22 }}
                >
                  {String(state.lease.number).padStart(2, "0")}
                </motion.div>
              </AnimatePresence>
            </div>
            <div className="lease-holder">
              <small>Held by</small>
              <b style={{ color: holder === "standby" ? "var(--cool)" : holder ? "var(--hot)" : "var(--ink-3)" }}>{holder || "nobody"}</b>
              <span className="mono" style={{ fontSize: 11, color: "var(--ink-3)" }}>{state.lease.phase}</span>
            </div>
          </div>
          <span className="status-chip">
            <span className={`dot ${status === "accepted" ? "on" : ""}`} style={status && status !== "accepted" ? { background: "var(--hot)" } : undefined} />
            {status ? STATUS_LABEL[status] ?? status : "idle"}
          </span>
        </div>
      </section>

      <AnimatePresence mode="wait">
        {tab === "live" ? (
          <motion.div key="live" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} transition={{ duration: 0.22 }}>
            {status ? <StoryRail state={state} events={events} /> : <GettingStarted />}
            {status === "awaiting_acceptance" && <AcceptCallout head={head} room={state.room_name || "hotswap-demo"} />}
            <div className="section-title"><span>Agents in the room</span><small>each bay is one agent process connected to Switch</small></div>
            <Rack state={state} onToast={setToast} />
            <div className="section-title"><span>What is happening</span><small>live from the gateway and the Switch room</small></div>
            <div className="grid">
              <div className="col">
                <Timeline events={events} />
                {state.run.receipt && <Receipt receipt={state.run.receipt} />}
              </div>
              <div className="col">
                <Barrier events={events} />
                <Plan state={state} />
                <Verification state={state} events={events} />
                <div className="panel">
                  <div className="panel-head">
                    <h3>Does it work</h3>
                    <button className="btn" disabled={!head || busy} onClick={() => act(async () => { const r = await fetch("/api/run-demo", { headers: operatorHeaders() }); const data = await r.json(); if (!r.ok) throw new Error(data.reason || "Demo execution failed"); setDemo(data); })}>
                      Run the feature
                    </button>
                  </div>
                  {demo ? (
                    <div className="term">
                      <span className="prompt">$ </span>{demo.command}{"\n"}{demo.output}
                    </div>
                  ) : (
                    <div className="panel-body" style={{ color: "var(--ink-2)", fontSize: 12.5 }}>
                      Runs <code>python -m ledger export</code> on the current branch head{head ? ` (${short(head)})` : ""}.
                    </div>
                  )}
                </div>
              </div>
            </div>
          </motion.div>
        ) : (
          <motion.div key="proof" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} transition={{ duration: 0.22 }}>
            <Proof state={state} />
          </motion.div>
        )}
      </AnimatePresence>

      <div className="dock">
        <span className="dock-label">Builder fails</span>
        <select value={fault} onChange={(e) => setFault(e.target.value)} aria-label="Failure to inject">
          {FAULTS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <button className="btn primary" disabled={busy} onClick={() => act(() => post("/api/demo/start", { fault, mode: "hotswap" }), "Task started. The verifier writes tests first.")}>
          Start task
        </button>
        <div className="dock-sep" />
        <button className="btn danger" disabled={busy || !holder} onClick={() => act(() => post(`/api/demo/kill/${holder}`), `${holder} killed.`)}>
          Kill lease holder
        </button>
        <button className="btn" disabled={busy} onClick={() => act(() => post("/api/demo/reset"), "Reset: fresh repository, fresh agents.")}>
          Reset
        </button>
      </div>

      <AnimatePresence>
        {toast && (
          <motion.div className="toast" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 10 }}>
            {toast}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
