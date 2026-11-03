import {
  Archive, Bug, CheckCircle2, ExternalLink, FlaskConical, GitBranch, GitPullRequest, History, Lock, MousePointerClick,
  Plus, Radar, RotateCcw, Settings2, Trash2, Wrench, XCircle,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, operatorName } from "../api";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, StatusBadge, Tabs, Toast, Toggle } from "../components/ui";
import { ago, time } from "../format";
import { usePoll, useToast } from "../hooks";
import DataBackups from "./DataBackups";

const SEV_TONE = { error: "red", warning: "amber" };
const ISSUE_TONE = { open: "amber", fixing: "blue", in_preview: "violet", resolved: "green", ignored: "slate" };
const PREVIEW_TONE = { deploying: "blue", ready: "green", failed: "red", merged: "indigo", rejected: "slate", stopped: "slate", resolving: "blue", conflict: "red" };
const REL_TONE = { live: "green", backup: "cyan", removed: "slate" };

function NeedsKey({ error }) {
  if (!/admin key|DEVNEXA_ADMIN_TOKEN/i.test(error || "")) return <ErrorBox error={error} />;
  return (
    <Card title="Admin key needed" icon={Lock}>
      <p className="text-sm text-slate-300">Unlock DevNexa on the <Link className="text-indigo-300 hover:underline" to="/deploy">Deploy from GitHub</Link> page first, then come back.</p>
    </Card>
  );
}

