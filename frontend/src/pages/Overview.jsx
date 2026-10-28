import { CheckCircle2, GitBranch, Rocket, Siren, Timer, TriangleAlert, UserCheck } from "lucide-react";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Card, chartTooltip, ErrorBox, Loading, StatusBadge, StatusIcon } from "../components/ui";
import { ago, duration, pct, titleCase } from "../format";
import { usePoll } from "../hooks";

function StatusBanner({ env, k }) {
  const ok = env.healthy && !k.open_incidents;
  const Icon = ok ? CheckCircle2 : TriangleAlert;
  const detail = !env.healthy
    ? `Active fault: ${env.active_faults.map(titleCase).join(", ") || "health check failing"}`
    : k.open_incidents
      ? `${k.open_incidents} open incident${k.open_incidents > 1 ? "s" : ""} being handled`
      : "All health checks passing";
  return (
    <div className={`flex flex-col gap-3 rounded-2xl border px-5 py-4 sm:flex-row sm:items-center ${
      ok ? "border-emerald-500/25 bg-emerald-500/[0.07]" : "border-rose-500/30 bg-rose-500/[0.08]"
    }`}>
      <Icon className={`h-7 w-7 shrink-0 ${ok ? "text-emerald-400" : "text-rose-400"}`} />
      <div className="flex-1">
        <p className="text-lg font-semibold text-white">{ok ? "All systems operational" : "Attention needed"}</p>
        <p className="text-sm text-slate-400">{detail}</p>
      </div>
      <div className="text-sm text-slate-400 sm:text-right">
        <p className="font-mono text-slate-200">{env.version || "—"}</p>
        <p className="text-xs">{env.mode === "live" ? "Live stack" : "Simulated environment"}</p>
      </div>
    </div>
  );
}

