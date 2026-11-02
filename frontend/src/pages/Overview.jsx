import {
  Activity, AlertOctagon, CheckCircle2, ChevronRight, Gauge, Siren, Timer, TriangleAlert, UserCheck, Users,
} from "lucide-react";
import { Link } from "react-router-dom";
import { LiveDot, ms, num, secsAgo, StatusCode, TrafficChart } from "../components/traffic";
import { Badge, Card, ErrorBox, Loading } from "../components/ui";
import { ago, pct, titleCase } from "../format";
import { usePoll } from "../hooks";

function Tile({ label, value, hint, icon: Icon, tone = "slate", to }) {
  const color = { slate: "text-white", red: "text-rose-300", amber: "text-amber-200", green: "text-emerald-300" }[tone];
  const body = (
    <div className={`card h-full p-4 ${to ? "transition hover:border-white/15" : ""}`}>
      <div className="flex items-center justify-between text-slate-400">
        <p className="text-xs font-medium">{label}</p>
        <Icon className="h-4 w-4" />
      </div>
      <p className={`mt-2 text-3xl font-semibold tracking-tight ${color}`}>{value ?? "—"}</p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}

/** One list of everything that needs a human, most urgent first. */
function buildAttention(t, ov, incidents) {
  const items = [];
  for (const p of t?.problems || []) {
    if (p.severity !== "error") continue;
    items.push({
      key: `p-${p.app}-${p.method}-${p.path}-${p.status}`, tone: "red", status: p.status,
      title: `${p.method} ${p.path}`,
      detail: `${p.app} · ${p.count} failed request${p.count > 1 ? "s" : ""} · last ${secsAgo(p.last_seen)}`,
      to: `/traffic?app=${encodeURIComponent(p.app)}&errors=1&path=${encodeURIComponent(p.path)}`,
    });
  }
  for (const i of incidents || []) {
    items.push({
      key: `i-${i.id}`, tone: "red", icon: Siren, title: `Incident #${i.id}: ${i.title}`,
      detail: `${i.diagnosis?.title || "Diagnosing"} · ${titleCase(i.status)} · ${ago(i.detected_at)}`,
      to: `/incidents/${i.id}`,
    });
  }
  for (const c of ov?.environment?.health || []) {
    if (c.passed) continue;
    items.push({
      key: `h-${c.name}`, tone: "red", icon: TriangleAlert, title: `Health check failing: ${c.name}`,
      detail: `${typeof c.value === "number" ? +c.value.toFixed(3) : c.value} (limit ${c.threshold})`, to: "/incidents",
    });
  }
  if (ov?.kpis?.pending_approvals) {
    const n = ov.kpis.pending_approvals;
    items.push({
      key: "approvals", tone: "amber", icon: UserCheck, title: `${n} fix${n > 1 ? "es" : ""} waiting for your approval`,
      detail: "Review and approve or reject", to: "/approvals",
    });
  }
  for (const p of (t?.problems || []).filter((x) => x.severity !== "error").slice(0, 3)) {
    items.push({
      key: `w-${p.app}-${p.method}-${p.path}-${p.status}`, tone: "amber", status: p.status,
      title: `${p.method} ${p.path}`,
      detail: `${p.app} · ${p.count} request${p.count > 1 ? "s" : ""} · ${p.status === 404 ? "missing page or broken link" : "client error"}`,
      to: `/traffic?app=${encodeURIComponent(p.app)}&errors=1&path=${encodeURIComponent(p.path)}`,
    });
  }
  return items;
}

function Attention({ items }) {
  if (!items.length) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 px-5 py-10 text-center">
        <CheckCircle2 className="h-7 w-7 text-emerald-400" />
        <p className="text-sm font-medium text-slate-200">Nothing needs attention</p>
        <p className="text-xs text-slate-500">No failing pages, incidents or pending approvals.</p>
      </div>
    );
  }
  return (
    <ul className="divide-y divide-white/[0.04]">
      {items.slice(0, 8).map((it) => (
        <li key={it.key}>
          <Link to={it.to} className="flex items-center gap-3 px-5 py-3 hover:bg-white/[0.03]">
            {it.status ? <StatusCode status={it.status} /> : (
              <span className="grid w-11 shrink-0 place-items-center">
                <it.icon className={`h-4 w-4 ${it.tone === "red" ? "text-rose-400" : "text-amber-300"}`} />
              </span>
            )}
            <div className="min-w-0 flex-1">
              <p className={`truncate text-sm text-slate-100 ${it.status ? "font-mono text-[12.5px]" : ""}`}>{it.title}</p>
              <p className="truncate text-xs text-slate-500">{it.detail}</p>
            </div>
            <ChevronRight className="h-4 w-4 shrink-0 text-slate-600" />
          </Link>
        </li>
      ))}
    </ul>
  );
}

