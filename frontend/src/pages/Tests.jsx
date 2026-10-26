import { ListChecks, Target } from "lucide-react";
import { useEffect, useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { Badge, Card, chartTooltip, Loading, Meter, PageHeader } from "../components/ui";
import { usePoll } from "../hooks";

export default function Tests() {
  const { data: risk } = usePoll("/api/modules/risk", 5000);
  const { data: tests } = usePoll("/api/tests", 5000);
  const { data: catalog } = usePoll("/api/catalog", 0);
  const [files, setFiles] = useState(["app/cache/lru.py"]);
  const [plan, setPlan] = useState(null);
  useEffect(() => {
    if (!files.length) return setPlan(null);
    api.get(`/api/tests/prioritize?files=${encodeURIComponent(files.join(","))}`).then(setPlan);
  }, [files, tests?.length]);
  const allFiles = catalog ? Object.values(catalog.modules).flat().filter((f) => !f.endsWith("/")) : [];
  const regressions = (tests || []).filter((t) => t.is_regression);
  return (
    <>
      <PageHeader
        eyebrow="Learning → CI"
        title="Test intelligence"
        description="Verified incidents raise module risk and add regression tests. The Test Agent ranks tests by change impact, learned risk and failure history instead of always running the full suite."
      />
      <div className="grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2" title="Module risk learned from verified incidents" icon={Target}>
          {!risk ? <Loading /> : (
            <div className="h-64">
              <ResponsiveContainer>
                <BarChart data={risk} layout="vertical" margin={{ left: 40, right: 16 }}>
                  <CartesianGrid horizontal={false} />
                  <XAxis type="number" domain={[0, 1]} axisLine={false} tickLine={false} />
                  <YAxis type="category" dataKey="module" width={120} axisLine={false} tickLine={false} />
                  <Tooltip {...chartTooltip} formatter={(v, n, p) => [`${v.toFixed(2)} (${p.payload.incident_count} incidents)`, "Risk"]} />
                  <Bar dataKey="risk_score" radius={[0, 4, 4, 0]}>
                    {risk.map((r) => <Cell key={r.module} fill={r.risk_score > 0.5 ? "#fb7185" : r.risk_score > 0.2 ? "#fbbf24" : "#34d399"} />)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </Card>
        <Card title="Regression tests from incidents" subtitle={`${regressions.length} added by the Feedback Engine`}>
          <ul className="space-y-2.5">
            {regressions.map((t) => (
              <li key={t.id}>
                <p className="font-mono text-[11.5px] text-cyan-300">{t.id}</p>
                <p className="text-xs text-slate-500">{t.module} · from incident #{t.origin_incident_id} · catches {t.detects.join(", ")}</p>
              </li>
            ))}
            {!regressions.length && <p className="text-sm text-slate-500">None yet.</p>}
          </ul>
        </Card>
      </div>
      <Card className="mt-6" title="Prioritisation simulator" subtitle="Pick the files a commit changes to see what the Test Agent would run" icon={ListChecks}>
        <div className="flex flex-wrap gap-1.5">
          {allFiles.map((f) => (
            <button key={f} onClick={() => setFiles(files.includes(f) ? files.filter((x) => x !== f) : [...files, f])}
              className={`rounded-md px-2 py-1 font-mono text-[11.5px] ring-1 ring-inset transition ${files.includes(f) ? "bg-indigo-500/20 text-indigo-200 ring-indigo-400/40" : "bg-white/[0.03] text-slate-400 ring-white/10 hover:text-slate-200"}`}>{f}</button>
          ))}
        </div>
        {plan && (
          <>
            <div className="mt-5 grid gap-4 sm:grid-cols-4">
              <div className="rounded-xl bg-white/[0.03] p-3"><p className="label">Selected</p><p className="text-xl font-semibold text-white">{plan.selected.length}<span className="text-sm text-slate-500"> / {plan.ranking.length}</span></p></div>
              <div className="rounded-xl bg-white/[0.03] p-3"><p className="label">Est. time</p><p className="text-xl font-semibold text-white">{plan.estimated_time_s}s<span className="text-sm text-slate-500"> / {plan.full_suite_time_s}s</span></p></div>
              <div className="rounded-xl bg-white/[0.03] p-3"><p className="label">Time saved</p><p className="text-xl font-semibold text-emerald-300">{Math.round((1 - plan.estimated_time_s / plan.full_suite_time_s) * 100)}%</p></div>
              <div className="rounded-xl bg-white/[0.03] p-3"><p className="label">Change risk</p><p className="text-xl font-semibold text-white">{plan.risk_score.toFixed(2)}</p><Meter value={plan.risk_score} tone={plan.risk_score > 0.5 ? "amber" : "green"} className="mt-1" /></div>
            </div>
            <div className="mt-4 max-h-96 overflow-auto">
              <table className="w-full">
                <thead className="sticky top-0 bg-ink-900"><tr><th className="table-head">Test</th><th className="table-head">Score</th><th className="table-head">Reasons</th><th className="table-head">Run?</th></tr></thead>
                <tbody className="divide-y divide-white/[0.04]">
                  {plan.ranking.map((t) => (
                    <tr key={t.test_id} className={t.selected ? "" : "opacity-45"}>
                      <td className="table-cell font-mono text-xs">{t.test_id} {t.is_regression && <Badge tone="cyan">regression</Badge>}</td>
                      <td className="table-cell font-mono text-xs">{t.score.toFixed(2)}</td>
                      <td className="table-cell text-xs text-slate-400">{t.reasons.join(" · ")}</td>
                      <td className="table-cell">{t.selected ? <Badge tone="green">run</Badge> : <Badge>skip</Badge>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Card>
    </>
  );
}
