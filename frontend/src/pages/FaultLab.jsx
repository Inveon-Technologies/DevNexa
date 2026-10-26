import { Bug, FlaskConical, GitBranch, RotateCcw, Zap } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, operatorName } from "../api";
import { Badge, Card, Loading, PageHeader, RiskBadge, Toast } from "../components/ui";
import { clock, titleCase } from "../format";
import { usePoll, useToast } from "../hooks";

function Scenario({ f, onInject }) {
  const [mode, setMode] = useState(f.default_mode);
  return (
    <div className="card flex flex-col p-5">
      <div className="flex items-start justify-between gap-2">
        <div className="grid h-9 w-9 place-items-center rounded-xl bg-rose-500/10 ring-1 ring-rose-400/20"><Bug className="h-4 w-4 text-rose-300" /></div>
        <Badge tone={f.severity === "critical" ? "red" : "amber"}>{f.severity}</Badge>
      </div>
      <h3 className="mt-3 font-semibold text-white">{f.title}</h3>
      <p className="mt-1 flex-1 text-sm text-slate-400">{f.description}</p>
      <div className="mt-3 flex flex-wrap gap-1">
        <Badge tone="indigo">{f.module}</Badge>
        {f.tags.map((t) => <Badge key={t}>{t}</Badge>)}
      </div>
      <div className="mt-3 flex items-center gap-2 text-xs text-slate-500">Runbook fix: <span className="text-slate-300">{f.remediation[0]}</span> <RiskBadge risk={f.remediation_risk} /></div>
      <div className="mt-4 flex items-center gap-2">
        <select className="input w-auto flex-1 py-1.5 text-xs" value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="pipeline">Ship in a commit (through CI/CD)</option>
          <option value="runtime">Inject at runtime</option>
        </select>
        <button className="btn-primary py-1.5" onClick={() => onInject(f, mode)}><Zap className="h-4 w-4" /> Inject</button>
      </div>
    </div>
  );
}

export default function FaultLab() {
  const { data: faults } = usePoll("/api/faults", 0);
  const { data: env, reload } = usePoll("/api/environment", 2000);
  const [toast, show] = useToast();
  const nav = useNavigate();
  const inject = async (f, mode) => {
    try {
      const r = await api.post(`/api/faults/${f.id}/inject`, { mode, actor: operatorName() });
      if (r.mode === "pipeline") {
        show(`Shipped "${f.title}" in a commit. Following pipeline #${r.run_id}…`, "info");
        setTimeout(() => nav(`/pipelines/${r.run_id}`), 600);
      } else {
        show(`Injected "${f.title}". Monitoring will raise an alert and open an incident.`, "info");
        setTimeout(() => nav("/incidents"), 1200);
      }
    } catch (e) {
      show(e.message, "error");
    }
  };
  const reset = async () => {
    await api.post(`/api/environment/reset?actor=${encodeURIComponent(operatorName())}`);
    show("All faults cleared.", "success");
    reload();
  };
  return (
    <>
      <PageHeader
        eyebrow="Controlled experiments"
        title="Fault lab"
        description="Inject the controlled fault scenarios used to evaluate DevNexa. Code faults ship inside a commit so CI can catch recurrences; infrastructure faults are injected at runtime."
        actions={<button className="btn-ghost" onClick={reset}><RotateCcw className="h-4 w-4" /> Clear all faults</button>}
      />
      <div className="grid gap-6 xl:grid-cols-4">
        <Card className="xl:col-span-1" title="Target environment" icon={FlaskConical}>
          {!env ? <Loading /> : (
            <>
              <div className="flex items-center justify-between"><span className="font-mono text-sm text-white">{env.version}</span>
                <Badge tone={env.healthy ? "green" : "red"} dot pulse>{env.healthy ? "Healthy" : "Degraded"}</Badge></div>
              <p className="mt-1 font-mono text-[11px] text-slate-500">{env.image}</p>
              <p className="mt-1 text-xs text-slate-500">{env.mode === "live" ? "Live docker-compose stack" : "Simulated orders-service"}</p>
              <p className="label mt-4">Active faults</p>
              {env.active_faults.length ? env.active_faults.map((f) => <Badge key={f} tone="red" className="mr-1">{titleCase(f)}</Badge>)
                : <p className="text-xs text-slate-500">None</p>}
              <p className="label mt-4">Health checks</p>
              <ul className="space-y-1.5">
                {env.health.map((c) => (
                  <li key={c.name} className="flex justify-between text-xs"><span className="text-slate-400">{c.name}</span>
                    <span className={`font-mono ${c.passed ? "text-emerald-300" : "text-rose-300"}`}>{typeof c.value === "number" ? +c.value.toFixed(3) : c.value}</span></li>
                ))}
              </ul>
              <p className="label mt-4">Recent events</p>
              <ul className="space-y-1">
                {[...env.events].reverse().slice(0, 6).map((e, i) => (
                  <li key={i} className="text-[11px] text-slate-400"><span className="text-slate-600">{clock(e.ts)}</span> {titleCase(e.event)} {e.fault || e.action || e.version || ""}</li>
                ))}
              </ul>
            </>
          )}
        </Card>
        <div className="grid gap-4 sm:grid-cols-2 xl:col-span-3 xl:grid-cols-3">
          {!faults ? <Loading /> : faults.map((f) => <Scenario key={f.id} f={f} onInject={inject} />)}
          <div className="card flex flex-col justify-center p-5 text-sm text-slate-400">
            <GitBranch className="mb-2 h-5 w-5 text-indigo-300" />
            Want to see a build or test failure explained instead? Push a commit with an injected CI defect from the Pipelines page.
          </div>
        </div>
      </div>
      <Toast toast={toast} />
    </>
  );
}
