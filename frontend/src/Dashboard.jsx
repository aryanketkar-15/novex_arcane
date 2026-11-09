import { useState, useEffect, useRef } from "react";
import "./Dashboard.css";

const API_URL = `${
  import.meta.env.VITE_API_URL || "http://127.0.0.1:8000"
}/api/status`;

const STATE_META = {
  IDLE:       { label: "IDLE",       cls: "badge--idle"      },
  ANALYZING:  { label: "ANALYZING",  cls: "badge--running"   },
  BISECTING:  { label: "BISECTING",  cls: "badge--running"   },
  PATCHING:   { label: "PATCHING",   cls: "badge--running"   },
  VALIDATING: { label: "VALIDATING", cls: "badge--validating"},
  DONE:       { label: "DONE",       cls: "badge--done"      },
  ESCALATED:  { label: "ESCALATED",  cls: "badge--escalated" },
};

const AGENT_LABELS = {
  analyst:        "Analyst",
  bisect:         "Bisect",
  patch_generator:"Patch Generator",
  validator:      "Validator",
  pr_agent:       "PR Agent",
};

function StatusDot({ status }) {
  return (
    <span
      className={`dot dot--${status}`}
      title={status}
      aria-label={`Agent status: ${status}`}
    />
  );
}

function AgentRow({ id, status }) {
  return (
    <div className="agent-row">
      <StatusDot status={status} />
      <span className="agent-name">{AGENT_LABELS[id] ?? id}</span>
      <span className={`agent-status-label agent-status-label--${status}`}>
        {status.toUpperCase()}
      </span>
    </div>
  );
}

export default function Dashboard() {
  const [data, setData]       = useState(null);
  const [error, setError]     = useState(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick]       = useState(0);
  const intervalRef           = useRef(null);

  const fetchStatus = async () => {
    try {
      const res = await fetch(API_URL);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const json = await res.json();
      setData(json);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
      setTick(t => t + 1);
    }
  };

  useEffect(() => {
    fetchStatus();
    intervalRef.current = setInterval(fetchStatus, 2000);
    return () => clearInterval(intervalRef.current);
  }, []);

  const stateMeta = data
    ? (STATE_META[data.current_state] ?? { label: data.current_state, cls: "badge--idle" })
    : null;

  return (
    <div className="dashboard">
      {/* Header */}
      <header className="dashboard__header">
        <div className="header__logo">
          <span className="logo-arc">ARC</span><span className="logo-ane">ANE</span>
        </div>
        <p className="header__sub">Autonomous Repair &amp; Conflict-Aware Node Engine</p>
        <div className="header__pulse" aria-hidden="true" />
      </header>

      <main className="dashboard__body">

        {/* Loading splash */}
        {loading && (
          <div className="splash">
            <div className="spinner" />
            <p>Connecting to pipeline…</p>
          </div>
        )}

        {/* Fetch error */}
        {!loading && error && (
          <div className="alert alert--error">
            <span>⚠</span> Could not reach FastAPI: <strong>{error}</strong>
            <br /><small>Make sure <code>uvicorn api.main:app --reload</code> is running.</small>
          </div>
        )}

        {/* Main content */}
        {!loading && data && (
          <>
            {/* Pipeline state badge */}
            <section className="card card--state">
              <h2 className="card__title">Pipeline State</h2>
              <div className={`badge ${stateMeta.cls}`}>
                {stateMeta.label}
              </div>
              {data.error && (
                <p className="error-msg">⚠ {data.error}</p>
              )}
            </section>

            {/* Last event */}
            <section className="card card--event">
              <h2 className="card__title">Last Event</h2>
              {data.last_event ? (
                <div className="event-grid">
                  <span className="event-label">Repository</span>
                  <span className="event-value">{data.last_event.repo ?? "—"}</span>
                  <span className="event-label">Commit SHA</span>
                  <span className="event-value mono">{data.last_event.sha?.slice(0, 12) ?? "—"}</span>
                  <span className="event-label">Triggered</span>
                  <span className="event-value">{data.last_event.triggered_at ?? "—"}</span>
                </div>
              ) : (
                <p className="muted">Waiting for first webhook event…</p>
              )}
            </section>

            {/* Agents */}
            <section className="card card--agents">
              <h2 className="card__title">Agent Status</h2>
              <div className="agents-list">
                {Object.entries(data.agents ?? {}).map(([id, status]) => (
                  <AgentRow key={id} id={id} status={status} />
                ))}
              </div>
            </section>

            {/* PR URL */}
            <section className="card card--pr">
              <h2 className="card__title">Generated Pull Request</h2>
              {data.pr_url ? (
                <a
                  className="pr-link"
                  href={data.pr_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  🔗 {data.pr_url}
                </a>
              ) : (
                <p className="muted">No PR generated yet.</p>
              )}
            </section>

            {/* Footer tick */}
            <p className="refresh-note">Auto-refreshing every 2s · Tick #{tick}</p>
          </>
        )}
      </main>
    </div>
  );
}