// ------------------------------------------------------------------ list
export function LiveAppsList() {
  const { data: apps, error } = usePoll("/api/live/apps", 5000);
  return (
    <>
      <PageHeader eyebrow="After deploy" title="Live apps"
        description="Every deployed app with its live version, the issues visitors and the server are hitting right now, and the fixes waiting for you to test." />
      {error ? <NeedsKey error={error} /> : !apps ? <Loading /> : !apps.length ? (
        <Card><Empty title="No apps yet" icon={Radar}>Deploy a repo from <Link className="text-indigo-300" to="/deploy">Deploy from GitHub</Link> and it shows up here.</Empty></Card>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {apps.map((a) => (
            <Link key={a.app} to={`/live/${a.app}`} className="card card-pad block transition hover:border-white/20">
              <div className="flex items-center justify-between gap-2">
                <p className="truncate text-base font-semibold text-white">{a.app}</p>
                {a.live ? <StatusBadge status={a.live.health} /> : <Badge>not running</Badge>}
              </div>
              <p className="mt-1 truncate font-mono text-xs text-indigo-300">{a.live?.url || "—"}</p>
              <div className="mt-3 flex flex-wrap gap-2 text-xs">
                {a.version && <Badge tone="green">{a.version} live</Badge>}
                <Badge tone={a.issues.errors ? "red" : "slate"}>{a.issues.errors} errors</Badge>
                <Badge tone={a.issues.warnings ? "amber" : "slate"}>{a.issues.warnings} warnings</Badge>
                {a.previews > 0 && <Badge tone="violet">{a.previews} to test</Badge>}
              </div>
            </Link>
          ))}
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------- issues
function IssueRow({ issue, app, reload, show }) {
  const [open, setOpen] = useState(false);
  const [hint, setHint] = useState("");
  const [busy, setBusy] = useState(false);
  const f = issue.fix || {};
  const fix = async () => {
    setBusy(true);
    try {
      await api.post(`/api/live/apps/${app}/issues/${issue.id}/fix`, { actor: operatorName(), hint: hint || null });
      show("Fix Agent started: it opens a branch and a test URL when it has a patch", "success");
      reload();
    } catch (e) { show(e.message, "error"); } finally { setBusy(false); }
  };
  const setStatus = async (status) => {
    try { await api.post(`/api/live/apps/${app}/issues/${issue.id}/status`, { status, actor: operatorName() }); reload(); } catch (e) { show(e.message, "error"); }
  };
  return (
    <li className="px-5 py-3">
      <button className="flex w-full items-start gap-3 text-left" onClick={() => setOpen(!open)}>
        <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${issue.severity === "error" ? "bg-rose-400" : "bg-amber-400"}`} />
        <div className="min-w-0 flex-1">
          <p className="break-words text-sm text-slate-100">{issue.message}</p>
          <p className="mt-0.5 text-xs text-slate-500">
            {issue.kind_title}{issue.section || issue.selector ? <> · in <span className="font-mono text-slate-400">{issue.section || issue.selector}</span></> : null}
            {" "}· {issue.count}× · last {ago(issue.last_seen)}{issue.reopened ? ` · came back ${issue.reopened}×` : ""}
          </p>
        </div>
        <Badge tone={ISSUE_TONE[issue.status] || "slate"}>{issue.status.replace("_", " ")}</Badge>
      </button>
      {open && (
        <div className="ml-5 mt-3 space-y-3">
          {issue.stack && <pre className="max-h-48 overflow-auto rounded-lg bg-black/40 p-3 font-mono text-[11.5px] text-rose-200">{issue.stack}</pre>}
          {(issue.sample?.url || issue.sample?.last_click || issue.sample?.text) && (
            <dl className="grid gap-1 text-xs text-slate-400 sm:grid-cols-[110px_1fr]">
              {issue.sample.url && <><dt>URL</dt><dd className="break-all font-mono">{issue.sample.url}</dd></>}
              {issue.sample.last_click && <><dt>After clicking</dt><dd className="break-all font-mono">{issue.sample.last_click}</dd></>}
              {issue.sample.text && <><dt>Section text</dt><dd className="break-words">{issue.sample.text}</dd></>}
              {issue.sample.viewport && <><dt>Screen</dt><dd>{issue.sample.viewport}</dd></>}
            </dl>
          )}
          {f.status && (
            <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-3 text-sm">
              <p className="flex flex-wrap items-center gap-2 text-slate-200"><Wrench className="h-4 w-4" /> Fix Agent: <Badge tone={f.status === "failed" || f.status === "no_patch" ? "amber" : "indigo"}>{f.status.replace("_", " ")}</Badge></p>
              {f.diagnosis && <p className="mt-2 text-slate-300">{f.diagnosis.root_cause} <span className="text-slate-400">{f.diagnosis.suggestion}</span></p>}
              {f.error && <p className="mt-2 text-rose-300">{f.error}</p>}
              {f.pr_url && <a className="mt-2 inline-flex items-center gap-1 text-xs text-indigo-300" href={f.pr_url} target="_blank" rel="noreferrer"><GitPullRequest className="h-3.5 w-3.5" /> Pull request #{f.pr_number}</a>}
              {f.status === "preview" || issue.status === "in_preview" ? <p className="mt-1 text-xs text-slate-400">Try it on the <b>Test URLs</b> tab, then approve to merge.</p> : null}
            </div>
          )}
          {["open", "ignored"].includes(issue.status) && (
            <div className="space-y-2">
              <textarea className="input h-16 text-xs" placeholder="Optional: tell the Fix Agent what you expected here" value={hint} onChange={(e) => setHint(e.target.value)} />
              <div className="flex flex-wrap gap-2">
                <button className="btn-primary py-1.5 text-xs" disabled={busy} onClick={fix}><Wrench className="h-3.5 w-3.5" /> {busy ? "Starting…" : "Fix it on a test URL"}</button>
                {issue.status === "open" ? <button className="btn-ghost py-1.5 text-xs" onClick={() => setStatus("ignored")}>Ignore</button>
                  : <button className="btn-ghost py-1.5 text-xs" onClick={() => setStatus("open")}>Reopen</button>}
                <button className="btn-ghost py-1.5 text-xs" onClick={() => setStatus("resolved")}><CheckCircle2 className="h-3.5 w-3.5" /> Mark fixed</button>
              </div>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

function Issues({ app, ov, show }) {
  const [filter, setFilter] = useState("active");
  const { data: list, reload } = usePoll(`/api/live/apps/${app}/issues?status=${filter}`, 4000, [filter]);
  const [scanning, setScanning] = useState(false);
  const pages = useMemo(() => {
    const m = new Map();
    (list || []).forEach((i) => { if (!m.has(i.page)) m.set(i.page, []); m.get(i.page).push(i); });
    return [...m.entries()];
  }, [list]);
  const scan = async () => {
    setScanning(true);
    try { const r = await api.post(`/api/live/apps/${app}/scan`); show(`Scanned ${r.pages} pages`, "success"); reload(); } catch (e) { show(e.message, "error"); } finally { setScanning(false); }
  };
  const liveUrl = ov.live?.url;
  return (
    <div className="space-y-4">
      <Card bodyClass="p-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <Tabs value={filter} onChange={setFilter} tabs={[{ value: "active", label: "Needs attention" }, { value: "resolved", label: "Fixed" }, { value: "ignored", label: "Ignored" }, { value: "all", label: "All" }]} />
          <div className="flex flex-wrap gap-2">
            {liveUrl && <a className="btn-ghost py-1.5 text-xs" href={`${liveUrl}/?devnexa-report=1`} target="_blank" rel="noreferrer"><MousePointerClick className="h-3.5 w-3.5" /> Point at a problem</a>}
            <button className="btn-ghost py-1.5 text-xs" onClick={scan} disabled={scanning || !ov.live}><Radar className="h-3.5 w-3.5" /> {scanning ? "Scanning…" : "Scan pages"}</button>
          </div>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          Issues come from visitors' browsers (errors, failed API calls, blank pages and empty sections), the app's server logs and page scans.
          {" "}<b className="text-slate-400">Point at a problem</b> opens your site in report mode: click the broken section and describe it.
          {!ov.settings.steps.issue_capture && " Browser capture is off in Pipeline settings."}
        </p>
      </Card>
      {!list ? <Loading /> : !pages.length ? (
        <Card><Empty title={filter === "active" ? "No open issues" : "Nothing here"} icon={CheckCircle2}>The app looks clean. New problems show up here within seconds.</Empty></Card>
      ) : pages.map(([page, items]) => (
        <Card key={page} bodyClass="" title={<span className="font-mono">{page}</span>}
          subtitle={`${items.length} issue${items.length > 1 ? "s" : ""} on this page`}>
          <ul className="divide-y divide-white/[0.04]">
            {items.map((i) => <IssueRow key={i.id} issue={i} app={app} reload={reload} show={show} />)}
          </ul>
        </Card>
      ))}
    </div>
  );
}

// ------------------------------------------------------------- test URLs
function Previews({ app, show }) {
  const { data: list, reload } = usePoll(`/api/live/apps/${app}/previews`, 3000);
  const [branch, setBranch] = useState("");
  const [busy, setBusy] = useState(false);
  const create = async (e) => {
    e.preventDefault();
    setBusy(true);
    try { await api.post(`/api/live/apps/${app}/previews`, { branch: branch.trim(), actor: operatorName() }); setBranch(""); show("Deploying the branch to a test URL", "success"); reload(); } catch (err) { show(err.message, "error"); } finally { setBusy(false); }
  };
  const decide = async (p, verdict) => {
    let reason = "";
    if (verdict === "approve" && !window.confirm(`Merge ${p.branch} into ${p.base_branch} and redeploy ${app}?`)) return;
    if (verdict === "reject") { reason = window.prompt("Why? (optional)") ?? null; if (reason === null) return; }
    try {
      const r = await api.post(`/api/live/previews/${p.id}/${verdict}`, { actor: operatorName(), reason });
      show(verdict !== "approve" ? "Rejected and removed"
        : r.status === "resolving_conflicts" ? "The branch conflicts with the base. The Merge Agent is resolving it, re-testing the result, and will merge and roll out once it is healthy"
          : "Merged. Rolling out the new version with zero downtime", "success");
      reload();
    } catch (e) { show(e.message, "error"); }
  };
  return (
    <div className="space-y-4">
      <Card title="Test a branch" icon={GitBranch} subtitle="Deploy any branch to its own URL, e.g. a feature a developer pushed. Approving merges it.">
        <form onSubmit={create} className="flex flex-col gap-2 sm:flex-row">
          <input className="input font-mono" placeholder="branch name, e.g. feature/new-cart" value={branch} onChange={(e) => setBranch(e.target.value)} />
          <button className="btn-primary shrink-0 whitespace-nowrap" disabled={!branch.trim() || busy}><FlaskConical className="h-4 w-4" /> {busy ? "Starting…" : "Create test URL"}</button>
        </form>
      </Card>
      {!list ? <Loading /> : !list.length ? (
        <Card><Empty title="No test URLs" icon={FlaskConical}>When the Fix Agent fixes an issue, its branch is deployed here so you can try it before it goes live.</Empty></Card>
      ) : (
        <Card bodyClass="">
          <ul className="divide-y divide-white/[0.04]">
            {list.map((p) => (
              <li key={p.id} className="space-y-2 px-5 py-4">
                <div className="flex flex-wrap items-center gap-2">
                  <p className="text-sm font-medium text-slate-100">{p.title || p.branch}</p>
                  <Badge tone={PREVIEW_TONE[p.status] || "slate"} dot>{p.status}</Badge>
                </div>
                <p className="text-xs text-slate-500"><GitBranch className="mr-1 inline h-3 w-3" />{p.branch} → {p.base_branch} · by {p.created_by} · {ago(p.created_at)}{p.issue_message ? ` · fixes "${p.issue_message}"` : ""}</p>
                <div className="flex flex-wrap items-center gap-3 text-xs">
                  {p.url && <a className="inline-flex items-center gap-1 font-mono text-indigo-300 hover:underline" href={p.url} target="_blank" rel="noreferrer">{p.url} <ExternalLink className="h-3 w-3" /></a>}
                  {p.pr_url && <a className="inline-flex items-center gap-1 text-slate-300 hover:underline" href={p.pr_url} target="_blank" rel="noreferrer"><GitPullRequest className="h-3.5 w-3.5" /> PR #{p.pr_number}</a>}
                  {p.deployment && <Link className="text-slate-400 hover:underline" to="/deploy">logs: deploy #{p.deployment.id}</Link>}
                </div>
                {p.deployment?.error && <p className="text-xs text-rose-300">{p.deployment.error}</p>}
                {p.status === "resolving" && <p className="text-xs text-sky-300">Merge Agent: merging {p.base_branch} into {p.branch} and resolving conflicts…</p>}
                {["ready", "deploying", "failed", "conflict"].includes(p.status) && (
                  <div className="flex gap-2 pt-1">
                    <button className="btn-success py-1.5 text-xs" disabled={!["ready", "conflict"].includes(p.status)} onClick={() => decide(p, "approve")}><CheckCircle2 className="h-3.5 w-3.5" /> {p.status === "conflict" ? "Retry merge" : "Approve and merge"}</button>
                    <button className="btn-ghost py-1.5 text-xs" onClick={() => decide(p, "reject")}><XCircle className="h-3.5 w-3.5" /> Reject</button>
                  </div>
                )}
                {p.note && <p className={`text-xs ${p.status === "conflict" ? "text-rose-300" : "text-slate-500"}`}>Note: {p.note}</p>}
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

// ----------------------------------------------------- versions / backups
function Versions({ app, ov, reloadOv, show }) {
  const { data: rels, reload } = usePoll(`/api/live/apps/${app}/releases`, 5000);
  const [every, setEvery] = useState(24);
  const [keep, setKeep] = useState(7);
  const act = async (fn, msg) => { try { await fn(); show(msg, "success"); reload(); reloadOv(); } catch (e) { show(e.message, "error"); } };
  const schedules = ov.settings.backup_schedules;
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
      <Card title="Versions" icon={History} bodyClass=""
        subtitle={`Each deploy of ${ov.branch} becomes the next version. ${ov.settings.steps.auto_backup ? `The last ${ov.settings.keep_versions} are kept as backups.` : "Backups are off, so old versions are deleted."}`}
        action={<button className="btn-ghost py-1.5 text-xs" disabled={!ov.live} onClick={() => act(() => api.post(`/api/live/apps/${app}/backups`, { actor: operatorName() }), "Backup taken")}><Archive className="h-3.5 w-3.5" /> Back up now</button>}>
        {!rels ? <Loading /> : !rels.length ? <Empty title="No versions yet" icon={History}>The first successful deploy becomes v1.</Empty> : (
          <ul className="divide-y divide-white/[0.04]">
            {rels.map((r) => (
              <li key={r.id} className="flex flex-col gap-2 px-5 py-3 sm:flex-row sm:items-center">
                <div className="min-w-0 flex-1">
                  <p className="flex flex-wrap items-center gap-2 text-sm text-slate-100"><b>{r.label}</b> <Badge tone={REL_TONE[r.status]}>{r.status}</Badge> {r.kind === "snapshot" && <Badge tone="violet">snapshot</Badge>}</p>
                  <p className="truncate text-xs text-slate-500">{r.commit_sha?.slice(0, 7)} {r.commit_message || ""} · {r.source.replace("-", " ")} · {time(r.created_at)}</p>
                </div>
                {r.status === "backup" && (
                  <button className="btn-ghost py-1.5 text-xs" onClick={() => window.confirm(`Put ${r.label} live on ${app}?`) && act(() => api.post(`/api/live/apps/${app}/releases/${r.id}/rollback`, { actor: operatorName() }), `Rolling back to ${r.label}`)}>
                    <RotateCcw className="h-3.5 w-3.5" /> Roll back
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Container snapshots" icon={Archive} subtitle="Image snapshots of the running container on a timer. For files and databases use Data & backups.">
        <ul className="space-y-2">
          {schedules.map((s) => (
            <li key={s.id} className="flex items-center gap-2 rounded-xl bg-white/[0.03] px-3 py-2 text-sm">
              <span className="flex-1 text-slate-200">Every {s.every_hours}h, keep {s.keep}<span className="block text-xs text-slate-500">last run {s.last_run ? ago(s.last_run) : "not yet"}</span></span>
              <button className="btn-ghost px-2 py-1 text-xs" title="Remove schedule" onClick={() => window.confirm("Remove this backup schedule? Existing backups stay.") && act(() => api.del(`/api/live/apps/${app}/backup-schedules/${s.id}?actor=${encodeURIComponent(operatorName())}`), "Schedule removed")}><Trash2 className="h-3.5 w-3.5" /></button>
            </li>
          ))}
          {!schedules.length && <li className="text-sm text-slate-500">No schedules.</li>}
        </ul>
        <div className="mt-4 grid grid-cols-[1fr_1fr_auto] items-end gap-2">
          <label><span className="label">Every (hours)</span><input className="input" type="number" min="1" value={every} onChange={(e) => setEvery(e.target.value)} /></label>
          <label><span className="label">Keep</span><input className="input" type="number" min="1" max="30" value={keep} onChange={(e) => setKeep(e.target.value)} /></label>
          <button className="btn-primary" onClick={() => act(() => api.post(`/api/live/apps/${app}/backup-schedules`, { every_hours: Number(every), keep: Number(keep), actor: operatorName() }), "Schedule added")}><Plus className="h-4 w-4" /></button>
        </div>
      </Card>
    </div>
  );
}

// ------------------------------------------------------ pipeline settings
function Pipeline({ app, ov, reloadOv, show }) {
  const s = ov.settings;
  const save = async (body, msg) => {
    try { await api.put(`/api/live/apps/${app}/settings`, { ...body, actor: operatorName() }); show(msg, "success"); reloadOv(); } catch (e) { show(e.message, "error"); }
  };
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
      <Card title="Deploy pipeline" icon={Settings2} subtitle="What happens on every deploy of this app, in order. Switch off the optional steps you don't want.">
        <ol className="space-y-2">
          {ov.steps.map((st, i) => (
            <li key={st.key} className="flex items-center gap-3 rounded-xl bg-white/[0.03] px-3 py-2.5">
              <span className="w-5 text-right text-xs text-slate-500">{i + 1}</span>
              <span className={`flex-1 text-sm ${st.optional && !s.steps[st.key] ? "text-slate-500 line-through" : "text-slate-200"}`}>{st.title}</span>
              {st.optional ? <Toggle label={st.title} checked={!!s.steps[st.key]} onChange={(v) => save({ steps: { [st.key]: v } }, `${st.title}: ${v ? "on" : "off"}`)} />
                : <span className="text-[11px] text-slate-500">always</span>}
            </li>
          ))}
        </ol>
        <p className="mt-3 text-xs text-slate-500">Auto-fix only prepares a branch, a pull request and a test URL. Nothing reaches the live URL until you approve it.</p>
      </Card>
      <div className="space-y-4">
        <Card title="Backups" icon={History}>
          <label><span className="label">Versions to keep</span>
            <select className="input" value={s.keep_versions} disabled={!s.steps.auto_backup} onChange={(e) => save({ keep_versions: Number(e.target.value) }, "Saved")}>
              {[1, 2, 3, 5, 10, 20].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
        </Card>
        <Card title="Load balancer" icon={Radar}>
          <label><span className="label">Containers</span>
            <select className="input" value={s.replicas} disabled={!s.steps.load_balancer} onChange={(e) => save({ replicas: Number(e.target.value) }, "Saved")}>
              {[2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <ul className="mt-3 space-y-1 font-mono text-xs text-slate-400">
            {(ov.load_balancer.upstreams.length ? ov.load_balancer.upstreams : ov.live ? [`${ov.live.container}:${ov.live.port}`] : []).map((u) => <li key={u}>● {u}</li>)}
          </ul>
        </Card>
        <Card title="Issue capture script" icon={Bug}>
          <p className="text-xs text-slate-400">Added to static index.html files automatically. For server-rendered apps (Next.js, Django, …) add this to your layout:</p>
          <pre className="mt-2 overflow-x-auto rounded-lg bg-black/40 p-2 font-mono text-[11px] text-slate-300">{ov.agent_script}</pre>
        </Card>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- detail
export default function LiveApp() {
  const { app } = useParams();
  const { data: ov, error, reload } = usePoll(`/api/live/apps/${app}`, 5000, [app]);
  const [tab, setTab] = useState("issues");
  const [toast, show] = useToast();
  if (error) return <NeedsKey error={error} />;
  if (!ov) return <Loading />;
  return (
    <>
      <PageHeader eyebrow={<Link to="/live" className="hover:underline">Live apps</Link>} title={app}
        description={<>
          {ov.repo} · {ov.branch}
          {ov.live?.url && <> · <a className="font-mono text-indigo-300 hover:underline" href={ov.live.url} target="_blank" rel="noreferrer">{ov.live.url}</a></>}
        </>}
        actions={<div className="flex items-center gap-2">{ov.live ? <StatusBadge status={ov.live.health} /> : <Badge>not running</Badge>}</div>} />
      <div className="mb-4">
        <Tabs value={tab} onChange={setTab} tabs={[
          { value: "issues", label: "Issues", count: ov.issues.open },
          { value: "previews", label: "Test URLs" },
          { value: "versions", label: "Versions" },
          { value: "data", label: "Data & backups" },
          { value: "pipeline", label: "Pipeline settings" },
        ]} />
      </div>
      {tab === "issues" && <Issues app={app} ov={ov} show={show} />}
      {tab === "previews" && <Previews app={app} show={show} />}
      {tab === "versions" && <Versions app={app} ov={ov} reloadOv={reload} show={show} />}
      {tab === "data" && <DataBackups app={app} show={show} />}
      {tab === "pipeline" && <Pipeline app={app} ov={ov} reloadOv={reload} show={show} />}
      <Toast toast={toast} />
    </>
  );
}
