import { FlaskRound, Info, Play } from "lucide-react";
import { useEffect, useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { Badge, Card, chartTooltip, ErrorBox, Loading, PageHeader, StatusBadge } from "../components/ui";
import { duration, pct, time } from "../format";
import { usePoll } from "../hooks";

const COLORS = { devnexa: "#818cf8", no_rag: "#22d3ee", baseline: "#64748b" };
const METRICS = [
  { key: "failure_detection_s", label: "Failure detection time", fmt: duration, better: "lower" },
  { key: "rca_accuracy", label: "Root-cause accuracy (1st diagnosis)", fmt: (v) => pct(v), better: "higher" },
  { key: "mttr_s", label: "Mean time to recovery", fmt: duration, better: "lower" },
  { key: "repeated_incidents", label: "Repeated incidents in production", fmt: (v) => v, better: "lower" },
  { key: "avg_test_time_s", label: "Test time per pipeline", fmt: (v) => `${v}s`, better: "lower" },
  { key: "faults_caught_in_ci", label: "Recurring faults caught in CI", fmt: (v) => v, better: "higher" },
  { key: "human_steps_per_incident", label: "Human steps per incident", fmt: (v) => v, better: "lower" },
];

function Results({ exp }) {
  const r = exp.results;
  if (exp.status !== "completed") return exp.status === "failed" ? <ErrorBox error={r?.error} /> : <Loading label="Running fault-injection experiment" />;
  const names = Object.keys(r.variants);
  const chart = (key, scale = 1) => names.map((n) => ({ name: r.variants[n].label, value: (r.variants[n].metrics[key] ?? 0) * scale, id: n }));
  return (
    <>
      <Card className="mt-6" title="Results" subtitle={`${exp.name} · ${time(exp.created_at)}`} bodyClass="">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead className="border-b border-white/[0.06]"><tr><th className="table-head">Metric</th>
              {names.map((n) => <th key={n} className="table-head"><span className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-full" style={{ background: COLORS[n] }} />{r.variants[n].label}</span></th>)}</tr></thead>
            <tbody className="divide-y divide-white/[0.04]">
              {METRICS.map((m) => {
                const vals = names.map((n) => r.variants[n].metrics[m.key]);
                const valid = vals.filter((v) => v !== null && v !== undefined);
                const best = m.better === "lower" ? Math.min(...valid) : Math.max(...valid);
                return (
                  <tr key={m.key}>
                    <td className="table-cell text-slate-300">{m.label}<span className="ml-1 text-xs text-slate-500">({m.better} is better)</span></td>
                    {names.map((n, i) => (
                      <td key={n} className={`table-cell font-mono text-sm ${vals[i] === best && valid.length > 1 ? "text-emerald-300" : "text-slate-200"}`}>
                        {vals[i] === null || vals[i] === undefined ? "—" : m.fmt(vals[i])}
                        {n === "baseline" && ["rca_accuracy", "failure_detection_s", "mttr_s", "human_steps_per_incident"].includes(m.key) && <span className="ml-1 text-[10px] text-amber-300/80">assumed</span>}
                      </td>
                    ))}
                  </tr>
                );
              })}
              <tr><td className="table-cell text-slate-300">Fault occurrences / incidents reaching prod</td>
                {names.map((n) => <td key={n} className="table-cell font-mono text-sm">{r.variants[n].metrics.fault_occurrences} / {r.variants[n].metrics.incidents}</td>)}</tr>
              <tr><td className="table-cell text-slate-300">Test time saved vs full suite</td>
                {names.map((n) => <td key={n} className="table-cell font-mono text-sm">{r.variants[n].metrics.test_time_saved_pct}%</td>)}</tr>
            </tbody>
          </table>
        </div>
      </Card>
      <div className="mt-6 grid gap-6 lg:grid-cols-3">
        {[["mttr_s", "MTTR (minutes)", 1 / 60], ["rca_accuracy", "Root-cause accuracy (%)", 100], ["repeated_incidents", "Repeated incidents", 1]].map(([k, t, s]) => (
          <Card key={k} title={t}>
            <div className="h-52">
              <ResponsiveContainer>
                <BarChart data={chart(k, s)} margin={{ left: -20, right: 8, top: 8 }}>
                  <CartesianGrid vertical={false} />
                  <XAxis dataKey="id" axisLine={false} tickLine={false} />
                  <YAxis axisLine={false} tickLine={false} />
                  <Tooltip {...chartTooltip} formatter={(v) => [+v.toFixed(2), t]} />
                  <Bar dataKey="value" radius={[6, 6, 0, 0]} fill="#818cf8" shape={(p) => <rect x={p.x} y={p.y} width={p.width} height={p.height} rx={6} fill={COLORS[p.payload.id]} />} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Card>
        ))}
      </div>
      <Card className="mt-6" title="Per-scenario root-cause accuracy" bodyClass="">
        <div className="h-80 px-4 pt-4">
          <ResponsiveContainer>
            <BarChart data={r.scenarios.map((s) => ({ name: s.title, ...Object.fromEntries(names.map((n) => [n, (s[`${n}_rca`] ?? 0) * 100])) }))} margin={{ left: -10, right: 8 }}>
              <CartesianGrid vertical={false} />
              <XAxis dataKey="name" axisLine={false} tickLine={false} interval={0} tick={{ fontSize: 10 }} angle={-18} textAnchor="end" height={80} />
              <YAxis axisLine={false} tickLine={false} domain={[0, 100]} />
              <Tooltip {...chartTooltip} formatter={(v) => `${v.toFixed(0)}%`} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {names.map((n) => <Bar key={n} dataKey={n} name={r.variants[n].label} fill={COLORS[n]} radius={[3, 3, 0, 0]} />)}
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Card>
      <div className="mt-6 rounded-xl border border-amber-400/20 bg-amber-500/[0.05] p-4 text-sm text-amber-100/90">
        <p className="mb-1 flex items-center gap-2 font-medium"><Info className="h-4 w-4" /> How to read these numbers</p>
        <ul className="list-disc space-y-1 pl-5 text-amber-100/80">{r.notes.map((n) => <li key={n}>{n}</li>)}</ul>
      </div>
    </>
  );
}

export default function Evaluation() {
  const { data: defaults } = usePoll("/api/experiments/defaults", 0);
  const { data: list, reload } = usePoll("/api/experiments", 3000);
  const [selected, setSelected] = useState(null);
  const { data: exp } = usePoll(selected ? `/api/experiments/${selected}` : null, 2000, [selected]);
  const [cfg, setCfg] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (defaults && !cfg) setCfg(defaults.config); }, [defaults, cfg]);
  useEffect(() => { if (list?.length && !selected) setSelected(list[0].id); }, [list, selected]);
  const run = async () => {
    setBusy(true);
    try {
      const e = await api.post("/api/experiments", { config: cfg });
      setSelected(e.id);
      reload();
    } finally { setBusy(false); }
  };
  return (
    <>
      <PageHeader eyebrow="Testing & evaluation plan" title="Evaluation"
        description="Controlled fault-injection experiments comparing DevNexa with a conventional CI/CD baseline and a no-RAG ablation on detection time, root-cause accuracy, MTTR, repeated incidents, test efficiency and human intervention." />
      <div className="grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2" title="New experiment" icon={FlaskRound}>
          {!cfg ? <Loading /> : (
            <div className="space-y-5">
              <div>
                <label className="label">Fault scenarios</label>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(defaults.scenarios).map(([id, title]) => {
                    const on = cfg.scenarios.includes(id);
                    return <button key={id} onClick={() => setCfg({ ...cfg, scenarios: on ? cfg.scenarios.filter((s) => s !== id) : [...cfg.scenarios, id] })}
                      className={`rounded-md px-2.5 py-1 text-xs ring-1 ring-inset transition ${on ? "bg-indigo-500/20 text-indigo-200 ring-indigo-400/40" : "bg-white/[0.03] text-slate-400 ring-white/10"}`}>{title}</button>;
                  })}
                </div>
              </div>
              <div className="grid gap-4 sm:grid-cols-3">
                <div><label className="label">Repetitions per scenario</label><input type="number" min="1" max="10" className="input" value={cfg.repetitions} onChange={(e) => setCfg({ ...cfg, repetitions: +e.target.value })} /></div>
                <div><label className="label">Random seed</label><input type="number" className="input" value={cfg.seed} onChange={(e) => setCfg({ ...cfg, seed: +e.target.value })} /></div>
                <div><label className="label">Observability gap · {Math.round(cfg.observability_gap * 100)}%</label>
                  <input type="range" min="0" max="0.9" step="0.05" value={cfg.observability_gap} onChange={(e) => setCfg({ ...cfg, observability_gap: +e.target.value })} className="mt-3 w-full accent-indigo-400" /></div>
              </div>
              <div>
                <label className="label">Baseline assumptions (conventional workflow)</label>
                <div className="grid gap-3 sm:grid-cols-4">
                  <div><span className="text-xs text-slate-500">Manual RCA accuracy</span><input type="number" step="0.05" min="0" max="1" className="input mt-1" value={cfg.baseline.rca_accuracy} onChange={(e) => setCfg({ ...cfg, baseline: { ...cfg.baseline, rca_accuracy: +e.target.value } })} /></div>
                  <div><span className="text-xs text-slate-500">Manual steps / incident</span><input type="number" min="1" className="input mt-1" value={cfg.baseline.manual_steps} onChange={(e) => setCfg({ ...cfg, baseline: { ...cfg.baseline, manual_steps: +e.target.value } })} /></div>
                  <div className="sm:col-span-2"><span className="text-xs text-slate-500">Detection range (s)</span>
                    <div className="mt-1 flex gap-2">{[0, 1].map((i) => <input key={i} type="number" className="input" value={cfg.baseline.detection_s[i]} onChange={(e) => { const d = [...cfg.baseline.detection_s]; d[i] = +e.target.value; setCfg({ ...cfg, baseline: { ...cfg.baseline, detection_s: d } }); }} />)}</div></div>
                </div>
              </div>
              <button className="btn-primary" disabled={busy || !cfg.scenarios.length} onClick={run}><Play className="h-4 w-4" /> Run experiment</button>
            </div>
          )}
        </Card>
        <Card title="Past experiments" bodyClass="px-2 py-2">
          <ul className="max-h-96 divide-y divide-white/[0.04] overflow-y-auto">
            {(list || []).map((e) => (
              <li key={e.id}>
                <button onClick={() => setSelected(e.id)} className={`w-full px-3 py-3 text-left transition ${selected === e.id ? "bg-white/[0.05]" : "hover:bg-white/[0.02]"}`}>
                  <div className="flex items-center justify-between gap-2"><span className="truncate text-sm text-slate-200">#{e.id} {e.name}</span><StatusBadge status={e.status} /></div>
                  <p className="mt-0.5 text-xs text-slate-500">{time(e.created_at)}{e.variants && ` · MTTR ${duration(e.variants.devnexa?.metrics.mttr_s)} vs ${duration(e.variants.baseline?.metrics.mttr_s)}`}</p>
                </button>
              </li>
            ))}
            {list && !list.length && <p className="px-3 py-4 text-sm text-slate-500">No experiments yet. Run one to compare against the baseline.</p>}
          </ul>
        </Card>
      </div>
      {exp && <Results exp={exp} />}
      {exp?.status === "completed" && <p className="mt-4 text-xs text-slate-500">Config: <Badge>{exp.config.repetitions}× each</Badge> <Badge>seed {exp.config.seed}</Badge> <Badge>gap {pct(exp.config.observability_gap)}</Badge></p>}
    </>
  );
}
