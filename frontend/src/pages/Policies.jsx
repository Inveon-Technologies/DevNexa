import { ShieldCheck, SlidersHorizontal } from "lucide-react";
import { api, operatorName } from "../api";
import { Card, Loading, PageHeader, RiskBadge, Toast, Toggle } from "../components/ui";
import { usePoll, useToast } from "../hooks";

const MATRIX = [
  { risk: "low", examples: "Re-run a failed job, restart a test container, clear an approved cache", behaviour: "Execute only if explicitly allowlisted; then verify health." },
  { risk: "medium", examples: "Roll back to the last stable image, adjust a non-critical configuration", behaviour: "Recommend with evidence; require the configured approval policy." },
  { risk: "high", examples: "Database/schema changes, security settings, infrastructure changes", behaviour: "Human approval required before execution." },
];

export default function Policies() {
  const { data, reload } = usePoll("/api/policies", 0);
  const [toast, show] = useToast();
  const patch = async (rule, change) => {
    try {
      await api.patch(`/api/policies/${rule.action_type}`, { ...change, actor: operatorName() });
      reload();
    } catch (e) { show(e.message, "error"); }
  };
  const setAuto = async (change) => {
    await api.put("/api/policies/autonomy", { ...change, actor: operatorName() });
    reload();
  };
  if (!data) return <Loading />;
  const a = data.autonomy;
  return (
    <>
      <PageHeader eyebrow="Graded autonomy" title="Risk policy"
        description="The policy layer decides whether a proposed action runs automatically, is recommended for approval, or must wait for an explicit human decision." />
      <div className="grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2" title="Risk-based remediation policy" icon={ShieldCheck} bodyClass="">
          <table className="w-full">
            <thead className="border-b border-white/[0.06]"><tr><th className="table-head">Risk level</th><th className="table-head">Illustrative actions</th><th className="table-head">DevNexa behaviour</th></tr></thead>
            <tbody className="divide-y divide-white/[0.04]">
              {MATRIX.map((m) => (
                <tr key={m.risk}><td className="table-cell"><RiskBadge risk={m.risk} /></td><td className="table-cell text-sm text-slate-300">{m.examples}</td><td className="table-cell text-sm text-slate-400">{m.behaviour}</td></tr>
              ))}
            </tbody>
          </table>
        </Card>
        <Card title="Autonomy settings" icon={SlidersHorizontal}>
          <div className="space-y-5">
            <label className="flex items-center justify-between gap-3"><span><span className="block text-sm text-slate-200">Auto-execute low-risk</span><span className="text-xs text-slate-500">Allowlisted actions only</span></span>
              <Toggle checked={a.auto_execute_low} onChange={(v) => setAuto({ auto_execute_low: v })} label="auto low" /></label>
            <label className="flex items-center justify-between gap-3"><span><span className="block text-sm text-slate-200">Auto-execute medium-risk</span><span className="text-xs text-slate-500">If the action is allowlisted by policy</span></span>
              <Toggle checked={a.auto_execute_medium} onChange={(v) => setAuto({ auto_execute_medium: v })} label="auto medium" /></label>
            <div>
              <div className="flex justify-between text-sm"><span className="text-slate-200">Minimum diagnosis confidence</span><span className="font-mono text-xs text-slate-400">{a.min_confidence_for_auto.toFixed(2)}</span></div>
              <input type="range" min="0" max="1" step="0.05" value={a.min_confidence_for_auto} onChange={(e) => setAuto({ min_confidence_for_auto: parseFloat(e.target.value) })} className="mt-2 w-full accent-indigo-400" />
              <p className="text-xs text-slate-500">Below this, even allowlisted actions wait for approval.</p>
            </div>
            <p className="rounded-lg bg-white/[0.03] p-3 text-xs text-slate-400">High-risk actions can never be allowlisted.</p>
          </div>
        </Card>
      </div>
      <Card className="mt-6" bodyClass="" title="Action allowlist">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead className="border-b border-white/[0.06]"><tr><th className="table-head">Action</th><th className="table-head">Risk</th><th className="table-head">Allowlisted</th><th className="table-head">Enabled</th></tr></thead>
            <tbody className="divide-y divide-white/[0.04]">
              {data.rules.map((r) => (
                <tr key={r.action_type}>
                  <td className="table-cell"><p className="text-slate-100">{r.title}</p><p className="text-xs text-slate-500">{r.description}</p></td>
                  <td className="table-cell"><RiskBadge risk={r.risk_level} /></td>
                  <td className="table-cell"><Toggle checked={r.allowlisted} disabled={r.risk_level === "high"} onChange={(v) => patch(r, { allowlisted: v })} label="allowlisted" /></td>
                  <td className="table-cell"><Toggle checked={r.enabled} onChange={(v) => patch(r, { enabled: v })} label="enabled" /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Toast toast={toast} />
    </>
  );
}