function Tile({ label, value, hint, icon: Icon, to, alert }) {
  const body = (
    <div className={`card h-full p-4 transition ${to ? "hover:border-white/15" : ""}`}>
      <div className="flex items-center justify-between text-slate-400">
        <p className="text-xs font-medium">{label}</p>
        <Icon className={`h-4 w-4 ${alert ? "text-amber-300" : ""}`} />
      </div>
      <p className={`mt-2 text-2xl font-semibold tracking-tight ${alert ? "text-amber-200" : "text-white"}`}>{value ?? "—"}</p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}

const viewAll = (to) => <Link to={to} className="text-xs text-indigo-300 hover:text-indigo-200">View all</Link>;

export default function Overview() {
  const { data, error } = usePoll("/api/overview", 4000);
  const { data: runs } = usePoll("/api/pipelines?limit=5", 4000);
  const { data: open } = usePoll("/api/incidents?status=open&limit=5", 4000);
  if (!data) return error ? <ErrorBox error={error} /> : <Loading />;
  const k = data.kpis;
  const env = data.environment;
  const dep = data.active_deployment;
  return (
    <>
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-white">Dashboard</h1>
          <p className="mt-1 text-sm text-slate-400">Service health, deployments and incidents at a glance. Refreshes every few seconds.</p>
        </div>
        <Link to="/pipelines" className="btn-primary"><GitBranch className="h-4 w-4" /> Run pipeline</Link>
      </div>

      <StatusBanner env={env} k={k} />

      <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Tile label="Pipeline success" value={pct(k.pipeline_success_rate)} hint={`${k.pipeline_runs} runs`} icon={GitBranch} to="/pipelines" />
        <Tile label="Open incidents" value={k.open_incidents} hint={`${k.resolved_incidents} resolved`} icon={Siren} to="/incidents" alert={k.open_incidents > 0} />
        <Tile label="Mean time to recovery" value={duration(k.mttr_s)} hint="detection to verified fix" icon={Timer} />
        <Tile label="Waiting for approval" value={k.pending_approvals} hint="fixes needing a human" icon={UserCheck} to="/approvals" alert={k.pending_approvals > 0} />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card title="Health checks" subtitle={dep ? `Running ${dep.version} · deployed ${ago(dep.deployed_at)}` : "orders-service"}
              action={<Link to="/deployments" className="text-xs text-indigo-300 hover:text-indigo-200"><Rocket className="inline h-3.5 w-3.5" /> Deployments</Link>}>
          <ul className="space-y-2.5">
            {env.health.map((c) => (
              <li key={c.name} className="flex items-center justify-between gap-3 text-sm">
                <span className="flex items-center gap-2 text-slate-300">
                  <span className={`h-2 w-2 rounded-full ${c.passed ? "bg-emerald-400" : "bg-rose-400"}`} />
                  {c.name}
                </span>
                <span className={`font-mono text-xs ${c.passed ? "text-slate-300" : "text-rose-300"}`}>
                  {typeof c.value === "number" ? +c.value.toFixed(3) : c.value}
                  <span className="text-slate-500"> / {c.threshold}</span>
                </span>
              </li>
            ))}
          </ul>
        </Card>

        <Card title="Open incidents" bodyClass="py-1" action={viewAll("/incidents")}>
          {!open ? <Loading /> : !open.length ? (
            <p className="flex items-center gap-2 px-5 py-6 text-sm text-slate-400">
              <CheckCircle2 className="h-4 w-4 text-emerald-400" /> No open incidents.
            </p>
          ) : (
            <ul className="divide-y divide-white/[0.04]">
              {open.map((i) => (
                <li key={i.id}>
                  <Link to={`/incidents/${i.id}`} className="flex items-center gap-3 px-5 py-3 hover:bg-white/[0.02]">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm text-slate-100">#{i.id} {i.title}</p>
                      <p className="text-xs text-slate-500">
                        {i.diagnosis?.title || "Diagnosing…"} · {ago(i.detected_at)}
                      </p>
                    </div>
                    <StatusBadge status={i.status} />
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Recent pipelines" bodyClass="py-1" action={viewAll("/pipelines")}>
          {!runs ? <Loading /> : !runs.length ? (
            <p className="px-5 py-6 text-sm text-slate-400">No pipeline runs yet.</p>
          ) : (
            <ul className="divide-y divide-white/[0.04]">
              {runs.map((r) => (
                <li key={r.id}>
                  <Link to={`/pipelines/${r.id}`} className="flex items-center gap-3 px-5 py-3 hover:bg-white/[0.02]">
                    <StatusIcon status={r.status} />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm text-slate-100">{r.commit_message || r.commit_sha.slice(0, 7)}</p>
                      <p className="truncate text-xs text-slate-500">
                        #{r.id} · {r.repo} · {r.branch} · {ago(r.created_at)}
                      </p>
                    </div>
                    <span className="text-xs text-slate-500">{duration(r.duration_s)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Last 7 days" subtitle="Pipeline runs per day">
          <div className="h-48">
            <ResponsiveContainer>
              <BarChart data={data.timeline} margin={{ left: -20, right: 8, top: 8 }}>
                <CartesianGrid vertical={false} />
                <XAxis dataKey="date" tickFormatter={(d) => d.slice(5)} axisLine={false} tickLine={false} />
                <YAxis allowDecimals={false} axisLine={false} tickLine={false} />
                <Tooltip {...chartTooltip} />
                <Bar dataKey="success" stackId="a" fill="#34d399" name="Passed" />
                <Bar dataKey="failed" stackId="a" fill="#fb7185" radius={[4, 4, 0, 0]} name="Failed" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>

      <Card className="mt-4" title="Recent activity" bodyClass="py-1" action={viewAll("/audit")}>
        <ul className="divide-y divide-white/[0.04]">
          {data.recent_activity.slice(0, 6).map((a) => (
            <li key={a.id} className="flex items-baseline gap-3 px-5 py-2.5">
              <p className="min-w-0 flex-1 truncate text-[13px] text-slate-200">{a.message}</p>
              <p className="shrink-0 text-[11px] text-slate-500">{a.actor} · {ago(a.ts)}</p>
            </li>
          ))}
          {!data.recent_activity.length && <li className="px-5 py-6 text-sm text-slate-400">Nothing yet.</li>}
        </ul>
      </Card>
    </>
  );
}
