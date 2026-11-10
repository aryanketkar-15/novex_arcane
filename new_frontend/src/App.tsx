import { useEffect, useMemo, useState } from "react";
import {
  api,
  ApiError,
  clock,
  confidenceLabel,
  duration,
  shortRepo,
  statusLabel,
  timeAgo,
  type AuditRow,
  type GhRepo,
  type MemoryItem,
  type ModelCredential,
  type Run,
  type Screen,
  type Settings,
} from "./api";
import { DataProvider, useData } from "./data";
import {
  Avatar,
  Badge,
  Button,
  Empty,
  Field,
  Icon,
  Mark,
  Modal,
  PageHeader,
  SearchBox,
  Select,
  Toggle,
  type IconName,
} from "./ui";
import { ComponentsPage } from "./static-screens";
import { SignIn } from "./signin";

type ModalKind = "run" | "retry" | "reject" | "assign" | null;

const PAGE_SIZE = 8;

const badgeTone = (s: Run["status"]) =>
  s === "ready" ? "success" : s === "working" ? "saffron" : s === "failed" ? "error" : "neutral";

// ───────────────────────────────────────────────────────────── shell ──────

function AppShell({
  screen,
  setScreen,
  onPalette,
  children,
}: {
  screen: Screen;
  setScreen: (s: Screen) => void;
  onPalette: () => void;
  children: React.ReactNode;
}) {
  const { overview, me, settings, repoFilter, setRepoFilter, runs, error, signOut } = useData();
  const counts = overview?.counts;
  const nav: [Screen, IconName, string, number | undefined][] = [
    ["home", "home", "Home", counts ? counts.ready + counts.person : undefined],
    ["repair", "repair", "Repair runs", counts?.working || undefined],
    ["logs", "terminal", "Failure log", undefined],
    ["conflict", "conflict", "Merge conflicts", undefined],
    ["pr", "pull", "Pull requests", undefined],
    ["people", "person", "Sent to a person", counts?.person || undefined],
    ["history", "history", "Past fixes", undefined],
  ];
  const workspace = settings?.workspace ?? "Team Mosambi";
  const repos = Array.from(new Set([...(settings?.repositories.map((r) => r.name) ?? []), ...runs.map((r) => r.repo)]));
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button className="brand" onClick={() => setScreen("home")}>
          <Mark />
          <span>ARCANE</span>
        </button>
        <div className="workspace" role="presentation">
          <span className="workspace-mark">{workspace.split(" ").map((w) => w[0]).join("").slice(0, 2)}</span>
          <span>
            <strong>{workspace}</strong>
            <small>Autonomous Repair Engine</small>
          </span>
        </div>
        <nav className="main-nav">
          <span className="nav-label">Workspace</span>
          {nav.map(([id, icon, label, n]) => (
            <button key={id} className={screen === id ? "active" : ""} onClick={() => setScreen(id)}>
              <Icon name={icon} />
              <span>{label}</span>
              {n ? <em>{n}</em> : null}
            </button>
          ))}
          <span className="nav-label section">Manage</span>
          <button className={screen === "settings" ? "active" : ""} onClick={() => setScreen("settings")}>
            <Icon name="settings" />
            <span>Settings</span>
          </button>
          <button className={screen === "components" ? "active" : ""} onClick={() => setScreen("components")}>
            <Icon name="sliders" />
            <span>Components</span>
          </button>
        </nav>
        <div className="sidebar-foot">
          <div className="user-card" role="presentation">
            {me?.avatar ? (
              <img src={me.avatar} alt="" width={32} height={32} style={{ borderRadius: "50%" }} />
            ) : (
              <Avatar name={me?.name ?? "Local user"} />
            )}
            <span>
              <strong>{me?.name ?? "Local user"}</strong>
              <small>{me?.login ? `@${me.login}` : ""}</small>
            </span>
            {me?.oauth && (
              <button className="icon-button" title="Sign out" aria-label="Sign out" onClick={signOut}>
                <Icon name="logout" size={16} />
              </button>
            )}
          </div>
        </div>
      </aside>
      <div className="app-main">
        <header className="topbar">
          <button className="global-search" onClick={onPalette}>
            <Icon name="search" />
            <span>Search runs, commits, repositories</span>
            <kbd>/</kbd>
          </button>
          <div className="top-actions">
            <span className={`live-pill ${error ? "off" : ""}`}>
              <i />
              {error ? "Backend offline" : overview?.pipeline.current_state === "IDLE" ? "Pipeline idle" : `Pipeline · ${overview?.pipeline.current_state ?? "…"}`}
            </span>
            <Select
              value={repoFilter}
              onChange={setRepoFilter}
              options={[{ value: "", label: "All repositories" }, ...repos.map((r) => ({ value: r, label: shortRepo(r) }))]}
            />
            <button className="command-button" onClick={onPalette}>
              <Icon name="command" size={15} />K
            </button>
          </div>
        </header>
        {error && (
          <div className="api-banner" role="alert">
            <Icon name="info" size={16} />
            <span>
              {error} Start it with <code>uvicorn api.main:app --port 8000</code>.
            </span>
          </div>
        )}
        <main className="content">{children}</main>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────── home ─────

function Home({ go, openRun }: { go: (s: Screen) => void; openRun: () => void }) {
  const { runs, overview, repoFilter, select, loaded, settings } = useData();
  const [seg, setSeg] = useState<"all" | "ready" | "working" | "person">("all");
  const [branch, setBranch] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(0);

  const scoped = runs.filter((r) => !repoFilter || r.repo === repoFilter);
  const branches = Array.from(new Set(scoped.map((r) => r.branch).filter(Boolean)));
  const decided = scoped.filter((r) => r.status !== "working");
  const rows = decided
    .filter((r) => seg === "all" || r.status === seg)
    .filter((r) => !branch || r.branch === branch)
    .filter((r) => !q || [r.repo, r.failingTest, r.sha, r.title].join(" ").toLowerCase().includes(q.toLowerCase()));
  const working = scoped.filter((r) => r.status === "working");
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const shown = rows.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);
  useEffect(() => setPage(0), [seg, branch, q, repoFilter]);
  const week = overview?.week;
  const minConf = settings?.safety.minConfidence ?? 85;

  return (
    <div className="page">
      <PageHeader
        title="Needs your decision"
        description="Repairs ARCANE has tested and prepared for you."
        action={
          <Button kind="primary" icon="plus" onClick={openRun}>
            Run repair on a build
          </Button>
        }
      />
      <div className="filter-bar">
        <div className="segmented">
          {(
            [
              ["all", "All"],
              ["ready", "Ready for review"],
              ["working", "Working"],
              ["person", "Sent to a person"],
            ] as const
          ).map(([k, label]) => (
            <button key={k} className={seg === k ? "active" : ""} onClick={() => setSeg(k)}>
              {label}
            </button>
          ))}
        </div>
        <Select
          value={branch}
          onChange={setBranch}
          options={[{ value: "", label: "All branches" }, ...branches.map((b) => ({ value: b, label: b }))]}
        />
        <SearchBox placeholder="Search fixes" value={q} onChange={setQ} />
      </div>
      {seg !== "working" && (
        <section className="table-section">
          <div className="section-head">
            <div>
              <h2>Fixes ready to review</h2>
              <p>Autonomous AI repair with sandbox testing & verified pull requests.</p>
            </div>
            <span className="count">{rows.length} {rows.length === 1 ? "fix" : "fixes"}</span>
          </div>
          {shown.length === 0 ? (
            <Empty
              title={loaded ? "No repairs yet" : "Loading…"}
              body={
                runs.length === 0
                  ? "When a CI build fails on a watched branch, ARCANE will analyze it and prepare a tested fix here. You can also start one by hand."
                  : "Nothing matches these filters."
              }
              action={
                runs.length === 0 ? (
                  <Button kind="primary" icon="plus" onClick={openRun}>
                    Run repair on a build
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <div className="data-table home-table">
              <div className="tr th">
                <span>Repository and branch</span>
                <span>Failing test</span>
                <span>Confidence</span>
                <span>Model used</span>
                <span>Failed</span>
                <span></span>
              </div>
              {shown.map((r) => (
                <button
                  className="tr"
                  key={r.id}
                  onClick={() => {
                    select(r.id);
                    go("repair");
                  }}
                >
                  <span className="repo-cell">
                    <strong>{shortRepo(r.repo)}</strong>
                    <small>
                      <Icon name="branch" size={13} />
                      {r.branch}
                    </small>
                  </span>
                  <span className="mono">{r.failingTest || r.title}</span>
                  <span>
                    <strong className={r.confidence != null && r.confidence < minConf ? "low" : ""}>
                      {confidenceLabel(r.confidence).split(" · ")[0]}
                    </strong>
                    <small>{confidenceLabel(r.confidence).split(" · ")[1]}</small>
                  </span>
                  <span>{r.model || "—"}</span>
                  <span>{timeAgo(r.startedAt)}</span>
                  <span>
                    <Badge tone={badgeTone(r.status)}>{statusLabel[r.status]}</Badge>
                  </span>
                </button>
              ))}
            </div>
          )}
          {rows.length > PAGE_SIZE && (
            <div className="table-footer">
              <span>
                Showing {page * PAGE_SIZE + 1}–{Math.min(rows.length, (page + 1) * PAGE_SIZE)} of {rows.length}
              </span>
              <div>
                <Button disabled={page === 0} onClick={() => setPage(page - 1)}>
                  Previous
                </Button>
                <Button disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>
                  Next
                </Button>
              </div>
            </div>
          )}
        </section>
      )}
      {(seg === "all" || seg === "working") && (
        <section className="working-section">
          <div className="section-head">
            <div>
              <h2>ARCANE is working on</h2>
              <p>Live repairs that are not ready for a decision yet.</p>
            </div>
          </div>
          {working.length === 0 && <p className="helper">Nothing running right now.</p>}
          {working.map((w) => {
            const done = w.steps.filter((s) => s.status === "done").length;
            const cur = w.steps.find((s) => s.status === "running");
            return (
              <button
                className="working-row"
                key={w.id}
                onClick={() => {
                  select(w.id);
                  go("repair");
                }}
              >
                <span className="working-pulse" />
                <div>
                  <strong>{shortRepo(w.repo)}</strong>
                  <p>{cur?.body || w.stage}</p>
                </div>
                <span className="time">{duration(w.startedAt, null)}</span>
                <div className="thin-progress">
                  <span style={{ width: `${Math.max(8, (done / Math.max(1, w.steps.length)) * 100)}%` }} />
                </div>
              </button>
            );
          })}
        </section>
      )}
      {week && (
        <p className="summary-sentence">
          <Icon name="shield" size={17} /> This week ARCANE prepared {week.prepared} fixes, your team opened{" "}
          {week.merged}, and {week.sentBack} {week.sentBack === 1 ? "was" : "were"} sent back.
          {overview?.avgSeconds != null && ` Average repair time: ${duration(new Date(Date.now() - overview.avgSeconds * 1000).toISOString(), null)}.`}
        </p>
      )}
    </div>
  );
}

// ───────────────────────────────────────────────────────── run dialog ─────

function RunDialog({ close, started }: { close: () => void; started: (r: Run) => void }) {
  const { settings, notify, refresh } = useData();
  const repos = settings?.repositories ?? [];
  const [repo, setRepo] = useState(repos[0]?.name ?? "");
  const [mine, setMine] = useState<GhRepo[]>([]);
  const [repoHint, setRepoHint] = useState("Loading repositories…");
  useEffect(() => {
    api.ghRepos().then(({ repos }) => {
      setMine(repos);
      setRepoHint(repos.length ? "owner/name" : "Enter a repository as owner/name.");
    }).catch(() => setRepoHint("Could not load repositories. Enter owner/name."));
  }, []);
  const [branch, setBranch] = useState("main");
  const [workflow, setWorkflow] = useState("");
  const [context, setContext] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const submit = async () => {
    setBusy(true);
    setErr("");
    try {
      const run = await api.createRun({ repo, branch, workflowRun: workflow, context });
      notify("Repair started");
      await refresh();
      started(run);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
      setBusy(false);
    }
  };
  return (
    <Modal
      title="Run repair on a build"
      description="Point ARCANE at a failed GitHub Actions run."
      onClose={close}
      footer={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button kind="primary" onClick={submit} disabled={busy || (!repo && !workflow)}>
            {busy ? "Starting…" : "Start repair"}
          </Button>
        </>
      }
    >
      <div className="form-grid two">
        <Field label="Repository" helper={repoHint}>
          <input value={repo} onChange={(e) => setRepo(e.target.value)} list="known-repos" placeholder="owner/repository" />
          <datalist id="known-repos">
            {[...repos.map((r) => r.name), ...mine.map((r) => r.name)].filter((n, i, a) => a.indexOf(n) === i).map((n) => (
              <option key={n} value={n} />
            ))}
          </datalist>
        </Field>
        <Field label="Branch">
          <input value={branch} onChange={(e) => setBranch(e.target.value)} />
        </Field>
      </div>
      <Field label="Workflow run URL or ID" helper="Paste a GitHub Actions URL or the numeric run ID. Optional.">
        <input value={workflow} onChange={(e) => setWorkflow(e.target.value)} placeholder="https://github.com/owner/repo/actions/runs/123" />
      </Field>
      <Field label="Extra context for ARCANE" helper="Included with the failure log sent to the analyst.">
        <textarea rows={4} value={context} onChange={(e) => setContext(e.target.value)} placeholder="For example: the provider started retrying webhooks last week." />
      </Field>
      {err && <p className="inline-error">{err}</p>}
    </Modal>
  );
}

// ─────────────────────────────────────────────────────── repair run ───────

interface DiffLine {
  old: string;
  neu: string;
  text: string;
  kind: "ctx" | "add" | "del";
}
interface DiffFile {
  path: string;
  add: number;
  del: number;
  hunks: { head: string; lines: DiffLine[] }[];
}

function parseDiff(diff: string): DiffFile[] {
  const files: DiffFile[] = [];
  let file: DiffFile | null = null;
  let hunk: DiffFile["hunks"][number] | null = null;
  let o = 0;
  let n = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("--- ")) {
      const p = line.slice(4).trim().replace(/^a\//, "");
      file = { path: p, add: 0, del: 0, hunks: [] };
      files.push(file);
      hunk = null;
    } else if (line.startsWith("+++ ")) {
      if (file) file.path = line.slice(4).trim().replace(/^b\//, "") || file.path;
    } else if (line.startsWith("@@")) {
      const m = /@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(line);
      o = m ? Number(m[1]) : 1;
      n = m ? Number(m[2]) : 1;
      if (!file) {
        file = { path: "patch", add: 0, del: 0, hunks: [] };
        files.push(file);
      }
      hunk = { head: line, lines: [] };
      file.hunks.push(hunk);
    } else if (hunk && file) {
      if (line.startsWith("+")) {
        hunk.lines.push({ old: "", neu: String(n++), text: line, kind: "add" });
        file.add++;
      } else if (line.startsWith("-")) {
        hunk.lines.push({ old: String(o++), neu: "", text: line, kind: "del" });
        file.del++;
      } else if (line.startsWith("\\")) {
        continue;
      } else {
        hunk.lines.push({ old: String(o++), neu: String(n++), text: line, kind: "ctx" });
      }
    }
  }
  return files;
}

function DiffPanel({ run }: { run: Run | null }) {
  const { notify } = useData();
  const files = useMemo(() => parseDiff(run?.patchDiff ?? ""), [run?.patchDiff]);
  const [sel, setSel] = useState(0);
  useEffect(() => setSel(0), [run?.id]);
  const f = files[Math.min(sel, files.length - 1)];
  const totalAdd = files.reduce((a, x) => a + x.add, 0);
  const totalDel = files.reduce((a, x) => a + x.del, 0);
  return (
    <section className="diff-panel">
      <div className="diff-toolbar">
        <div>
          <h2>Proposed changes</h2>
          <span>
            {files.length} {files.length === 1 ? "file" : "files"} · +{totalAdd} −{totalDel}
          </span>
        </div>
        <div>
          <button
            title="Copy patch"
            onClick={() => {
              navigator.clipboard?.writeText(run?.patchDiff ?? "");
              notify("Patch copied");
            }}
          >
            <Icon name="copy" size={15} />
          </button>
        </div>
      </div>
      {!f ? (
        <Empty
          title={run?.status === "working" ? "Writing the fix…" : "No patch produced"}
          body={run?.status === "working" ? "The proposed diff appears here as soon as the patch generator finishes." : "ARCANE did not generate a diff for this run."}
        />
      ) : (
        <div className="diff-layout">
          <aside className="file-tree">
            <strong>Changed files</strong>
            {files.map((x, i) => (
              <button key={x.path + i} className={i === sel ? "active" : ""} onClick={() => setSel(i)}>
                <Icon name="file" size={15} />
                <span>{x.path.split("/").pop()}</span>
                <em>
                  +{x.add} −{x.del}
                </em>
              </button>
            ))}
            <div className="file-context">
              <Icon name="folder" size={14} />
              <span>{f.path.includes("/") ? f.path.split("/").slice(0, -1).join("/") : "repository root"}</span>
            </div>
          </aside>
          <div className="code-diff">
            <div className="file-head">
              <span>{f.path}</span>
              {run?.testsPassed ? <Badge tone="success">Validated</Badge> : <Badge tone="saffron">Not validated</Badge>}
            </div>
            {f.hunks.map((h, hi) => (
              <div key={hi}>
                <div className="hunk">{h.head}</div>
                {h.lines.map((l, i) => (
                  <div className={`code-line ${l.kind}`} key={i}>
                    <span className="ln">{l.old}</span>
                    <span className="ln">{l.neu}</span>
                    <code>{l.text}</code>
                  </div>
                ))}
              </div>
            ))}
            {run?.rootCause && (
              <div className="inline-note">
                <Avatar name="ARCANE" small />
                <div>
                  <strong>Why this change</strong>
                  <p>{run.rootCause}</p>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function RepairRun({ go, retry, assign }: { go: (s: Screen) => void; retry: () => void; assign: () => void }) {
  const { runs, repoFilter, selected, detail, select } = useData();
  const [open, setOpen] = useState<number | null>(null);
  const [filter, setFilter] = useState<"all" | "ready" | "working">("all");
  const [q, setQ] = useState("");
  const list = runs
    .filter((r) => !repoFilter || r.repo === repoFilter)
    .filter((r) => filter === "all" || (filter === "ready" ? r.status === "ready" : r.status === "working"))
    .filter((r) => !q || [r.repo, r.failingTest, r.sha, r.title].join(" ").toLowerCase().includes(q.toLowerCase()));
  const run = detail ?? selected;
  const steps = run?.steps ?? [];
  const current = open ?? Math.max(0, steps.findIndex((s) => s.status === "running"));
  const counts = {
    all: runs.length,
    ready: runs.filter((r) => r.status === "ready").length,
    working: runs.filter((r) => r.status === "working").length,
  };
  useEffect(() => setOpen(null), [run?.id]);

  return (
    <div className="repair-layout">
      <aside className="run-rail">
        <div className="run-rail-head">
          <h2>Repair runs</h2>
        </div>
        <SearchBox placeholder="Search runs" value={q} onChange={setQ} />
        <div className="chips">
          {(["all", "ready", "working"] as const).map((k) => (
            <button key={k} className={filter === k ? "active" : ""} onClick={() => setFilter(k)}>
              {k[0].toUpperCase() + k.slice(1)} {counts[k]}
            </button>
          ))}
        </div>
        <div className="run-list">
          {list.length === 0 && <p className="helper">No runs yet.</p>}
          {list.map((r) => (
            <button className={r.id === run?.id ? "selected" : ""} key={r.id} onClick={() => select(r.id)}>
              <span>
                <strong>{shortRepo(r.repo)}</strong>
                <Badge tone={badgeTone(r.status)}>{statusLabel[r.status]}</Badge>
              </span>
              <code>{r.failingTest || r.sha.slice(0, 7)}</code>
              <small>{timeAgo(r.startedAt)}</small>
            </button>
          ))}
        </div>
      </aside>
      {!run ? (
        <div className="repair-workspace">
          <Empty title="No run selected" body="Pick a run on the left, or start one from Home." />
        </div>
      ) : (
        <div className="repair-workspace">
          <div className="repair-title">
            <div>
              <span className="breadcrumbs">
                {run.repo} <b>/</b> <code>{run.branch}</code> <b>/</b> Run #{run.id}
              </span>
              <h1>{run.title}</h1>
              <p>
                {run.sha && !run.sha.startsWith("manual") ? (
                  <>
                    Triggered by commit <code>{run.sha.slice(0, 7)}</code>
                    {run.author && ` by ${run.author}`}.{" "}
                  </>
                ) : null}
                {run.status === "working" && `Currently: ${run.stage.toLowerCase()}.`}
                {run.status === "ready" && (run.isDraftPr || run.validationStatus === "SKIPPED" ? "ARCANE prepared a draft repair proposal (sandbox skipped in cloud)." : "ARCANE prepared a tested fix.")}
                {run.status === "person" && (run.reason || "ARCANE needs a person to decide.")}
                {run.status === "failed" && (run.error || "The repair failed.")}
              </p>
            </div>
            <Badge tone={badgeTone(run.status)}>
              {run.status === "ready" ? (run.isDraftPr || run.validationStatus === "SKIPPED" ? "Draft PR ready for review" : "Fix ready to review") : statusLabel[run.status]}
            </Badge>
          </div>
          <div className="repair-body">
            <section className="timeline-panel">
              <div className="panel-title">
                <h2>Repair timeline</h2>
                <button className="text-button" onClick={() => go("logs")}>
                  View failure log
                </button>
              </div>
              <div className="timeline">
                {steps.length === 0 && <p className="helper">Waiting for repair steps…</p>}
                {steps.map((step, i) => (
                  <button
                    key={step.key}
                    className={`timeline-step ${current === i ? "open" : ""} ${
                      step.status === "failed" ? "failed" : step.key === "ready" && step.status === "done" ? "ready" : step.status
                    }`}
                    onClick={() => setOpen(i)}
                  >
                    <span className="seam-node">
                      {step.status === "failed" ? <Icon name="x" size={13} /> : step.status === "pending" ? null : <Icon name="check" size={13} />}
                    </span>
                    <span className="step-copy">
                      <strong>{step.title}</strong>
                      <p>{step.body}</p>
                      {current === i && step.evidence && <code className="evidence">{step.evidence}</code>}
                    </span>
                    <Icon name="chevron" size={15} />
                  </button>
                ))}
              </div>
              <div className="model-footnote">
                <Icon name="spark" size={15} />
                <span>
                  {run.model || "Model pending"} · {run.attempts} {run.attempts === 1 ? "attempt" : "attempts"} ·{" "}
                  {duration(run.startedAt, run.finishedAt)}
                  {run.memoryHit && " · reused a past fix"}
                </span>
              </div>
            </section>
            <DiffPanel run={detail} />
          </div>
          <div className="decision-bar">
            <div>
              <strong>
                {run.testVerification === "NOT_VERIFIED" || run.validationStatus === "SKIPPED" || run.isDraftPr
                  ? (run.prUrl ? "Draft PR open" : run.validationStatus === "ERROR" ? "Static checks failed" : "Static checks passed") + " · Docker tests not verified"
                  : run.testsPassed == null
                  ? "Tests not run yet"
                  : run.testsPassed
                  ? "Sandbox tests passed"
                  : "Sandbox tests failed"}
              </strong>
              <span>
                {run.confidence != null ? `${run.confidence}% confidence` : "No confidence score yet"}
                {run.isDraftPr && " · draft PR (review required)"}
                {run.autoApproved && " · auto-approved"}
                {run.decision === "accepted" && " · pull request opened"}
                {run.decision === "rejected" && " · rejected"}
              </span>
            </div>
            <div className="decision-actions">
              <Button onClick={retry} disabled={run.status === "working"}>
                Try again with a hint
              </Button>
              <Button onClick={assign} disabled={run.status === "working"}>
                Send to a person
              </Button>
              <Button kind="primary" icon="pull" onClick={() => go("pr")} disabled={run.status === "working" || !detail?.patchDiff}>
                Open pull request
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function RetryDialog({ close, go }: { close: () => void; go: (s: Screen) => void }) {
  const { selected, settings, select, notify, refresh } = useData();
  const [hint, setHint] = useState("");
  const [files, setFiles] = useState<string[]>(selected?.failingFile ? [selected.failingFile] : []);
  const [file, setFile] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const submit = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      const r = await api.retry(selected.id, { hint, files, model: settings?.models[0]?.name ?? "" });
      select(r.id);
      notify("New repair attempt started");
      await refresh();
      go("repair");
      close();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
      setBusy(false);
    }
  };
  return (
    <Modal
      title="Try again with a hint"
      description="ARCANE keeps the failure evidence and starts a new run with your guidance."
      onClose={close}
      footer={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button kind="primary" onClick={submit} disabled={busy || !selected}>
            {busy ? "Starting…" : "Run again"}
          </Button>
        </>
      }
    >
      <Field label="Tell ARCANE what to look at">
        <textarea rows={4} value={hint} onChange={(e) => setHint(e.target.value)} placeholder="For example: check whether the retry happens before the lock is acquired." />
      </Field>
      <Field label="Files to focus on">
        <div className="chip-input">
          {files.map((f) => (
            <span key={f}>
              {f}{" "}
              <button onClick={() => setFiles(files.filter((x) => x !== f))}>
                <Icon name="x" size={12} />
              </button>
            </span>
          ))}
          <input
            placeholder="Add a file…"
            value={file}
            onChange={(e) => setFile(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && file.trim()) {
                setFiles([...files, file.trim()]);
                setFile("");
              }
            }}
          />
        </div>
      </Field>
      {err && <p className="inline-error">{err}</p>}
    </Modal>
  );
}

function AssignDialog({ close }: { close: () => void }) {
  const { selected, settings, notify, refresh } = useData();
  const team = settings?.team ?? [];
  const [who, setWho] = useState(team[0]?.name ?? "");
  const submit = async () => {
    if (!selected) return;
    await api.decide(selected.id, { action: "assign", assignee: who });
    notify(`Sent to ${who}`);
    await refresh();
    close();
  };
  return (
    <Modal
      title="Send to a person"
      description="They will see this fix under “Sent to a person”."
      onClose={close}
      footer={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button kind="primary" onClick={submit} disabled={!who}>
            Send
          </Button>
        </>
      }
    >
      <Field label="Assignee">
        <Select value={who} onChange={setWho} options={team.map((t) => ({ value: t.name, label: t.name }))} />
      </Field>
    </Modal>
  );
}

// ───────────────────────────────────────────────────── failure log ────────

function FailureLog({ go }: { go: (s: Screen) => void }) {
  const { detail, selected, notify } = useData();
  const run = detail ?? selected;
  const [q, setQ] = useState("");
  const [level, setLevel] = useState<"all" | "WARN" | "ERROR">("all");
  const [wrap, setWrap] = useState(true);
  const lines = useMemo(() => {
    const input = (detail?.failureLog ?? "")
      .split("\n")
      .filter(Boolean)
      .map((m) => ({ t: detail?.startedAt ?? "", level: /error|fail|assert/i.test(m) ? "ERROR" : "INFO", msg: m, src: "ci" }));
    const pipe = (detail?.logs ?? []).map((l) => ({ ...l, src: "arcane" }));
    return [...input, ...pipe];
  }, [detail]);
  const view = lines.filter(
    (l) => (level === "all" || (level === "ERROR" ? l.level === "ERROR" : l.level !== "INFO")) && (!q || l.msg.toLowerCase().includes(q.toLowerCase())),
  );
  const firstErr = lines.findIndex((l) => l.level === "ERROR");
  if (!run) return <div className="page"><Empty title="No run selected" body="Choose a run from Home to see its log." /></div>;
  return (
    <div className="page logs-page">
      <PageHeader
        title="Failure log"
        description={`${run.repo} · run #${run.id}${run.workflowRun ? ` · ${run.workflowRun}` : ""}`}
        action={
          <Button
            icon="external"
            onClick={() => window.open(run.workflowRun || `https://github.com/${run.repo}/commit/${run.sha}`, "_blank")}
            disabled={run.sha.startsWith("manual") && !run.workflowRun}
          >
            Open in GitHub
          </Button>
        }
      />
      <div className="log-controls">
        <SearchBox placeholder="Search logs" value={q} onChange={setQ} />
        <div className="segmented">
          <button className={level === "ERROR" ? "active" : ""} onClick={() => setLevel("ERROR")}>Errors only</button>
          <button className={level === "WARN" ? "active" : ""} onClick={() => setLevel("WARN")}>Warnings</button>
          <button className={level === "all" ? "active" : ""} onClick={() => setLevel("all")}>All</button>
        </div>
        <label className="toggle-label">
          <Toggle checked={wrap} onChange={setWrap} />
          Wrap lines
        </label>
        <Button
          icon="copy"
          onClick={() => {
            navigator.clipboard?.writeText(view.map((l) => `${l.level} ${l.msg}`).join("\n"));
            notify("Log copied");
          }}
        >
          Copy
        </Button>
      </div>
      {firstErr >= 0 && (
        <div className="pinned-error">
          <span>First error</span>
          <code>{lines[firstErr].msg.slice(0, 160)}</code>
          <button onClick={() => setLevel("ERROR")}>Show errors</button>
        </div>
      )}
      <div className="log-layout">
        <div className="log-viewer" style={wrap ? undefined : { whiteSpace: "nowrap", overflowX: "auto" }}>
          {view.length === 0 && <Empty title="No log lines" body={run.status === "working" ? "Waiting for the pipeline to emit output…" : "Nothing matches this filter."} />}
          {view.map((l, i) => (
            <div className={`log-line ${l.level.toLowerCase()}`} key={i}>
              <span>{String(i + 1).padStart(3, "0")}</span>
              <time>{l.t ? clock(l.t) : ""}</time>
              <b>{l.level}</b>
              <code>{l.msg}</code>
            </div>
          ))}
        </div>
        <aside className="why-card">
          <h2>Why this commit</h2>
          <p>{run.bisectReport || "Git bisect has not reported yet for this run."}</p>
          <dl>
            <dt>Suspect</dt>
            <dd><code>{run.sha.slice(0, 7) || "—"}</code></dd>
            <dt>Author</dt>
            <dd>
              {run.author ? <><Avatar name={run.author} small />{run.author}</> : "—"}
            </dd>
            <dt>Message</dt>
            <dd>{run.commitMessage || "—"}</dd>
            <dt>Started</dt>
            <dd>{timeAgo(run.startedAt)}</dd>
            <dt>Failing test</dt>
            <dd>{run.failingTest || "—"}</dd>
          </dl>
          <Button onClick={() => go("repair")}>Back to repair</Button>
        </aside>
      </div>
    </div>
  );
}

// ───────────────────────────────────────────────────────── conflict ──────

function Conflict() {
  const { detail, selected, notify } = useData();
  const run = detail ?? selected;
  const blocks = ((run?.conflict?.details ?? []) as any[]).filter((d) => d?.conflict);
  const [idx, setIdx] = useState(0);
  const b = blocks[Math.min(idx, blocks.length - 1)];
  const arcaneText = useMemo(() => {
    if (!b) return "";
    const w = b.intent?.winner ?? "ours";
    return w === "theirs" ? b.conflict.theirs : w === "merge" ? `${b.conflict.ours}${b.conflict.theirs}` : b.conflict.ours;
  }, [b]);
  const [text, setText] = useState(arcaneText);
  useEffect(() => setText(arcaneText), [arcaneText]);
  if (!run) return <div className="page"><Empty title="No run selected" body="Choose a run from Home." /></div>;
  if (!blocks.length) {
    return (
      <div className="page">
        <PageHeader title="Merge conflicts" description={`${run.repo} · run #${run.id}`} />
        <Empty
          title={run.conflict?.detected === false ? "No conflicts" : run.conflict ? "Conflict details unavailable" : "Not checked yet"}
          body={
            run.conflict
              ? "The repair merged cleanly with the target branch, so there is nothing to resolve."
              : "Conflict checking runs after the fix is written. Pick a run that has reached that step."
          }
        />
      </div>
    );
  }
  const score = Math.round((b.score ?? 0) * 100);
  return (
    <div className="page">
      <PageHeader
        title="Resolve a merge conflict"
        description={`${run.repo} · ${run.failingFile || "conflicting file"}`}
        action={<Badge tone="saffron">{blocks.length} {blocks.length === 1 ? "conflict" : "conflicts"}</Badge>}
      />
      <div className="intent-note">
        <Icon name="spark" />
        <div>
          <strong>
            ARCANE chose “{b.intent?.winner ?? "ours"}” · {score}% confidence · {b.resolution?.action?.replace("_", " ")}
          </strong>
          <p>{b.intent?.reasoning || "No reasoning was returned."}</p>
        </div>
      </div>
      {blocks.length > 1 && (
        <div className="segmented">
          {blocks.map((_, i) => (
            <button key={i} className={i === idx ? "active" : ""} onClick={() => setIdx(i)}>
              Block {i + 1}
            </button>
          ))}
        </div>
      )}
      <div className="conflict-grid">
        <section className="code-pane">
          <div className="pane-head"><div><h2>Yours</h2><span>{run.branch}</span></div></div>
          <pre>{b.conflict.ours}</pre>
        </section>
        <section className="code-pane">
          <div className="pane-head"><div><h2>Theirs</h2><span>arcane/fix-{run.sha.slice(0, 7)}</span></div></div>
          <pre>{b.conflict.theirs}</pre>
        </section>
        <section className="resolution-pane">
          <div className="pane-head">
            <div><h2>ARCANE’s resolution</h2><span>Editable</span></div>
            <button onClick={() => setText(arcaneText)}>Reset to ARCANE’s version</button>
          </div>
          <textarea value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
          <div className="resolution-check"><Icon name="check" size={15} /> {run.conflictResolution ? "Saved resolution recorded" : "Not saved yet"}</div>
        </section>
      </div>
      <div className="bottom-actions">
        <Button
          kind="primary"
          onClick={async () => {
            await api.resolveConflict(run.id, text);
            notify("Resolution saved");
          }}
        >
          Use this resolution
        </Button>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────── pull request ─────

function PRPreview() {
  const { detail, selected, notify, refresh, me } = useData();
  const run = detail ?? selected;
  const [title, setTitle] = useState("");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => {
    if (run) setTitle(`fix: ARCANE auto-repair for ${run.failingTest || run.title}`);
  }, [run?.id, run?.failingTest]);
  if (!run) return <div className="page"><Empty title="No run selected" body="Choose a run from Home." /></div>;
  const stats = run.diffSummary;
  const sandboxSkipped = run.testVerification === "NOT_VERIFIED" || run.validationStatus === "SKIPPED" || run.isDraftPr;
  const mermaid = `Build failed → ${run.sha.slice(0, 7)} found → fix written → sandbox ${sandboxSkipped ? "skipped" : run.testsPassed ? "passed" : "failed"} → review`;
  const open = async () => {
    setBusy(true);
    setErr("");
    try {
      const r = await api.openPr(run.id, { title, comment });
      notify(r.prUrl ? "Pull request opened" : "Done");
      await refresh();
      if (r.prUrl) window.open(r.prUrl, "_blank");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="page pr-page">
      <PageHeader
        title="Pull request preview"
        description="Review exactly what your team will see on GitHub."
        action={run.prUrl ? <Badge tone="success">Opened</Badge> : <Badge tone={run.status === "ready" ? "success" : "saffron"}>{run.status === "ready" ? "Ready to open" : statusLabel[run.status]}</Badge>}
      />
      <div className="pr-layout">
        <div className="pr-document">
          <Field label="Title">
            <input value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <div className="branch-flow">
            <code>arcane/fix-{run.sha.slice(0, 7)}</code>
            <span>into</span>
            <code>{run.branch}</code>
            {sandboxSkipped ? (
              <Badge tone="saffron">Docker tests skipped · Unverified</Badge>
            ) : (
              run.testsPassed != null && <Badge tone={run.testsPassed ? "success" : "error"}>{run.testsPassed ? "Sandbox tests passed" : "Sandbox tests failed"}</Badge>
            )}
            {run.executionMode && <Badge tone="neutral">{run.executionMode.toUpperCase()} MODE</Badge>}
          </div>
          <div className="pr-section">
            <h2>What broke</h2>
            <p>{run.failingTest ? <><code>{run.failingTest}</code> failed. </> : null}{run.commitMessage || "The build failed after the latest push."}</p>
          </div>
          <div className="pr-section">
            <h2>Why</h2>
            <p>{run.rootCause || "Root cause not available yet."}</p>
            {run.bisectReport && <p>{run.bisectReport}</p>}
          </div>
          <div className="pr-section">
            <h2>What changed</h2>
            <ul>
              <li>{stats.files} {stats.files === 1 ? "file" : "files"} changed (+{stats.add} −{stats.del}){run.failingFile ? ` in ${run.failingFile}` : ""}.</li>
              {run.cascadeReport && <li>{run.cascadeReport}</li>}
            </ul>
          </div>
          <div className="pr-section">
            <h2>{sandboxSkipped ? "Verification & Checks" : "What was tested"}</h2>
            <div className="test-grid">
              {sandboxSkipped ? (
                <>
                  <span><Icon name="check" />Analysis completed</span>
                  <span><Icon name="check" />Commit inspection completed</span>
                  {stats.files > 0 && <span><Icon name="check" />Candidate patch generated</span>}
                  <span><Icon name={run.validationStatus === "ERROR" ? "x" : "check"} />{run.validationStatus === "ERROR" ? "Static checks failed" : "Static checks passed"}</span>
                  <span><Icon name="clock" />Docker sandbox skipped (cloud)</span>
                  <span><Icon name="clock" />Tests not verified</span>
                  {run.prUrl && <span><Icon name="check" />Draft PR open for review</span>}
                </>
              ) : (
                <>
                  <span><Icon name="check" />{run.validatorSummary || "Sandbox validation"}</span>
                  <span><Icon name="check" />Docker sandbox, network off</span>
                  <span><Icon name="check" />{run.confidence != null ? `${run.confidence}% confidence` : "No confidence score"}</span>
                  <span><Icon name="check" />{run.conflict?.detected ? "Merge conflict resolved" : "No merge conflicts"}</span>
                </>
              )}
            </div>
          </div>
          <div className="pr-section">
            <h2>New regression test</h2>
            <code className="code-block" style={{ whiteSpace: "pre-wrap" }}>
              {detail?.regressionTest || "No regression test was generated for this run."}
            </code>
          </div>
          <div className="flow-diagram">
            <span className="fail">Build failed</span><i />
            <span>Commit found</span><i />
            <span>Fix written</span><i />
            {sandboxSkipped ? (
              <>
                <span>Static checks passed</span><i />
                <span className="warn">Sandbox skipped</span><i />
                <span>{run.prUrl ? "Draft PR review" : "Review proposal"}</span>
              </>
            ) : (
              <>
                <span>Sandbox {run.testsPassed ? "passed" : "failed"}</span><i />
                <span className="ready">Review</span>
              </>
            )}
          </div>
          <span className="helper" hidden>{mermaid}</span>
          <Field label="Add a comment">
            <textarea rows={4} value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Leave context for reviewers…" />
          </Field>
          {err && <p className="inline-error">{err}</p>}
        </div>
        <aside className="pr-sidebar">
          <Field label="Reviewer">
            <div className="picker-value"><Avatar name={me?.name ?? "Local user"} small /><span>{me?.name ?? "Local user"}</span></div>
          </Field>
          <Field label="Labels">
            <div className="label-list"><span>arcane-fix</span><span>{shortRepo(run.repo)}</span></div>
          </Field>
          <div className="merge-checks">
            <strong>Run summary</strong>
            <span><Icon name="check" />{run.model || "Model n/a"}</span>
            <span><Icon name="check" />{run.attempts} {run.attempts === 1 ? "attempt" : "attempts"}</span>
            <span><Icon name="check" />{duration(run.startedAt, run.finishedAt)}</span>
          </div>
        </aside>
      </div>
      <div className="bottom-actions">
        {run.prUrl && <Button icon="external" onClick={() => window.open(run.prUrl!, "_blank")}>Open on GitHub</Button>}
        <Button kind="primary" onClick={open} disabled={busy || run.status === "working" || !!run.prUrl}>
          {run.prUrl ? "Pull request opened" : busy ? "Opening…" : "Open pull request"}
        </Button>
      </div>
    </div>
  );
}

// ───────────────────────────────────────────────── sent to a person ───────

function SentToPerson({ reject, go }: { reject: (id: string) => void; go: (s: Screen) => void }) {
  const { runs, repoFilter, settings, select, notify, refresh } = useData();
  const [q, setQ] = useState("");
  const rows = runs
    .filter((r) => r.status === "person" && r.decision !== "rejected")
    .filter((r) => !repoFilter || r.repo === repoFilter)
    .filter((r) => !q || [r.repo, r.reason, r.failingTest].join(" ").toLowerCase().includes(q.toLowerCase()));
  const team = settings?.team ?? [];
  return (
    <div className="page">
      <PageHeader title="Sent to a person" description="Fixes where context or intent matters more than model confidence." />
      <div className="filter-bar">
        <SearchBox placeholder="Search uncertain fixes" value={q} onChange={setQ} />
      </div>
      {rows.length === 0 ? (
        <Empty title="Nothing waiting on a person" body="When ARCANE’s confidence is below your safety rule or a repair is escalated, it lands here." />
      ) : (
        <div className="data-table people-table">
          <div className="tr th">
            <span>Repository</span>
            <span>Why ARCANE wasn’t sure</span>
            <span>Best attempt</span>
            <span>Assignee</span>
            <span>Actions</span>
          </div>
          {rows.map((r) => (
            <div className="tr" key={r.id}>
              <span className="repo-cell">
                <strong>{shortRepo(r.repo)}</strong>
                <small>{timeAgo(r.startedAt)}</small>
              </span>
              <span>
                {r.reason || "Escalated"}
                <small>{r.confidence != null ? `${r.confidence}% confidence` : "no confidence score"}</small>
              </span>
              <span>{r.title}</span>
              <span>
                <Select
                  value={r.assignee}
                  onChange={async (v) => {
                    await api.decide(r.id, { action: "assign", assignee: v });
                    refresh();
                  }}
                  options={[{ value: "", label: "Unassigned" }, ...team.map((t) => ({ value: t.name, label: t.name }))]}
                />
              </span>
              <span className="inline-actions">
                <button
                  onClick={async () => {
                    await api.decide(r.id, { action: "accept" });
                    notify("Marked as accepted");
                    refresh();
                  }}
                >
                  Accept
                </button>
                <button
                  onClick={() => {
                    select(r.id);
                    go("repair");
                  }}
                >
                  Review
                </button>
                <button className="reject" onClick={() => reject(r.id)}>Reject</button>
              </span>
            </div>
          ))}
        </div>
      )}
      <div className="table-footer"><span>{rows.length} {rows.length === 1 ? "fix needs" : "fixes need"} a person</span></div>
    </div>
  );
}

function RejectDialog({ runId, close }: { runId: string | null; close: () => void }) {
  const { notify, refresh } = useData();
  const reasons = ["The change does not match our intent", "The fix is incorrect", "Too risky for this codebase", "Duplicate of another fix"];
  const [reason, setReason] = useState(reasons[0]);
  const [note, setNote] = useState("");
  return (
    <Modal
      title="Reject this fix"
      description="Your reason is saved with the run so the team can see why."
      onClose={close}
      footer={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button
            kind="danger"
            onClick={async () => {
              if (runId) await api.decide(runId, { action: "reject", reason, note });
              notify("Fix rejected");
              refresh();
              close();
            }}
          >
            Reject fix
          </Button>
        </>
      }
    >
      <Field label="Reason">
        <Select value={reason} onChange={setReason} options={reasons.map((r) => ({ value: r, label: r }))} />
      </Field>
      <Field label="What should ARCANE learn? (optional)" helper="Do not include secrets or customer data.">
        <textarea rows={4} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Explain the constraint or preferred approach…" />
      </Field>
    </Modal>
  );
}

// ─────────────────────────────────────────────────────── past fixes ───────

function PastFixes() {
  const { notify } = useData();
  const [q, setQ] = useState("");
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [available, setAvailable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [sel, setSel] = useState(0);
  useEffect(() => {
    setLoading(true);
    const t = setTimeout(() => {
      api
        .memory(q)
        .then((r) => {
          setItems(r.items);
          setTotal(r.total);
          setAvailable(r.available);
          setSel(0);
        })
        .catch(() => setAvailable(false))
        .finally(() => setLoading(false));
    }, q ? 350 : 0);
    return () => clearTimeout(t);
  }, [q]);
  const cur = items[sel];
  const toggle = async (v: boolean) => {
    if (!cur) return;
    await api.setMemoryReuse(cur.key, v);
    setItems(items.map((i) => (i.id === cur.id ? { ...i, reuse: v } : i)));
    notify(v ? "ARCANE may reuse this fix" : "Reuse disabled for this fix");
  };
  return (
    <div className="page">
      <PageHeader title="Past fixes" description="Repair knowledge ARCANE can reuse when a similar build breaks." />
      <div className="filter-bar">
        <SearchBox placeholder="Describe an error to find similar fixes" value={q} onChange={setQ} />
      </div>
      {!available ? (
        <Empty title="Memory is unavailable" body="The ChromaDB store could not be opened. Check CHROMADB_PATH in .env and that chromadb is installed." />
      ) : loading ? (
        <Empty title="Searching memory…" body="Loading the embedding model can take a few seconds the first time." />
      ) : items.length === 0 ? (
        <Empty title="No stored fixes yet" body="Every fix that passes validation is stored here. Run scripts/pre_populate_memory.py to seed the demo fix." />
      ) : (
        <div className="history-layout">
          <div className="history-list">
            <div className="history-head"><span>Fix</span><span>Similarity</span><span>Reuse</span></div>
            {items.map((r, i) => (
              <button className={sel === i ? "selected" : ""} onClick={() => setSel(i)} key={r.id}>
                <div>
                  <strong>{r.title}</strong>
                  <span>{r.repo} · {r.language}</span>
                </div>
                <div className="similarity">
                  <span><i style={{ width: `${r.similarity ?? 0}%` }} /></span>
                  <b>{r.similarity != null ? `${r.similarity}%` : "—"}</b>
                </div>
                <span>{r.reuses} {r.reuses === 1 ? "reuse" : "reuses"}</span>
                <Icon name="chevron" size={15} />
              </button>
            ))}
            <div className="table-footer"><span>Showing {items.length} of {total} fixes</span></div>
          </div>
          {cur && (
            <aside className="detail-drawer">
              <div className="drawer-head">
                {cur.similarity != null ? <Badge tone="success">{cur.similarity}% match</Badge> : <Badge>Stored fix</Badge>}
              </div>
              <h2>{cur.title}</h2>
              <p>Test: {cur.testFile || "n/a"}</p>
              <dl>
                <dt>Repository</dt><dd>{cur.repo}</dd>
                <dt>Commit</dt><dd><code>{cur.commit || "—"}</code></dd>
                <dt>Stored</dt><dd>{cur.date ? new Date(cur.date).toLocaleString() : "—"}</dd>
              </dl>
              <div className="memory-code"><code style={{ whiteSpace: "pre-wrap" }}>{cur.patch || "No patch stored."}</code></div>
              <label className="toggle-row">
                <span>
                  <strong>Allow ARCANE to reuse this fix</strong>
                  <small>When off, similar failures go through full analysis instead.</small>
                </span>
                <Toggle checked={cur.reuse} onChange={toggle} />
              </label>
            </aside>
          )}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────── settings ─────

const settingTabs = ["Repositories", "Models", "Safety", "Team", "Notifications", "Audit log"];

function Settings() {
  const { settings, reloadSettings, notify } = useData();
  const [tab, setTab] = useState("Repositories");
  const [draft, setDraft] = useState<Settings | null>(null);
  useEffect(() => {
    if (settings && !draft) setDraft(structuredClone(settings));
  }, [settings, draft]);
  const save = async () => {
    if (!draft) return;
    await api.saveSettings({
      repositories: draft.repositories,
      models: draft.models,
      safety: draft.safety,
      team: draft.team,
      notifications: draft.notifications,
    });
    await reloadSettings();
    notify("Settings saved");
  };
  const head = (title: string, description: string) => (
    <div className="settings-head">
      <div><h2>{title}</h2><p>{description}</p></div>
      {tab !== "Models" && <Button kind="primary" onClick={save}>Save changes</Button>}
    </div>
  );
  return (
    <div className="settings-layout">
      <aside className="settings-nav">
        <h1>Settings</h1>
        <p>{draft?.workspace}</p>
        {settingTabs.map((t) => (
          <button className={tab === t ? "active" : ""} onClick={() => setTab(t)} key={t}>{t}</button>
        ))}
      </aside>
      <div className="settings-content">
        {!draft ? <Empty title="Loading settings…" body="" /> : (
          <>
            {tab === "Repositories" && <RepositorySettings d={draft} set={setDraft} head={head} />}
            {tab === "Models" && <ModelSettings d={draft} set={setDraft} head={head} />}
            {tab === "Safety" && <SafetySettings d={draft} set={setDraft} head={head} />}
            {tab === "Team" && <TeamSettings d={draft} set={setDraft} head={head} />}
            {tab === "Notifications" && <NotificationSettings d={draft} set={setDraft} head={head} />}
            {tab === "Audit log" && <AuditLog />}
          </>
        )}
      </div>
    </div>
  );
}

interface SP {
  d: Settings;
  set: (s: Settings) => void;
  head: (t: string, d: string) => React.ReactNode;
}

function RepositorySettings({ d, set, head }: SP) {
  const { notify, reloadSettings } = useData();
  const [mine, setMine] = useState<GhRepo[] | null>(null);
  const [ghErr, setGhErr] = useState("");
  const [pick, setPick] = useState("");
  const [busy, setBusy] = useState(false);
  const loadMine = () =>
    api.ghRepos().then((r) => setMine(r.repos)).catch((e) => setGhErr(e instanceof ApiError ? e.message : String(e)));
  useEffect(() => {
    loadMine();
  }, []);
  const watch = async () => {
    if (!pick) return;
    setBusy(true);
    try {
      const r = await api.watch(pick);
      notify(r.message);
      setPick("");
      await reloadSettings();
      await loadMine();
    } catch (e) {
      notify(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const update = (i: number, patch: Partial<Settings["repositories"][number]>) =>
    set({ ...d, repositories: d.repositories.map((r, j) => (j === i ? { ...r, ...patch } : r)) });
  return (
    <>
      {head("Repositories", "How ARCANE tests and prepares fixes for each codebase.")}
      <div className="invite-row">
        <Field label="Watch one of your GitHub repositories" helper={ghErr}>
          <Select
            value={pick}
            onChange={setPick}
            options={[
              { value: "", label: mine ? "Choose a repository…" : "Loading your repositories…" },
              ...(mine ?? []).filter((r) => !r.watched).map((r) => ({ value: r.name, label: r.name + (r.private ? " 🔒" : "") })),
            ]}
          />
        </Field>
        <Button kind="primary" icon="plus" onClick={watch} disabled={!pick || busy}>
          {busy ? "Adding…" : "Watch"}
        </Button>
      </div>
      <div className="settings-block">
        <div className="repo-settings-head">
          <strong>Repository</strong><span>Test command</span><span>Language</span><span>Timeout</span><span>Auto-open PR</span>
        </div>
        {d.repositories.length === 0 && <p className="helper" style={{ padding: 16 }}>No repositories yet. Add one above, or start a repair and it will appear here.</p>}
        {d.repositories.map((r, i) => (
          <div className="repo-setting" key={r.name}>
            <span>
              <Icon name="github" size={16} />
              <strong>{r.name}</strong>
              <button
                className="icon-button"
                title="Stop watching"
                aria-label="Stop watching"
                onClick={async () => {
                  await api.unwatch(r.name);
                  await reloadSettings();
                  loadMine();
                }}
              >
                <Icon name="x" size={14} />
              </button>
            </span>
            <input value={r.testCommand} onChange={(e) => update(i, { testCommand: e.target.value })} />
            <Select
              value={r.language}
              onChange={(v) => update(i, { language: v })}
              options={["Python", "TypeScript", "JavaScript", "Go", "Kotlin", "Java"].map((l) => ({ value: l, label: l }))}
            />
            <div className="unit-input">
              <input value={r.timeoutMin} onChange={(e) => update(i, { timeoutMin: Number(e.target.value) || 0 })} />
              <span>min</span>
            </div>
            <Toggle checked={r.autoPr} onChange={(v) => update(i, { autoPr: v })} />
          </div>
        ))}
      </div>
      <div className="settings-note">
        <Icon name="info" />
        <p>
          Webhook endpoint: <code>{d.integrations.webhookPath}</code> · GitHub token {d.integrations.githubPat ? "configured" : "missing"} · webhook secret{" "}
          {d.integrations.webhookSecret ? "configured" : "missing"}. Secrets are read from <code>.env</code> and never sent to the browser.
        </p>
      </div>
    </>
  );
}

function ModelSettings({ head }: SP) {
  const [rows, setRows] = useState<ModelCredential[]>([]);
  const [provider, setProvider] = useState<ModelCredential["provider"]>("google");
  const [model, setModel] = useState("");
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(true);
  useEffect(() => {
    api.modelKeys().then(setRows).catch(e => setError(e.message)).finally(() => setBusy(false));
  }, []);

  async function update(id?: number) {
    setBusy(true);
    setError("");
    try {
      if (id === undefined) {
        await api.addModelKey(provider, model.trim(), key.trim());
        setKey("");
      } else {
        await api.removeModelKey(id);
      }
      setRows(await api.modelKeys());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not update model keys.");
    } finally {
      setBusy(false);
    }
  }

  return <>
    {head("Models", "Your repairs try your keys in the order shown, then use the server fallbacks.")}
    <div className="settings-block">
      <h3>Personal model keys</h3>
      <p>Each listed provider can receive repair prompts, including repository code and failure logs. Changes apply to new repairs.</p>
      {rows.map((row, i) => <div className="model-row" key={row.id}>
        <span className="order">{i + 1}</span>
        <strong>{row.provider} / {row.model}</strong>
        <Button disabled={busy} onClick={() => update(row.id)}>Remove</Button>
      </div>)}
      {!busy && !error && !rows.length && <p>Using server fallbacks.</p>}
      <Field label="Provider"><select value={provider} onChange={e => setProvider(e.target.value as ModelCredential["provider"])}>
        <option value="google">Google</option><option value="openai">OpenAI</option><option value="groq">Groq</option>
      </select></Field>
      <Field label="Model ID"><input value={model} onChange={e => setModel(e.target.value)} placeholder="Model ID from your provider" /></Field>
      <Field label="API key"><input type="password" autoComplete="new-password" value={key} onChange={e => setKey(e.target.value)} /></Field>
      <Button disabled={busy || !key.trim() || !model.trim()} onClick={() => update()}>Add key</Button>
      {error && <p role="alert">{error}</p>}
    </div>
  </>;
}

function SafetySettings({ d, set, head }: SP) {
  const s = d.safety;
  const { runs } = useData();
  const patch = (p: Partial<Settings["safety"]>) => set({ ...d, safety: { ...s, ...p } });
  const [branch, setBranch] = useState("");
  const scored = runs.filter((r) => r.confidence != null).slice(0, 12);
  const wouldAct = scored.filter((r) => (r.confidence ?? 0) >= s.minConfidence).length;
  return (
    <>
      {head("Safety", "Bound what ARCANE can change and when it can act without waiting.")}
      <div className="settings-block form-stack">
        <Field label={`Minimum confidence to open a pull request · ${s.minConfidence}%`}>
          <input className="range" type="range" min="50" max="100" value={s.minConfidence} onChange={(e) => patch({ minConfidence: Number(e.target.value) })} />
          <div className="range-labels"><span>Ask more often</span><span>Act more often</span></div>
        </Field>
        <p className="preview-sentence">
          {scored.length
            ? `At ${s.minConfidence}%, ARCANE would have acted on its own for ${wouldAct} of your last ${scored.length} fixes.`
            : "Once ARCANE has scored some fixes, you’ll see how this rule would have applied."}
        </p>
        <Field label={`Send to a person below · ${s.escalateBelow}%`}>
          <input className="range" type="range" min="30" max="90" value={s.escalateBelow} onChange={(e) => patch({ escalateBelow: Number(e.target.value) })} />
        </Field>
        <Field label="Protected branches">
          <div className="chip-input">
            {s.protectedBranches.map((b) => (
              <span key={b}>{b}{" "}<button onClick={() => patch({ protectedBranches: s.protectedBranches.filter((x) => x !== b) })}><Icon name="x" size={12} /></button></span>
            ))}
            <input
              placeholder="Add branch…"
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && branch.trim()) {
                  patch({ protectedBranches: [...s.protectedBranches, branch.trim()] });
                  setBranch("");
                }
              }}
            />
          </div>
        </Field>
        <Field label="Files ARCANE must never edit" helper="Use gitignore-style path patterns.">
          <textarea rows={3} value={s.protectedFiles} onChange={(e) => patch({ protectedFiles: e.target.value })} />
        </Field>
      </div>
      <div className="settings-block">
        <div className="block-title"><div><h3>Sandbox limits</h3><p>Applied to every generated repair.</p></div></div>
        <div className="form-grid four">
          <Field label="Network"><Select value={s.network} onChange={(v) => patch({ network: v })} options={[{ value: "Off", label: "Off" }, { value: "On", label: "On" }]} /></Field>
          <Field label="CPU limit"><div className="unit-input"><input value={s.cpu} onChange={(e) => patch({ cpu: Number(e.target.value) || 0 })} /><span>cores</span></div></Field>
          <Field label="Memory"><div className="unit-input"><input value={s.memoryGb} onChange={(e) => patch({ memoryGb: Number(e.target.value) || 0 })} /><span>GB</span></div></Field>
          <Field label="Runtime cap"><div className="unit-input"><input value={s.runtimeMin} onChange={(e) => patch({ runtimeMin: Number(e.target.value) || 0 })} /><span>min</span></div></Field>
        </div>
      </div>
    </>
  );
}

function TeamSettings({ d, set, head }: SP) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("Reviewer");
  const roles = ["Admin", "Reviewer", "Viewer"].map((r) => ({ value: r, label: r }));
  return (
    <>
      {head("Team", "Choose who can approve repairs and be assigned fixes.")}
      <div className="invite-row">
        <Field label="Add by email"><input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@company.com" /></Field>
        <Field label="Role"><Select value={role} onChange={setRole} options={roles} /></Field>
        <Button
          kind="primary"
          icon="plus"
          onClick={() => {
            if (!email.includes("@")) return;
            const name = email.split("@")[0].replace(/[._]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
            set({ ...d, team: [...d.team, { name, email, role }] });
            setEmail("");
          }}
        >
          Add member
        </Button>
      </div>
      <div className="settings-block team-table">
        <div className="team-row head"><span>Member</span><span>Role</span><span>Focus</span><span></span></div>
        {d.team.map((m, i) => (
          <div className="team-row" key={m.email}>
            <span><Avatar name={m.name} /><span><strong>{m.name}</strong><small>{m.email}</small></span></span>
            <Select value={m.role} onChange={(v) => set({ ...d, team: d.team.map((x, j) => (j === i ? { ...x, role: v } : x)) })} options={roles} />
            <span>{m.lead ?? "—"}</span>
            <button className="icon-button" aria-label="Remove member" onClick={() => set({ ...d, team: d.team.filter((_, j) => j !== i) })}><Icon name="x" /></button>
          </div>
        ))}
      </div>
    </>
  );
}

function NotificationSettings({ d, set, head }: SP) {
  const n = d.notifications;
  const events: [string, string, string][] = [
    ["ready", "Fix ready to review", "When a repair passes its checks."],
    ["person", "ARCANE needs a person", "When confidence or context is insufficient."],
    ["failed", "Repair failed", "When no candidate passes in the sandbox."],
    ["started", "Repair started", "When ARCANE begins analyzing a build."],
    ["pr", "Pull request opened", "When a tested repair reaches GitHub."],
  ];
  const upd = (k: string, p: Partial<{ on: boolean; email: boolean; slack: boolean }>) =>
    set({ ...d, notifications: { ...n, events: { ...n.events, [k]: { ...n.events[k], ...p } } } });
  return (
    <>
      {head("Notifications", "Choose where and when ARCANE contacts your team.")}
      <div className="settings-block notification-list">
        {events.map(([k, t, desc]) => (
          <label key={k}>
            <span><strong>{t}</strong><small>{desc}</small></span>
            <span className="channel-checks">
              <input type="checkbox" checked={n.events[k]?.email ?? false} onChange={(e) => upd(k, { email: e.target.checked })} />Email
              <input type="checkbox" checked={n.events[k]?.slack ?? false} onChange={(e) => upd(k, { slack: e.target.checked })} />Slack
            </span>
            <Toggle checked={n.events[k]?.on ?? false} onChange={(v) => upd(k, { on: v })} />
          </label>
        ))}
      </div>
      <div className="settings-block">
        <div className="form-grid two">
          <Field label="Notification email"><input value={n.email} onChange={(e) => set({ ...d, notifications: { ...n, email: e.target.value } })} placeholder="alerts@example.com" /></Field>
          <Field label="Slack channel"><input value={n.slack} onChange={(e) => set({ ...d, notifications: { ...n, slack: e.target.value } })} /></Field>
        </div>
        <p className="helper">Preferences are stored now; delivery to email and Slack is not wired up yet.</p>
      </div>
    </>
  );
}

function AuditLog() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [total, setTotal] = useState(0);
  const [q, setQ] = useState("");
  useEffect(() => {
    const load = () => api.audit(q).then((r) => { setRows(r.rows); setTotal(r.total); }).catch(() => {});
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [q]);
  return (
    <>
      <div className="settings-head"><div><h2>Audit log</h2><p>A durable record of what ARCANE and your team changed.</p></div></div>
      <div className="filter-bar compact"><SearchBox placeholder="Search actions" value={q} onChange={setQ} /></div>
      {rows.length === 0 ? (
        <Empty title="No activity yet" body="Actions appear here as repairs run and your team makes decisions." />
      ) : (
        <div className="data-table audit-table">
          <div className="tr th"><span>Time</span><span>Action</span><span>Scope</span><span>Actor or approver</span></div>
          {rows.map((r, i) => (
            <div className="tr" key={i}>
              <span>{new Date(r.t).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</span>
              <span><strong>{r.action}</strong></span>
              <span>{r.scope}</span>
              <span className="actor"><Avatar name={r.actor} small />{r.actor}</span>
            </div>
          ))}
        </div>
      )}
      <div className="table-footer"><span>Showing {rows.length} of {total} actions</span></div>
    </>
  );
}

// ───────────────────────────────────────────────── palette + toast ───────

function CommandPalette({ close, go }: { close: () => void; go: (s: Screen) => void }) {
  const { runs, settings, select } = useData();
  const [q, setQ] = useState("");
  const ql = q.toLowerCase();
  type Item = { label: string; icon: IconName; run: () => void; badge?: React.ReactNode };
  const groups: [string, Item[]][] = [
    [
      "Runs",
      runs
        .filter((r) => !ql || [r.repo, r.failingTest, r.sha, r.title].join(" ").toLowerCase().includes(ql))
        .slice(0, 6)
        .map((r) => ({
          label: `${shortRepo(r.repo)} · ${r.failingTest || r.title}`,
          icon: "repair" as IconName,
          badge: <Badge tone={badgeTone(r.status)}>{statusLabel[r.status]}</Badge>,
          run: () => { select(r.id); go("repair"); },
        })),
    ],
    [
      "Repositories",
      (settings?.repositories ?? []).filter((r) => !ql || r.name.toLowerCase().includes(ql)).map((r) => ({ label: r.name, icon: "github" as IconName, run: () => go("settings") })),
    ],
    [
      "Go to",
      ([
        ["Home", "home"], ["Repair runs", "repair"], ["Failure log", "logs"], ["Merge conflicts", "conflict"],
        ["Pull requests", "pr"], ["Sent to a person", "people"], ["Past fixes", "history"], ["Settings", "settings"],
      ] as [string, Screen][])
        .filter(([l]) => !ql || l.toLowerCase().includes(ql))
        .map(([l, s]) => ({ label: l, icon: "command" as IconName, run: () => go(s) })),
    ],
  ];
  return (
    <div className="palette-backdrop" onMouseDown={(e) => e.target === e.currentTarget && close()}>
      <div className="palette">
        <div className="palette-search">
          <Icon name="search" />
          <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search ARCANE…" />
          <kbd>esc</kbd>
        </div>
        <div className="palette-results">
          {groups.filter(([, items]) => items.length).map(([g, items]) => (
            <div className="palette-group" key={g}>
              <span>{g}</span>
              {items.map((i) => (
                <button key={g + i.label} onClick={() => { i.run(); close(); }}>
                  <Icon name={i.icon} />
                  <span>{i.label}</span>
                  {i.badge ?? <kbd>↵</kbd>}
                </button>
              ))}
            </div>
          ))}
        </div>
        <div className="palette-foot">
          <span><kbd>↵</kbd> Open</span>
          <span><kbd>esc</kbd> Close</span>
        </div>
      </div>
    </div>
  );
}

function Toast({ text, close }: { text: string; close: () => void }) {
  return (
    <div className="toast">
      <span><Icon name="check" /></span>
      <div><strong>{text}</strong></div>
      <button onClick={close}><Icon name="x" /></button>
    </div>
  );
}

// ───────────────────────────────────────────────────────────── root ──────

function Main() {
  const { authState } = useData();
  if (authState === "loading") return null;
  if (authState === "signedout") return <SignIn />;
  return <Shell />;
}

function Shell() {
  const [screen, setScreen] = useState<Screen>("home");
  const [modal, setModal] = useState<ModalKind>(null);
  const [rejectId, setRejectId] = useState<string | null>(null);
  const [palette, setPalette] = useState(false);
  const { toast, notify } = useData();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPalette((v) => !v);
      }
      if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement).tagName)) {
        e.preventDefault();
        setPalette(true);
      }
      if (e.key === "Escape") {
        setPalette(false);
        setModal(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <>
      <AppShell screen={screen} setScreen={setScreen} onPalette={() => setPalette(true)}>
        {screen === "home" && <Home go={setScreen} openRun={() => setModal("run")} />}
        {screen === "repair" && <RepairRun go={setScreen} retry={() => setModal("retry")} assign={() => setModal("assign")} />}
        {screen === "logs" && <FailureLog go={setScreen} />}
        {screen === "conflict" && <Conflict />}
        {screen === "pr" && <PRPreview />}
        {screen === "people" && <SentToPerson go={setScreen} reject={(id) => { setRejectId(id); setModal("reject"); }} />}
        {screen === "history" && <PastFixes />}
        {screen === "settings" && <Settings />}
        {screen === "components" && <ComponentsPage />}
      </AppShell>
      {modal === "run" && <RunDialog close={() => setModal(null)} started={() => { setModal(null); setScreen("repair"); }} />}
      {modal === "retry" && <RetryDialog close={() => setModal(null)} go={setScreen} />}
      {modal === "assign" && <AssignDialog close={() => setModal(null)} />}
      {modal === "reject" && <RejectDialog runId={rejectId} close={() => setModal(null)} />}
      {palette && <CommandPalette close={() => setPalette(false)} go={setScreen} />}
      {toast && <Toast text={toast} close={() => notify("")} />}
    </>
  );
}

export default function App() {
  return (
    <DataProvider>
      <Main />
    </DataProvider>
  );
}
