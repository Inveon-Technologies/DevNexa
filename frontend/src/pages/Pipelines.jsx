import { GitBranch, GitCommitHorizontal, Play, X } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, operatorName } from "../api";
import StagePills from "../components/StagePills";
import { Badge, Card, Empty, ErrorBox, Loading, Meter, PageHeader, StatusBadge, Tabs } from "../components/ui";
import { ago, duration } from "../format";
import { usePoll } from "../hooks";

function TriggerDrawer({ onClose }) {
  const { data: catalog } = usePoll("/api/catalog", 0);
  const nav = useNavigate();
  const [form, setForm] = useState({
    message: "feat(orders): improve order listing",
    files: ["app/orders/routes.py"],
    branch: "main",
    strategy: "prioritized",
    defect: "",
  });
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const allFiles = catalog ? Object.values(catalog.modules).flat().filter((f) => !f.endsWith("/")).concat(["requirements.txt"]) : [];

  const toggle = async (f) => {
    const files = form.files.includes(f) ? form.files.filter((x) => x !== f) : [...form.files, f];
    setForm({ ...form, files });
    if (files.length) setPreview(await api.get(`/api/tests/prioritize?files=${encodeURIComponent(files.join(","))}`));
  };

  const submit = async () => {
    setBusy(true);
    try {
      const run = await api.post("/api/pipelines", { ...form, defect: form.defect || null, author: operatorName() });
      nav(`/pipelines/${run.id}`);
    } catch (e) {
      setErr(e.message);
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div className="relative flex h-full w-full max-w-xl flex-col border-l border-white/10 bg-ink-900 shadow-2xl">
        <div className="flex items-center justify-between border-b border-white/[0.06] px-6 py-4">
          <div>
            <h2 className="font-semibold text-white">Push a commit</h2>
            <p className="text-xs text-slate-400">Simulates a GitHub push; the agents take it from there.</p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white" aria-label="Close"><X className="h-5 w-5" /></button>
        </div>
        <div className="flex-1 space-y-5 overflow-y-auto px-6 py-5">
          <ErrorBox error={err} />
          <div>
            <label className="label">Commit message</label>
            <input className="input" value={form.message} onChange={(e) => setForm({ ...form, message: e.target.value })} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label">Branch</label>
              <input className="input" value={form.branch} onChange={(e) => setForm({ ...form, branch: e.target.value })} />
            </div>
            <div>
              <label className="label">Test strategy</label>
              <select className="input" value={form.strategy} onChange={(e) => setForm({ ...form, strategy: e.target.value })}>
                <option value="prioritized">Risk-prioritised (DevNexa)</option>
                <option value="full">Full suite (baseline)</option>
              </select>
            </div>
          </div>
          <div>
            <label className="label">Changed files</label>
            <div className="flex flex-wrap gap-1.5">
              {allFiles.map((f) => (
                <button key={f} onClick={() => toggle(f)}
                  className={`rounded-md px-2 py-1 font-mono text-[11.5px] ring-1 ring-inset transition ${
                    form.files.includes(f) ? "bg-indigo-500/20 text-indigo-200 ring-indigo-400/40" : "bg-white/[0.03] text-slate-400 ring-white/10 hover:text-slate-200"}`}>
                  {f}
                </button>
              ))}
            </div>
          </div>
          <div>
            <label className="label">Inject a defect (optional)</label>
            <select className="input" value={form.defect} onChange={(e) => setForm({ ...form, defect: e.target.value })}>
              <option value="">None, clean commit</option>
              <optgroup label="CI failures (AI feedback to developer)">
                {catalog?.ci_defects.map((d) => <option key={d.id} value={d.id}>{d.title} ({d.stage})</option>)}
              </optgroup>
            </select>
            <p className="mt-1.5 text-xs text-slate-500">Production faults that ship inside a commit are in the Fault lab.</p>
          </div>
          {preview && (
            <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
              <div className="flex items-center justify-between text-xs">
                <span className="text-slate-400">Change risk</span>
                <span className="font-mono text-slate-200">{preview.risk_score.toFixed(2)}</span>
              </div>
              <Meter value={preview.risk_score} tone={preview.risk_score > 0.5 ? "amber" : "green"} className="mt-1.5" />
              <p className="mt-3 text-xs text-slate-400">
                Test Agent will run <span className="text-white">{preview.selected.length}</span> of {preview.ranking.length} tests
                (~{preview.estimated_time_s}s vs {preview.full_suite_time_s}s full suite).
              </p>
              {preview.risk_factors.map((f) => <p key={f} className="mt-1 text-xs text-amber-300/90">• {f}</p>)}
            </div>
          )}
        </div>
        <div className="flex justify-end gap-2 border-t border-white/[0.06] px-6 py-4">
          <button className="btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={busy || !form.files.length} onClick={submit}><Play className="h-4 w-4" /> Run pipeline</button>
        </div>
      </div>
    </div>
  );
}

export default function Pipelines() {
  const { data, error } = usePoll("/api/pipelines", 2500);
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState("all");
  const runs = (data || []).filter((r) => tab === "all" || r.status === tab);
  return (
    <>
      <PageHeader
        eyebrow="Agentic CI/CD"
        title="Pipelines"
        description="Build, Test, Release and Deployment agents coordinated by a LangGraph workflow. Failures come back to the developer with an AI explanation."
        actions={<button className="btn-primary" onClick={() => setOpen(true)}><GitCommitHorizontal className="h-4 w-4" /> Push commit</button>}
      />
      <ErrorBox error={error} />
      <Card bodyClass="" title="Recent runs" action={
        <Tabs value={tab} onChange={setTab} tabs={[
          { value: "all", label: "All" }, { value: "running", label: "Running" },
          { value: "success", label: "Passed" }, { value: "failed", label: "Failed" }]} />}>
        {!data ? <Loading /> : !runs.length ? <Empty title="No pipeline runs" icon={GitBranch}>Push a commit to start one.</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="border-b border-white/[0.06]">
                <tr>
                  <th className="table-head">Run</th><th className="table-head">Commit</th><th className="table-head">Stages</th>
                  <th className="table-head">Tests</th><th className="table-head">Risk</th><th className="table-head">Status</th><th className="table-head">When</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/[0.04]">
                {runs.map((r) => (
                  <tr key={r.id} className="transition hover:bg-white/[0.02]">
                    <td className="table-cell"><Link className="font-mono text-indigo-300 hover:text-indigo-200" to={`/pipelines/${r.id}`}>#{r.id}</Link></td>
                    <td className="table-cell max-w-xs">
                      <Link to={`/pipelines/${r.id}`} className="block truncate text-slate-100 hover:text-white">{r.commit_message}</Link>
                      <p className="mt-0.5 text-xs text-slate-500">
                        <span className="font-mono">{r.commit_sha.slice(0, 7)}</span> · {r.branch} · {r.author} · <span className="text-slate-400">{r.trigger}</span>
                      </p>
                    </td>
                    <td className="table-cell"><StagePills stages={r.stages} /></td>
                    <td className="table-cell"><Badge tone={r.test_strategy === "full" ? "slate" : "indigo"}>{r.test_strategy}</Badge></td>
                    <td className="table-cell w-24"><span className="font-mono text-xs text-slate-300">{r.risk_score.toFixed(2)}</span><Meter value={r.risk_score} tone={r.risk_score > 0.5 ? "amber" : "green"} className="mt-1" /></td>
                    <td className="table-cell"><StatusBadge status={r.status} /></td>
                    <td className="table-cell whitespace-nowrap text-xs text-slate-400">{ago(r.created_at)}<br /><span className="text-slate-500">{duration(r.duration_s)}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {open && <TriggerDrawer onClose={() => setOpen(false)} />}
    </>
  );
}
