import {
  Bot, CheckCircle2, Cpu, ExternalLink, GitBranch, Github, KeyRound, Lock, LogOut, MemoryStick, RefreshCw, Rocket,
  Search, Square, Terminal, Timer, XCircle,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ADMIN_KEY, adminKey, api, operatorName, streamUrl } from "../api";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, RiskBadge, StatusBadge, Tabs, Toast, chartTooltip } from "../components/ui";
import { ago, clock } from "../format";
import { usePoll, useToast } from "../hooks";

const STEPS = ["queued", "cloning", "building", "starting", "running"];
const LIVE = new Set(["queued", "cloning", "building", "starting"]);

function Unlock({ onDone }) {
  const [key, setKey] = useState("");
  const save = (e) => {
    e.preventDefault();
    try { localStorage.setItem(ADMIN_KEY, key.trim()); } catch { /* storage blocked */ }
    onDone();
  };
  return (
    <Card title="Enter the admin key" icon={Lock}
      subtitle="Deploying runs code on your server, so this page needs the DEVNEXA_ADMIN_TOKEN from the server's .env.">
      <form onSubmit={save} className="flex flex-col gap-2 sm:flex-row">
        <input className="input" type="password" placeholder="Admin key" value={key} onChange={(e) => setKey(e.target.value)} />
        <button className="btn-primary" disabled={!key.trim()}>Unlock</button>
      </form>
    </Card>
  );
}

function GitHubCard({ conn, reload, show }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const connect = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const c = await api.post("/api/github/connect", { token });
      setToken("");
      show(`Connected as ${c.login}`, "success");
      reload();
    } catch (err) { show(err.message, "error"); } finally { setBusy(false); }
  };
  const disconnect = async () => {
    if (!window.confirm("Disconnect GitHub? Running apps keep running.")) return;
    await api.del("/api/github");
    reload();
  };
  if (conn?.connected) {
    return (
      <Card title="GitHub" icon={Github}
        action={conn.source !== "env" && <button className="btn-ghost py-1.5 text-xs" onClick={disconnect}><LogOut className="h-3.5 w-3.5" /> Disconnect</button>}>
        <div className="flex items-center gap-3">
          {conn.avatar_url && <img src={conn.avatar_url} alt="" className="h-9 w-9 rounded-full" />}
          <div>
            <p className="text-sm text-slate-100">Connected{conn.login ? <> as <b>@{conn.login}</b></> : " (token from server .env)"}</p>
            <p className="font-mono text-xs text-slate-500">token {conn.token_hint}{conn.scopes ? ` · scopes: ${conn.scopes}` : ""}</p>
          </div>
        </div>
      </Card>
    );
  }
  return (
    <Card title="Connect GitHub" icon={Github} subtitle="Paste a personal access token so DevNexa can list and clone your repos.">
      <form onSubmit={connect} className="space-y-3">
        <input className="input font-mono" type="password" placeholder="ghp_… or github_pat_…" value={token} onChange={(e) => setToken(e.target.value)} />
        <div className="flex flex-wrap items-center gap-3">
          <button className="btn-primary" disabled={!token.trim() || busy}><KeyRound className="h-4 w-4" /> {busy ? "Checking…" : "Connect"}</button>
          <a className="text-xs text-indigo-300 hover:underline" target="_blank" rel="noreferrer"
            href="https://github.com/settings/tokens/new?scopes=repo&description=DevNexa">Create a token (needs the repo scope)</a>
        </div>
      </form>
    </Card>
  );
}

