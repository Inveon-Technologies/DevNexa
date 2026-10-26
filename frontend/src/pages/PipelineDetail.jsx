import { ArrowLeft, Bot, ChevronDown, ChevronRight, RotateCcw, Sparkles } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { Badge, Card, ErrorBox, Loading, Meter, PageHeader, StatusBadge, StatusIcon } from "../components/ui";
import { clock, duration, time } from "../format";
import { usePoll } from "../hooks";

function Stage({ s }) {
  const [open, setOpen] = useState(s.status === "failed");
  const dur = s.started_at && s.finished_at ? (new Date(s.finished_at) - new Date(s.started_at)) / 1000 : null;
  return (
    <div className="rounded-xl border border-white/[0.06] bg-white/[0.02]">
      <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-3 px-4 py-3 text-left">
        <StatusIcon status={s.status} className="h-5 w-5" />
        <div className="flex-1">
          <p className="text-sm font-medium capitalize text-slate-100">{s.name}</p>
          <p className="text-xs text-slate-500">{s.agent}{s.started_at && ` · started ${clock(s.started_at)}`}{dur !== null && ` · ${duration(dur)}`}</p>
        </div>
        <StatusBadge status={s.status} />
        {open ? <ChevronDown className="h-4 w-4 text-slate-500" /> : <ChevronRight className="h-4 w-4 text-slate-500" />}
      </button>
      {open && (
        <pre className="mx-4 mb-4 max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-black/40 p-3 font-mono text-[12px] leading-relaxed text-slate-300 ring-1 ring-white/5">
          {s.logs || "No output yet."}
        </pre>
      )}
    </div>
  );
}

export default function PipelineDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const { data: r, error } = usePoll(`/api/pipelines/${id}`, 1500);
  if (!r) return error ? <ErrorBox error={error} /> : <Loading />;
  const selected = r.tests.filter((t) => t.selected);
  const testStage = r.stages.find((s) => s.name === "test");
  const rerun = async () => nav(`/pipelines/${(await api.post(`/api/pipelines/${r.id}/rerun`)).id}`);
  return (
    <>
      <Link to="/pipelines" className="mb-4 inline-flex items-center gap-1 text-sm text-slate-400 hover:text-white"><ArrowLeft className="h-4 w-4" /> Pipelines</Link>
      <PageHeader
        eyebrow={`Pipeline #${r.id} · ${r.trigger}`}
        title={r.commit_message}
        description={<span><span className="font-mono">{r.commit_sha.slice(0, 10)}</span> on {r.branch} by {r.author} · {time(r.created_at)}</span>}
        actions={<><StatusBadge status={r.status} /><button className="btn-ghost" onClick={rerun}><RotateCcw className="h-4 w-4" /> Re-run</button></>}
      />
      {r.ai_feedback && (
        <div className="mb-6 flex gap-3 rounded-2xl border border-violet-400/25 bg-gradient-to-r from-violet-500/10 to-indigo-500/5 p-5">
          <div className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-violet-500/20"><Sparkles className="h-4 w-4 text-violet-200" /></div>
          <div>
            <p className="text-sm font-semibold text-violet-100">AI feedback for {r.author}</p>
            <p className="mt-1 whitespace-pre-line text-sm leading-relaxed text-slate-300">{r.ai_feedback}</p>
          </div>
        </div>
      )}
      <div className="grid gap-6 xl:grid-cols-3">
        <div className="space-y-3 xl:col-span-2">
          {r.stages.map((s) => <Stage key={s.id} s={s} />)}
        </div>
        <div className="space-y-6">
          <Card title="Change analysis" icon={Bot}>
            <div className="flex justify-between text-xs"><span className="text-slate-400">Risk score</span><span className="font-mono">{r.risk_score.toFixed(2)}</span></div>
            <Meter value={r.risk_score} tone={r.risk_score > 0.5 ? "amber" : "green"} className="mt-1.5" />
            <p className="label mt-4">Changed files</p>
            <ul className="space-y-1">{r.changed_files.map((f) => <li key={f} className="font-mono text-xs text-slate-300">{f}</li>)}</ul>
            <p className="label mt-4">Modules</p>
            <div className="flex flex-wrap gap-1">{r.modules.map((m) => <Badge key={m} tone="indigo">{m}</Badge>)}</div>
            {r.risk_factors.length > 0 && <>
              <p className="label mt-4">Risk factors</p>
              {r.risk_factors.map((f) => <p key={f} className="text-xs text-amber-300/90">• {f}</p>)}
            </>}
          </Card>
          {r.deployment && (
            <Card title="Deployment">
              <div className="flex items-center justify-between"><span className="font-mono text-sm">{r.deployment.version}</span><StatusBadge status={r.deployment.status} /></div>
              <p className="mt-1 font-mono text-xs text-slate-500">{r.deployment.image}</p>
            </Card>
          )}
        </div>
      </div>
      {r.tests.length > 0 && (
        <Card className="mt-6" bodyClass="" title="Test prioritisation"
              subtitle={`${selected.length} of ${r.tests.length} tests run · ${testStage?.details?.duration_s ?? "—"}s vs ${testStage?.details?.full_suite_s ?? "—"}s full suite`}>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="border-b border-white/[0.06]"><tr>
                <th className="table-head">#</th><th className="table-head">Test</th><th className="table-head">Module</th>
                <th className="table-head">Score</th><th className="table-head">Why</th><th className="table-head">Result</th></tr></thead>
              <tbody className="divide-y divide-white/[0.04]">
                {r.tests.map((t) => (
                  <tr key={t.id} className={t.selected ? "" : "opacity-50"}>
                    <td className="table-cell text-xs text-slate-500">{t.order + 1}</td>
                    <td className="table-cell font-mono text-xs text-slate-200">{t.test_id}{t.message && <p className="mt-1 whitespace-pre-wrap font-mono text-[11px] text-rose-300">{t.message}</p>}</td>
                    <td className="table-cell"><Badge>{t.module}</Badge></td>
                    <td className="table-cell font-mono text-xs">{t.priority_score.toFixed(2)}</td>
                    <td className="table-cell text-xs text-slate-400">{t.reasons.join(" · ")}</td>
                    <td className="table-cell"><StatusBadge status={t.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}
