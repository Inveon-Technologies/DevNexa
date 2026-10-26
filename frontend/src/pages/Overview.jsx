import { Activity, BookOpenCheck, Bot, CheckCircle2, Clock, GitBranch, ShieldAlert, Siren, Timer, UserCheck } from "lucide-react";
import { Link } from "react-router-dom";
import {
  Bar, BarChart, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import LoopDiagram from "../components/LoopDiagram";
import { Badge, Card, chartTooltip, ErrorBox, Loading, PageHeader, Stat } from "../components/ui";
import { ago, duration, pct, titleCase } from "../format";
import { usePoll } from "../hooks";

const CAUSE_COLORS = ["#818cf8", "#22d3ee", "#34d399", "#fbbf24", "#f472b6", "#a78bfa", "#fb7185", "#94a3b8"];

export default function Overview() {
  const { data, error } = usePoll("/api/overview", 4000);
  if (!data) return error ? <ErrorBox error={error} /> : <Loading />;
  const k = data.kpis;
  const env = data.environment;
  return (
    <>
      <PageHeader
        eyebrow="Command center"
        title="Closed-loop DevOps intelligence"
        description="From code push through build, test, deploy and monitoring to evidence-based diagnosis, controlled recovery, and learning that shapes the next pipeline."
        actions={
          <>
            <Link to="/fault-lab" className="btn-ghost">Inject a fault</Link>
            <Link to="/pipelines" className="btn-primary"><GitBranch className="h-4 w-4" /> Run pipeline</Link>
          </>
        }
      />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <Stat label="Pipeline runs" value={k.pipeline_runs} hint={`${pct(k.pipeline_success_rate)} success`} icon={GitBranch} />
        <Stat label="Open incidents" value={k.open_incidents} hint={`${k.resolved_incidents} resolved`} icon={Siren} tone={k.open_incidents ? "rose" : "emerald"} />
        <Stat label="Mean time to recovery" value={duration(k.mttr_s)} hint="detection → verified" icon={Timer} tone="cyan" />
        <Stat label="Detection time" value={duration(k.detection_s)} hint="fault → incident" icon={Clock} tone="violet" />
        <Stat label="Pending approvals" value={k.pending_approvals} hint="human in the loop" icon={UserCheck} tone={k.pending_approvals ? "amber" : "indigo"} />
        <Stat label="Auto-remediated" value={k.auto_remediations} hint="allowlisted low-risk" icon={Bot} tone="emerald" />
        <Stat label="Verified knowledge" value={k.knowledge_entries} hint="pgvector memory" icon={BookOpenCheck} tone="violet" />
        <Stat label="Regression tests" value={k.regression_tests} hint="learned from incidents" icon={CheckCircle2} tone="cyan" />
      </div>

      <Card className="mt-6" title="DevNexa architecture, live" subtitle="Five layers with a continuous feedback loop" icon={Activity}>
        <LoopDiagram
          stats={{
            source: `${k.pipeline_runs} commits processed`,
            cicd: `${pct(k.pipeline_success_rate)} pipeline success`,
            obs: env.healthy ? "All health checks passing" : `${env.active_faults.length} active fault(s)`,
            ai: `${k.resolved_incidents} incidents verified · ${pct(k.first_attempt_rca_confirmed)} fixed on 1st diagnosis`,
            learn: `${k.knowledge_entries} cases · ${k.regression_tests} new tests`,
          }}
        />
      </Card>

      <div className="mt-6 grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2" title="Delivery & incidents, last 7 days" subtitle="Pipeline outcomes per day with incidents opened">
          <div className="h-64">
            <ResponsiveContainer>
              <ComposedChart data={data.timeline} margin={{ left: -20, right: 8, top: 8 }}>
                <CartesianGrid vertical={false} />
                <XAxis dataKey="date" tickFormatter={(d) => d.slice(5)} axisLine={false} tickLine={false} />
                <YAxis allowDecimals={false} axisLine={false} tickLine={false} />
                <Tooltip {...chartTooltip} />
                <Bar dataKey="success" stackId="a" fill="#34d399" radius={[0, 0, 0, 0]} name="Successful runs" />
                <Bar dataKey="failed" stackId="a" fill="#fb7185" radius={[4, 4, 0, 0]} name="Failed runs" />
                <Line dataKey="incidents" stroke="#fbbf24" strokeWidth={2} dot={{ r: 3 }} name="Incidents" />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card title="Service health" subtitle={`orders-service ${env.version}`}
              action={<Badge tone={env.healthy ? "green" : "red"} dot pulse>{env.healthy ? "Healthy" : "Degraded"}</Badge>}>
          <ul className="space-y-2.5">
            {env.health.map((c) => (
              <li key={c.name} className="flex items-center justify-between text-sm">
                <span className="text-slate-300">{c.name}</span>
                <span className={`font-mono text-xs ${c.passed ? "text-emerald-300" : "text-rose-300"}`}>
                  {typeof c.value === "number" ? +c.value.toFixed(3) : c.value}
                  <span className="text-slate-500"> / {c.threshold}</span>
                </span>
              </li>
            ))}
          </ul>
          {env.active_faults.length > 0 && (
            <div className="mt-4 rounded-lg border border-rose-500/25 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
              Active fault: {env.active_faults.map(titleCase).join(", ")}
            </div>
          )}
        </Card>
      </div>

      <div className="mt-6 grid gap-6 xl:grid-cols-3">
        <Card title="Time to verified recovery" subtitle="Recent incidents, minutes">
          <div className="h-56">
            {data.mttr_trend.length ? (
              <ResponsiveContainer>
                <BarChart data={data.mttr_trend} margin={{ left: -20, right: 8, top: 8 }}>
                  <CartesianGrid vertical={false} />
                  <XAxis dataKey="incident" tickFormatter={(v) => `#${v}`} axisLine={false} tickLine={false} />
                  <YAxis axisLine={false} tickLine={false} />
                  <Tooltip {...chartTooltip} formatter={(v) => [`${v} min`, "MTTR"]} labelFormatter={(v) => `Incident #${v}`} />
                  <Bar dataKey="mttr_min" fill="#22d3ee" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            ) : <p className="text-sm text-slate-500">No resolved incidents yet.</p>}
          </div>
        </Card>
        <Card title="Incidents by confirmed cause">
          <ul className="space-y-3">
            {data.incidents_by_cause.map((c, i) => {
              const max = Math.max(...data.incidents_by_cause.map((x) => x.count));
              return (
                <li key={c.cause}>
                  <div className="mb-1 flex justify-between text-xs">
                    <span className="text-slate-300">{c.cause}</span>
                    <span className="text-slate-500">{c.count}</span>
                  </div>
                  <div className="h-1.5 rounded-full bg-white/[0.05]">
                    <div className="h-full rounded-full" style={{ width: `${(c.count / max) * 100}%`, background: CAUSE_COLORS[i % CAUSE_COLORS.length] }} />
                  </div>
                </li>
              );
            })}
            {!data.incidents_by_cause.length && <p className="text-sm text-slate-500">No incidents yet.</p>}
          </ul>
        </Card>
        <Card title="Agent activity" subtitle="Latest audited actions" bodyClass="px-2 py-2"
              action={<Link to="/audit" className="text-xs text-indigo-300 hover:text-indigo-200">View all</Link>}>
          <ul className="max-h-64 divide-y divide-white/[0.04] overflow-y-auto">
            {data.recent_activity.map((a) => (
              <li key={a.id} className="flex gap-3 px-3 py-2.5">
                <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-500" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[13px] text-slate-200">{a.message}</p>
                  <p className="text-[11px] text-slate-500">{a.actor} · {ago(a.ts)}</p>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      </div>
    </>
  );
}
