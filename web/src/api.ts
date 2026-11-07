import { useEffect, useRef, useState } from "react";

export type Lease = { number: number; holder: string | null; phase: string; updated_at: number };

export type Agent = {
  name: string;
  role: string;
  provider: string;
  vendor: string;
  state: string;
  step: number | null;
  note: string | null;
  alive: boolean;
  last_heartbeat: number | null;
  last_progress: number | null;
  pid: number | null;
};

export type PlanStep = { step: number; title: string; files: string[] };

export type WorklogEntry = {
  seq: number;
  agent: string;
  lease: number;
  step: number | null;
  status: string;
  commit_sha: string | null;
  note: string | null;
  at: number;
};

export type HsEvent = { seq: number; at: number; kind: string; data: Record<string, any> };

export type MatrixRow = {
  label: string;
  fault: string;
  mode: string;
  status: string;
  recovered: boolean;
  duplicate_prs: number;
  prs_opened: number;
  pr_reused: boolean;
  stale_writes_rejected: number;
  stale_writes_accepted: number;
  steps_redone: number;
  steps_adopted: number;
  regressions: number;
  tests_passed: number | null;
  mutation: number | null;
  seconds: number;
  failover_reason: string | null;
};

export type State = {
  room_name?: string;
  task: { id: string; title: string; request: string; acceptance: string[]; plan: PlanStep[] };
  lease: Lease;
  run: Record<string, any>;
  agents: Agent[];
  worklog: WorklogEntry[];
  prs: { number: number; head: string; title: string; state: string; url: string }[];
  connected: boolean;
  remote_kind: string;
  matrix: { crash_points: MatrixRow[]; comparison: MatrixRow[] } | null;
  now: number;
};

export async function post(path: string, body: unknown = {}) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", ...operatorHeaders() }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.reason || `request failed (${res.status})`);
  return data;
}

export function operatorHeaders(): Record<string, string> {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get("operator");
  if (token) {
    sessionStorage.setItem("hotswap-operator", token);
    history.replaceState(null, "", location.pathname + location.search);
  }
  const saved = sessionStorage.getItem("hotswap-operator");
  return saved ? { "X-Hotswap-Operator-Token": saved } : {};
}

operatorHeaders();

export function useConsole() {
  const [state, setState] = useState<State | null>(null);
  const [events, setEvents] = useState<HsEvent[]>([]);
  const [online, setOnline] = useState(false);
  const lastSeq = useRef(0);

  useEffect(() => {
    let stop = false;
    const refresh = async () => {
      try {
        const res = await fetch("/api/state");
        if (!res.ok) throw new Error();
        const data = await res.json();
        if (!stop) {
          setState(data);
          setOnline(true);
        }
      } catch {
        if (!stop) setOnline(false);
      }
    };
    refresh();
    const timer = setInterval(refresh, 1500);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    let source: EventSource | null = null;
    let retry: number | undefined;
    const connect = () => {
      source = new EventSource(`/api/stream?after=${lastSeq.current}`);
      source.addEventListener("hs", (message) => {
        const event: HsEvent = JSON.parse((message as MessageEvent).data);
        if (event.seq <= lastSeq.current) return;
        lastSeq.current = event.seq;
        if (event.kind === "reset" || event.kind === "task_started") {
          setEvents([event]);
        } else {
          setEvents((list) => [...list.slice(-399), event]);
        }
      });
      source.onerror = () => {
        source?.close();
        retry = window.setTimeout(connect, 2000);
      };
    };
    connect();
    return () => {
      source?.close();
      window.clearTimeout(retry);
    };
  }, []);

  return { state, events, online };
}

export const short = (sha?: string | null) => (sha ? sha.slice(0, 7) : "");

export function ago(at: number | null | undefined, now: number) {
  if (!at) return "never";
  const s = Math.max(0, now - at);
  if (s < 1.5) return "now";
  if (s < 60) return `${Math.round(s)}s ago`;
  return `${Math.round(s / 60)}m ago`;
}