function RepoPicker({ onDeployed, show }) {
  const { data: repos, error } = usePoll("/api/github/repos", 0);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(null);
  const [branches, setBranches] = useState([]);
  const [form, setForm] = useState({});
  const [busy, setBusy] = useState(false);
  const list = useMemo(() => (repos || []).filter((r) => r.full_name.toLowerCase().includes(q.toLowerCase())), [repos, q]);

  const pick = async (r) => {
    setSel(r);
    setForm({ branch: r.default_branch, app: r.name.toLowerCase().replace(/[^a-z0-9]+/g, "-"), port: "", root_dir: "", env: "" });
    setBranches([r.default_branch]);
    try { setBranches(await api.get(`/api/github/repos/${r.full_name}/branches`)); } catch { /* keep default */ }
  };
  const deploy = async () => {
    setBusy(true);
    const env = Object.fromEntries(form.env.split("\n").map((l) => l.trim()).filter((l) => l.includes("="))
      .map((l) => [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1).trim()]));
    try {
      const d = await api.post("/api/apps/deploy", {
        repo: sel.full_name, branch: form.branch, app: form.app || null, root_dir: form.root_dir,
        port: form.port ? Number(form.port) : null, env, actor: operatorName(),
      });
      show(`Deploying ${sel.name}…`, "success");
      setSel(null);
      onDeployed(d.id);
    } catch (e) { show(e.message, "error"); } finally { setBusy(false); }
  };
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  return (
    <Card title="Deploy a repository" icon={Rocket} bodyClass="">
      <ErrorBox error={error} />
      {!sel ? (
        <>
          <div className="relative border-b border-white/[0.06] px-5 py-3">
            <Search className="absolute left-8 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
            <input className="input pl-9" placeholder="Search your repositories" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          {!repos ? !error && <Loading label="Loading repositories" /> : (
            <ul className="max-h-[420px] divide-y divide-white/[0.04] overflow-y-auto">
              {list.map((r) => (
                <li key={r.full_name}>
                  <button onClick={() => pick(r)} className="flex w-full items-center gap-3 px-5 py-3 text-left hover:bg-white/[0.03]">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm text-slate-100">{r.full_name} {r.private && <Badge>private</Badge>}</p>
                      <p className="truncate text-xs text-slate-500">{r.language || "—"} · updated {ago(r.updated_at)}{r.description ? ` · ${r.description}` : ""}</p>
                    </div>
                    <span className="text-xs text-indigo-300">Select</span>
                  </button>
                </li>
              ))}
              {!list.length && <li className="px-5 py-6 text-sm text-slate-500">No repositories match.</li>}
            </ul>
          )}
        </>
      ) : (
        <div className="space-y-4 p-5">
          <p className="text-sm text-slate-200"><Github className="mr-1 inline h-4 w-4" /> {sel.full_name}</p>
          <div className="grid gap-3 sm:grid-cols-2">
            <label><span className="label">Branch</span>
              <select className="input" value={form.branch} onChange={set("branch")}>
                {branches.map((b) => <option key={b}>{b}</option>)}
              </select>
            </label>
            <label><span className="label">App name (URL)</span>
              <input className="input font-mono" value={form.app} onChange={set("app")} />
            </label>
            <label><span className="label">Port (optional)</span>
              <input className="input" placeholder="auto-detect" value={form.port} onChange={set("port")} inputMode="numeric" />
            </label>
            <label><span className="label">Folder (optional)</span>
              <input className="input font-mono" placeholder="repo root" value={form.root_dir} onChange={set("root_dir")} />
            </label>
          </div>
          <label className="block"><span className="label">Environment variables (KEY=value per line)</span>
            <textarea className="input h-20 font-mono" value={form.env} onChange={set("env")} />
          </label>
          <div className="flex gap-2">
            <button className="btn-primary" onClick={deploy} disabled={busy}><Rocket className="h-4 w-4" /> {busy ? "Starting…" : "Deploy"}</button>
            <button className="btn-ghost" onClick={() => setSel(null)}>Back</button>
          </div>
        </div>
      )}
    </Card>
  );
}