function AppsTable({ apps }) {
  if (!apps.length) {
    return (
      <p className="px-5 py-8 text-sm text-slate-400">
        No visits yet. Deploy an app from <Link to="/deploy" className="text-indigo-300 hover:text-indigo-200">Deploy from GitHub</Link> and
        open its URL to see traffic here.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px]">
        <thead>
          <tr className="border-b border-white/[0.06]">
            <th className="table-head">App</th>
            <th className="table-head text-right">Visitors</th>
            <th className="table-head text-right">Requests</th>
            <th className="table-head text-right">Errors</th>
            <th className="table-head text-right">p95</th>
            <th className="table-head text-right">Last visit</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-white/[0.04]">
          {apps.map((a) => (
            <tr key={a.app} className="hover:bg-white/[0.02]">
              <td className="table-cell">
                <Link to={`/traffic?app=${encodeURIComponent(a.app)}`} className="flex items-center gap-2 font-medium text-slate-100 hover:text-indigo-200">
                  <span className={`h-2 w-2 rounded-full ${a.errors ? "bg-rose-400" : "bg-emerald-400"}`} />
                  {a.app}
                </Link>
              </td>
              <td className="table-cell text-right text-slate-300">{num(a.visitors)}</td>
              <td className="table-cell text-right text-slate-300">{num(a.requests)}</td>
              <td className={`table-cell text-right ${a.errors ? "font-medium text-rose-300" : "text-slate-500"}`}>
                {a.errors ? `${a.errors} (${pct(a.error_rate, 1)})` : "0"}
              </td>
              <td className="table-cell text-right text-slate-300">{ms(a.p95_ms)}</td>
              <td className="table-cell text-right text-xs text-slate-500">{a.last_seen ? secsAgo(a.last_seen) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RecentRequests({ rows }) {
  if (!rows.length) return <p className="px-5 py-6 text-sm text-slate-400">No requests in the last 30 minutes.</p>;
  return (
    <ul className="divide-y divide-white/[0.04] font-mono text-[12px]">
      {rows.map((r, i) => (
        <li key={`${r.ts}-${i}`} className="flex items-center gap-3 px-5 py-2">
          <StatusCode status={r.status} />
          <span className="w-10 shrink-0 text-slate-500">{r.method}</span>
          <span className="min-w-0 flex-1 truncate text-slate-200">
            <span className="text-slate-500">{r.app}</span> {r.path}
          </span>
          <span className="hidden w-16 shrink-0 text-right text-slate-400 sm:block">{ms(r.ms)}</span>
          <span className="w-16 shrink-0 text-right text-slate-500">{secsAgo(r.ts)}</span>
        </li>
      ))}
    </ul>
  );
}

export default function Overview() {
  const { data: t, error: tErr } = usePoll("/api/traffic?minutes=30&limit=12", 3000);
  const { data: ov, error } = usePoll("/api/overview", 5000);
  const { data: incidents } = usePoll("/api/incidents?status=open&limit=5", 5000);
  if (!ov && !t) return error || tErr ? <ErrorBox error={error || tErr} /> : <Loading />;

  const s = t?.summary;
  const items = buildAttention(t, ov, incidents);
  const urgent = items.filter((i) => i.tone === "red").length;
  const errTone = !s ? "slate" : s.error_rate_5m >= 0.05 ? "red" : s.errors_5m ? "amber" : "green";

  return (
    <>
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-white">Dashboard</h1>
          <p className="mt-1 text-sm text-slate-400">Who is using your apps right now, and what is breaking.</p>
        </div>
        <div className="flex items-center gap-3">
          {t?.simulated && <Badge tone="violet">Simulated traffic</Badge>}
          <LiveDot />
        </div>
      </div>

      <div className={`mb-4 flex items-center gap-3 rounded-2xl border px-5 py-3.5 ${
        urgent ? "border-rose-500/30 bg-rose-500/[0.08]" : "border-emerald-500/25 bg-emerald-500/[0.07]"
      }`}>
        {urgent ? <TriangleAlert className="h-5 w-5 shrink-0 text-rose-400" /> : <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-400" />}
        <p className="flex-1 text-sm font-medium text-white">
          {urgent ? `${urgent} problem${urgent > 1 ? "s" : ""} need${urgent > 1 ? "" : "s"} attention` : "Everything is working"}
        </p>
        <span className="hidden text-xs text-slate-400 sm:block">
          {ov?.environment?.mode === "live" ? "Live stack" : "Simulated environment"}
          {ov?.environment?.version ? ` · ${ov.environment.version}` : ""}
        </span>
      </div>

      {t?.note && !t.simulated && (
        <p className="mb-4 rounded-xl border border-white/[0.06] bg-white/[0.02] px-4 py-2.5 text-xs text-slate-400">{t.note}</p>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Tile label="Visitors now" value={num(s?.visitors_5m)} hint={`${num(s?.visitors)} in the last 30 min`} icon={Users} to="/traffic" />
        <Tile label="Requests / min" value={s ? s.requests_per_min : "—"} hint={`${num(s?.requests)} in the last 30 min`} icon={Activity} to="/traffic" />
        <Tile label="Error rate" value={s ? pct(s.error_rate_5m, 1) : "—"} hint={`${s?.errors_5m ?? 0} server errors in 5 min`}
              icon={AlertOctagon} tone={errTone} to="/traffic?errors=1" />
        <Tile label="Slowest 5% (p95)" value={ms(s?.p95_ms_5m)} hint="response time, last 5 min" icon={Timer}
              tone={s?.p95_ms_5m >= 1000 ? "amber" : "slate"} />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2" title="Traffic" subtitle="Requests and server errors per minute, last 30 minutes"
              action={<Link to="/traffic" className="text-xs text-indigo-300 hover:text-indigo-200">Open live traffic</Link>}>
          {t ? <TrafficChart series={t.series} /> : <Loading />}
        </Card>
        <Card title="Needs attention" subtitle={items.length ? `${items.length} item${items.length > 1 ? "s" : ""}` : "All clear"}
              bodyClass="py-1">
          <Attention items={items} />
        </Card>
      </div>

      <Card className="mt-4" title="Apps" subtitle="Last 30 minutes" bodyClass="py-1"
            action={<Link to="/deploy" className="text-xs text-indigo-300 hover:text-indigo-200"><Gauge className="inline h-3.5 w-3.5" /> Manage apps</Link>}>
        <AppsTable apps={t?.apps || []} />
      </Card>

      <Card className="mt-4" title="Latest requests" bodyClass="py-1"
            action={<Link to="/traffic" className="text-xs text-indigo-300 hover:text-indigo-200">See all</Link>}>
        <RecentRequests rows={t?.recent || []} />
      </Card>
    </>
  );
}