function AppsList({ apps, selected, onSelect }) {
  if (!apps) return <Loading />;
  if (!apps.length) return <Empty title="Nothing deployed yet" icon={Rocket}>Pick a repository to get a live URL.</Empty>;
  return (
    <ul className="divide-y divide-white/[0.04]">
      {apps.map((a) => {
        const d = a.latest;
        const live = a.live;
        const m = live?.metrics?.last;
        return (
          <li key={a.app}>
            <button onClick={() => onSelect(d.id)}
              className={`flex w-full flex-col gap-2 px-5 py-3 text-left transition hover:bg-white/[0.03] sm:flex-row sm:items-center ${selected === d.id ? "bg-white/[0.04]" : ""}`}>
              <div className="min-w-0 flex-1">
                <p className="flex items-center gap-2 text-sm font-medium text-slate-100">{a.app} <StatusBadge status={d.status} /></p>
                <p className="truncate text-xs text-slate-500">{a.repo} · {d.branch} · {ago(d.created_at)}</p>
                {live?.url && <p className="truncate font-mono text-xs text-indigo-300">{live.url}</p>}
              </div>
              <div className="flex items-center gap-3 text-xs text-slate-400">
                {m?.latency_ms != null && <span className="flex items-center gap-1"><Timer className="h-3 w-3" />{m.latency_ms} ms</span>}
                {m?.mem_mb != null && <span className="flex items-center gap-1"><MemoryStick className="h-3 w-3" />{m.mem_mb} MB</span>}
                {live && <StatusBadge status={live.health} />}
                {d.pending_approvals > 0 && <Badge tone="amber">needs approval</Badge>}
              </div>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function LogView({ dep }) {
  const [source, setSource] = useState("build");
  const [lines, setLines] = useState([]);
  const [state, setState] = useState("");
  const box = useRef(null);
  const follow = useRef(true);
  const running = dep.status === "running";

  useEffect(() => { setSource(LIVE.has(dep.status) || !running ? "build" : "runtime"); }, [dep.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setLines([]);
    setState("connecting");
    const es = new EventSource(streamUrl(`/api/apps/deployments/${dep.id}/logs/stream?source=${source}`));
    const buf = [];
    let t = null;
    const flush = () => { t = null; const items = buf.splice(0); setLines((l) => l.concat(items).slice(-3000)); };
    es.addEventListener("line", (e) => { buf.push(JSON.parse(e.data)); if (!t) t = setTimeout(flush, 100); });
    es.addEventListener("status", (e) => setState(JSON.parse(e.data).status));
    es.addEventListener("end", (e) => { setState(`ended (${JSON.parse(e.data).status})`); es.close(); });
    es.onopen = () => setState("live");
    es.onerror = () => { setState("disconnected"); es.close(); };
    return () => { es.close(); if (t) clearTimeout(t); };
  }, [dep.id, source]);

  useEffect(() => {
    if (follow.current && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines]);

  const color = (l) => (/^!!|error|failed|traceback|exception/i.test(l) ? "text-rose-300"
    : l.startsWith("==>") ? "text-indigo-300 font-semibold" : l.startsWith("[agent]") ? "text-amber-300"
      : l.startsWith("[health]") || l.startsWith("[url]") ? "text-emerald-300" : "text-slate-300");
  return (
    <Card title="Logs" icon={Terminal} bodyClass=""
      action={<div className="flex items-center gap-2"><span className="text-[11px] text-slate-500">{state}</span>
        <Tabs value={source} onChange={setSource} tabs={[{ value: "build", label: "Build" }, ...(running ? [{ value: "runtime", label: "App (live)" }] : [])]} /></div>}>
      <div ref={box} onScroll={(e) => { const el = e.currentTarget; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40; }}
        className="h-[420px] overflow-auto bg-black/40 px-4 py-3 font-mono text-[12px] leading-5">
        {lines.length ? lines.map((l, i) => <div key={i} className={`whitespace-pre-wrap break-all ${color(l)}`}>{l}</div>)
          : <p className="text-slate-500">Waiting for log lines…</p>}
      </div>
    </Card>
  );
}

function AgentCard({ dep, reload, show }) {
  const d = dep.diagnosis;
  const decide = async (i, verdict) => {
    try {
      const r = await api.post(`/api/apps/deployments/${dep.id}/remediations/${i}/${verdict}`, { actor: operatorName() });
      show(r.pr_url ? "Pull request opened" : verdict === "approve" ? "Fix started" : "Rejected", "success");
      reload();
    } catch (e) { show(e.message, "error"); }
  };
  if (!d && !dep.remediations?.length) return null;
  return (
    <Card title="Agent diagnosis and fixes" icon={Bot}
      subtitle={d ? `${d.stage} · confidence ${Math.round(d.confidence * 100)}% · ${d.provider}` : undefined}>
      {d && (
        <div className="space-y-3">
          <p className="font-medium text-slate-100">{d.title}</p>
          <p className="text-sm text-slate-300">{d.root_cause}</p>
          <p className="text-sm text-slate-400"><b className="text-slate-300">Fix:</b> {d.suggestion}</p>
          {d.evidence?.length > 0 && (
            <pre className="max-h-40 overflow-auto rounded-lg bg-black/40 p-3 font-mono text-[11.5px] text-rose-200">{d.evidence.join("\n")}</pre>
          )}
          {d.patch && Object.entries(d.patch).map(([path, content]) => (
            <details key={path} className="rounded-lg border border-white/10">
              <summary className="cursor-pointer px-3 py-2 font-mono text-xs text-slate-300">Proposed change: {path}</summary>
              <pre className="max-h-60 overflow-auto bg-black/40 p-3 font-mono text-[11.5px] text-slate-300">{content}</pre>
            </details>
          ))}
          {d.similar?.length > 0 && (
            <p className="text-xs text-slate-500">Seen before: {d.similar.map((s) => `${s.title} → ${s.action} (${s.outcome}, ${Math.round(s.similarity * 100)}%)`).join("; ")}</p>
          )}
        </div>
      )}
      <ul className="mt-4 space-y-2">
        {(dep.remediations || []).map((r) => (
          <li key={r.id} className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm text-slate-100">{r.title}</span>
              <RiskBadge risk={r.risk} />
              <StatusBadge status={r.status} />
            </div>
            <p className="mt-1 text-xs text-slate-400">{r.reason}{r.detail ? ` ${r.detail}` : ""}</p>
            {r.pr_url && <a className="mt-1 inline-flex items-center gap-1 text-xs text-indigo-300" href={r.pr_url} target="_blank" rel="noreferrer">Open pull request <ExternalLink className="h-3 w-3" /></a>}
            {r.status === "pending_approval" && (
              <div className="mt-2 flex gap-2">
                <button className="btn-success py-1.5 text-xs" onClick={() => decide(r.id, "approve")}><CheckCircle2 className="h-3.5 w-3.5" /> Approve</button>
                <button className="btn-ghost py-1.5 text-xs" onClick={() => decide(r.id, "reject")}><XCircle className="h-3.5 w-3.5" /> Reject</button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}

function Detail({ id, onSelect, show }) {
  const { data: dep, reload } = usePoll(`/api/apps/deployments/${id}`, 2000, [id]);
  const { data: history } = usePoll(dep ? `/api/apps/deployments?app=${dep.app}&limit=10` : null, 5000, [dep?.app]);
  if (!dep) return <Loading />;
  const step = STEPS.indexOf(dep.status);
  const hist = (dep.metrics?.history || []).map((h) => ({ t: clock(h.t), latency: h.latency_ms, mem: h.mem_mb, cpu: h.cpu_pct }));
  const last = dep.metrics?.last;
  const act = async (path, msg) => {
    try { const r = await api.post(`/api/apps/deployments/${dep.id}/${path}?actor=${encodeURIComponent(operatorName())}`); show(msg, "success"); if (r.id !== dep.id) onSelect(r.id); else reload(); } catch (e) { show(e.message, "error"); }
  };
  return (
    <div className="space-y-4">
      <Card bodyClass="p-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <p className="flex flex-wrap items-center gap-2 text-lg font-semibold text-white">{dep.app} <StatusBadge status={dep.status} /> {dep.status === "running" && <StatusBadge status={dep.health} />}</p>
            <p className="text-xs text-slate-500"><GitBranch className="mr-1 inline h-3 w-3" />{dep.repo} · {dep.branch} · {dep.commit_sha?.slice(0, 7) || "…"} {dep.commit_message ? `· ${dep.commit_message}` : ""}</p>
            {dep.url && dep.status === "running" && (
              <a href={dep.url} target="_blank" rel="noreferrer" className="mt-2 inline-flex items-center gap-1.5 font-mono text-sm text-indigo-300 hover:underline">{dep.url} <ExternalLink className="h-3.5 w-3.5" /></a>
            )}
            {dep.error && <p className="mt-2 text-sm text-rose-300">{dep.error}</p>}
          </div>
          <div className="flex gap-2">
            <button className="btn-ghost py-1.5 text-xs" onClick={() => act("redeploy", "Redeploying")} disabled={LIVE.has(dep.status)}><RefreshCw className="h-3.5 w-3.5" /> Redeploy</button>
            {dep.status === "running" && <button className="btn-danger py-1.5 text-xs" onClick={() => window.confirm(`Stop ${dep.app}?`) && act("stop", "Stopped")}><Square className="h-3.5 w-3.5" /> Stop</button>}
          </div>
        </div>
        <ol className="mt-4 grid grid-cols-5 gap-1">
          {STEPS.map((s, i) => {
            const failed = dep.status === "failed" && i === Math.max(1, STEPS.indexOf(dep.diagnosis?.stage === "clone" ? "cloning" : dep.diagnosis?.stage === "build" ? "building" : "starting"));
            const done = step > i || (dep.status !== "failed" && step === i && s === "running") || ["replaced", "stopped"].includes(dep.status);
            return (
              <li key={s} className="text-center">
                <div className={`h-1.5 rounded-full ${failed ? "bg-rose-400" : done ? "bg-emerald-400" : step === i ? "animate-pulse bg-sky-400" : "bg-white/10"}`} />
                <p className="mt-1 text-[11px] capitalize text-slate-500">{s}</p>
              </li>
            );
          })}
        </ol>
        {dep.status === "failed" && <p className="mt-3 text-xs text-slate-500">If an earlier version was live, it keeps serving the URL until a new deploy succeeds.</p>}
      </Card>

      {dep.status === "running" && (
        <Card title="Monitoring" icon={Cpu} subtitle="Checked every few seconds by the Monitoring Agent: HTTP probe, CPU, memory and error lines in the logs.">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[["Response", last?.status ?? "—"], ["Latency", last?.latency_ms != null ? `${last.latency_ms} ms` : "—"],
              ["CPU", last?.cpu_pct != null ? `${last.cpu_pct}%` : "—"], ["Memory", last?.mem_mb != null ? `${last.mem_mb} MB` : "—"]].map(([k, v]) => (
              <div key={k} className="rounded-xl bg-white/[0.03] p-3"><p className="text-[11px] text-slate-500">{k}</p><p className="text-lg font-semibold text-white">{v}</p></div>
            ))}
          </div>
          {hist.length > 1 && (
            <div className="mt-4 h-40">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={hist}>
                  <XAxis dataKey="t" hide /><YAxis width={36} />
                  <Tooltip {...chartTooltip} />
                  <Area type="monotone" dataKey="latency" name="Latency ms" stroke="#818cf8" fill="#818cf833" />
                  <Area type="monotone" dataKey="mem" name="Memory MB" stroke="#22d3ee" fill="#22d3ee22" />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          )}
          {dep.metrics?.recent_errors?.length > 0 && (
            <pre className="mt-3 max-h-32 overflow-auto rounded-lg bg-black/40 p-3 font-mono text-[11.5px] text-rose-200">{dep.metrics.recent_errors.join("\n")}</pre>
          )}
        </Card>
      )}

      <AgentCard dep={dep} reload={reload} show={show} />
      <LogView dep={dep} />

      {history?.length > 1 && (
        <Card title="Deploy history" bodyClass="">
          <ul className="divide-y divide-white/[0.04]">
            {history.map((h) => (
              <li key={h.id}>
                <button onClick={() => onSelect(h.id)} className={`flex w-full items-center gap-3 px-5 py-2.5 text-left text-sm hover:bg-white/[0.03] ${h.id === dep.id ? "bg-white/[0.04]" : ""}`}>
                  <span className="font-mono text-slate-400">#{h.id}</span>
                  <span className="flex-1 truncate text-slate-300">{h.commit_message || h.branch}{h.parent_id ? ` (agent fix of #${h.parent_id})` : ""}</span>
                  <StatusBadge status={h.status} />
                  <span className="text-xs text-slate-500">{ago(h.created_at)}</span>
                </button>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

export default function Deploy() {
  const [, force] = useState(0);
  const { data: cfg } = usePoll("/api/apps/config", 0);
  const unlocked = !cfg?.admin_required || adminKey();
  const { data: conn, error: connErr, reload: reloadConn } = usePoll(unlocked ? "/api/github" : null, 0);
  const { data: apps } = usePoll(unlocked && conn?.connected ? "/api/apps" : null, 4000);
  const [selected, setSelected] = useState(null);
  const [toast, show] = useToast();

  useEffect(() => {
    if (!selected && apps?.length) setSelected(apps[0].latest.id);
  }, [apps, selected]);

  const lock = () => { try { localStorage.removeItem(ADMIN_KEY); } catch { /* ignore */ } force((n) => n + 1); };

  return (
    <>
      <PageHeader eyebrow="Deploy Agent" title="Deploy from GitHub"
        description="Pick a repo, get a live URL. DevNexa builds it with Docker, streams the logs, watches it, and its agents diagnose and fix failures: safe fixes run automatically, risky ones wait for your approval."
        actions={adminKey() && <button className="btn-ghost py-1.5 text-xs" onClick={lock}><Lock className="h-3.5 w-3.5" /> Lock</button>} />
      {cfg && !cfg.docker_available && <ErrorBox error="Docker is not available to the backend here, so deploys will fail. On the server, the backend container needs /var/run/docker.sock mounted (docker compose does this)." />}
      {cfg?.admin_required && !cfg.admin_configured && <ErrorBox error="Set DEVNEXA_ADMIN_TOKEN in the server's .env and restart the backend to turn on deploys." />}
      {!unlocked ? <Unlock onDone={() => force((n) => n + 1)} /> : connErr && /admin key/i.test(connErr) ? (
        <><ErrorBox error="That admin key was not accepted." /><Unlock onDone={() => force((n) => n + 1)} /></>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[400px_minmax(0,1fr)]">
          <div className="space-y-4">
            <GitHubCard conn={conn} reload={reloadConn} show={show} />
            {conn?.connected && <RepoPicker show={show} onDeployed={setSelected} />}
            {conn?.connected && (
              <Card title="Your apps" icon={Rocket} bodyClass=""
                subtitle={cfg?.url_mode === "subdomain" ? `Each app gets https://<name>.${cfg.apps_domain}` : "Each app gets its own port on this server"}>
                <AppsList apps={apps} selected={selected} onSelect={setSelected} />
              </Card>
            )}
          </div>
          <div className="min-w-0">
            {selected ? <Detail id={selected} onSelect={setSelected} show={show} />
              : <Card><Empty title="No deployment selected" icon={Terminal}>Deploy a repo or pick an app to see its live logs and health.</Empty></Card>}
          </div>
        </div>
      )}
      <Toast toast={toast} />
    </>
  );
}
