import {
  ArrowLeft, BellRing, Brain, CheckCircle2, CircleDashed, Database, Gavel, HeartPulse, Library, Loader2,
  RefreshCw, Search, Wrench, XCircle,
} from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { api, operatorName } from "../api";
import ActionCard from "../components/ActionCard";
import Evidence from "../components/Evidence";
import { Badge, Card, ErrorBox, Loading, Meter, PageHeader, StatusBadge, Toast } from "../components/ui";
import { clock, duration, titleCase } from "../format";
import { usePoll, useToast } from "../hooks";

const STEPS = [
  { key: "alert", label: "Alert", icon: BellRing },
  { key: "collect_evidence", label: "Evidence", icon: Search },
  { key: "retrieve_similar", label: "Retrieve", icon: Database },
  { key: "diagnose", label: "Diagnose", icon: Brain },
  { key: "policy", label: "Policy", icon: Gavel },
  { key: "execute", label: "Remediate", icon: Wrench },
  { key: "verify", label: "Verify", icon: HeartPulse },
  { key: "learn", label: "Learn", icon: Library },
];

function Stepper({ workflow, status }) {
  const last = {};
  workflow.forEach((w) => { last[w.step] = w.status; });
  return (
    <div className="flex items-center overflow-x-auto pb-1">
      {STEPS.map((s, i) => {
        const st = last[s.key];
        const waiting = s.key === "execute" && status === "awaiting_approval";
        const cls = st === "done" ? "bg-emerald-500/15 text-emerald-300 ring-emerald-400/30"
          : st === "failed" ? "bg-rose-500/15 text-rose-300 ring-rose-400/30"
          : st === "running" ? "bg-sky-500/15 text-sky-300 ring-sky-400/30"
          : waiting ? "bg-amber-500/15 text-amber-300 ring-amber-400/30"
          : "bg-white/[0.03] text-slate-500 ring-white/10";
        return (
          <div key={s.key} className="flex items-center">
            <div className="flex flex-col items-center gap-1.5 px-1">
              <div className={`grid h-10 w-10 place-items-center rounded-xl ring-1 ring-inset ${cls}`}>
                {st === "running" ? <Loader2 className="h-4 w-4 animate-spin" /> : <s.icon className="h-4 w-4" />}
              </div>
              <span className="text-[11px] text-slate-400">{waiting ? "Awaiting" : s.label}</span>
            </div>
            {i < STEPS.length - 1 && <div className={`mb-5 h-px w-6 sm:w-10 ${st === "done" ? "bg-emerald-400/40" : "bg-white/10"}`} />}
          </div>
        );
      })}
    </div>
  );
}

function DiagnosisCard({ d }) {
  return (
    <div className={`rounded-xl border p-4 ${d.outcome === "rejected" ? "border-rose-500/20 bg-rose-500/[0.04] opacity-75" : "border-indigo-400/20 bg-indigo-500/[0.05]"}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="indigo">Attempt {d.attempt}</Badge>
        <span className="font-semibold text-white">{d.title}</span>
        <StatusBadge status={d.outcome === "pending" ? "pending" : d.outcome} label={d.outcome === "pending" ? "Unverified" : titleCase(d.outcome)} />
        <span className="ml-auto text-xs text-slate-500">{d.provider}{d.used_rag ? " + RAG" : ""}</span>
      </div>
      <p className="mt-3 text-sm leading-relaxed text-slate-200">{d.root_cause}</p>
      <div className="mt-4 grid gap-4 sm:grid-cols-3">
        <div>
          <p className="label">Confidence</p>
          <div className="flex items-center gap-2">
            <Meter value={d.confidence} tone={d.confidence > 0.7 ? "green" : "amber"} />
            <span className="font-mono text-xs">{Math.round(d.confidence * 100)}%</span>
          </div>
        </div>
        <div><p className="label">Affected component</p><p className="font-mono text-xs text-slate-200">{d.affected_component}</p></div>
        <div><p className="label">Proposed action</p><p className="text-xs text-slate-200">{titleCase(d.proposed_action)}</p></div>
      </div>
      <p className="label mt-4">Reasoning</p>
      <p className="text-sm text-slate-300">{d.reasoning}</p>
      <p className="label mt-4">Uncertainty</p>
      <p className="text-sm text-amber-200/90">{d.uncertainty}</p>
      {d.supporting_evidence?.length > 0 && <>
        <p className="label mt-4">Supporting evidence</p>
        <ul className="space-y-1.5">
          {d.supporting_evidence.map((e, i) => (
            <li key={i} className="flex gap-2 text-xs">
              <Badge tone={e.kind === "similar_incident" ? "violet" : e.kind === "log" ? "slate" : "cyan"}>{titleCase(e.kind)}</Badge>
              <span className="min-w-0 break-words font-mono text-slate-300">{e.detail}</span>
            </li>
          ))}
        </ul>
      </>}
      {d.alternatives?.length > 0 && <>
        <p className="label mt-4">Alternatives considered</p>
        <div className="flex flex-wrap gap-2">
          {d.alternatives.map((a) => <Badge key={a.cause_id} tone="slate">{a.title} · {a.probability != null ? `${Math.round(a.probability * 100)}%` : "?"}</Badge>)}
        </div>
      </>}
    </div>
  );
}

export default function IncidentDetail() {
  const { id } = useParams();
  const { data: i, error, reload } = usePoll(`/api/incidents/${id}`, 1500);
  const [toast, show] = useToast();
  if (!i) return error ? <ErrorBox error={error} /> : <Loading />;
  const latest = i.diagnoses[i.diagnoses.length - 1];
  const similar = latest?.similar_incidents || [];
  const closed = ["resolved", "closed"].includes(i.status);
  const reanalyze = async () => {
    await api.post(`/api/incidents/${i.id}/reanalyze`, { actor: operatorName() });
    show("Re-analysis started; the current diagnosis was marked rejected.", "info");
    reload();
  };
  const close = async () => {
    await api.post(`/api/incidents/${i.id}/close?actor=${encodeURIComponent(operatorName())}`);
    reload();
  };
  return (
    <>
      <Link to="/incidents" className="mb-4 inline-flex items-center gap-1 text-sm text-slate-400 hover:text-white"><ArrowLeft className="h-4 w-4" /> Incidents</Link>
      <PageHeader
        eyebrow={`Incident #${i.id} · ${i.service}`}
        title={i.title}
        description={i.summary}
        actions={<>
          <Badge tone={i.severity === "critical" ? "red" : "amber"}>{i.severity}</Badge>
          <StatusBadge status={i.status} />
          {!closed && <button className="btn-ghost" onClick={reanalyze}><RefreshCw className="h-4 w-4" /> Re-analyse</button>}
          {!closed && <button className="btn-ghost" onClick={close}>Close manually</button>}
        </>}
      />

      <Card className="mb-6" bodyClass="px-5 py-5">
        <Stepper workflow={i.workflow} status={i.status} />
        <div className="mt-4 grid grid-cols-2 gap-4 border-t border-white/[0.06] pt-4 text-sm sm:grid-cols-4">
          <div><p className="label">Detection</p><p className="text-slate-200">{duration(i.detection_s)}</p></div>
          <div><p className="label">Time to recovery</p><p className="text-slate-200">{duration(i.mttr_s)}</p></div>
          <div><p className="label">Diagnosis attempts</p><p className="text-slate-200">{i.attempts}</p></div>
          <div><p className="label">Human interventions</p><p className="text-slate-200">{i.human_interventions}</p></div>
        </div>
      </Card>

      <div className="grid gap-6 xl:grid-cols-3">
        <div className="min-w-0 space-y-6 xl:col-span-2">
          <Card title="Diagnosis" subtitle="LLM reasoning grounded in collected evidence and retrieved verified incidents" icon={Brain}>
            {i.diagnoses.length ? (
              <div className="space-y-3">{[...i.diagnoses].reverse().map((d) => <DiagnosisCard key={d.id} d={d} />)}</div>
            ) : <p className="text-sm text-slate-500">The Incident Agent is analysing evidence…</p>}
          </Card>
          <Card title="Remediation" subtitle="Graded autonomy: low-risk allowlisted actions run automatically, others wait for approval" icon={Wrench}>
            {i.actions.length ? (
              <div className="space-y-3">
                {[...i.actions].reverse().map((a) => <ActionCard key={a.id} action={a} onDone={(v) => { show(v === "approve" ? "Approved. Executing and verifying…" : "Rejected.", v === "approve" ? "success" : "info"); reload(); }} />)}
              </div>
            ) : <p className="text-sm text-slate-500">No action proposed yet.</p>}
          </Card>
          <Card title="Evidence" subtitle="Collected by the Incident Agent at investigation time" icon={Search}>
            <Evidence items={i.evidence} />
          </Card>
        </div>
        <div className="min-w-0 space-y-6">
          <Card title="Similar verified incidents" subtitle="Retrieved from pgvector incident memory" icon={Database}>
            {similar.length ? (
              <ul className="space-y-3">
                {similar.map((s) => (
                  <li key={s.knowledge_id} className="rounded-lg border border-white/[0.06] bg-white/[0.02] p-3">
                    <div className="flex items-center justify-between gap-2">
                      <Link to={s.incident_id ? `/incidents/${s.incident_id}` : "/knowledge"} className="truncate text-sm text-slate-200 hover:text-white">{s.title}</Link>
                      <span className="font-mono text-xs text-violet-300">{s.similarity.toFixed(2)}</span>
                    </div>
                    <Meter value={s.similarity} tone="indigo" className="mt-2" />
                    <p className="mt-2 text-xs text-slate-500">Fixed by {titleCase(s.action_type)} · {s.affected_module}</p>
                  </li>
                ))}
              </ul>
            ) : <p className="text-sm text-slate-500">{latest ? "No sufficiently similar verified incident in memory." : "Retrieving…"}</p>}
          </Card>
          <Card title="Health verification" icon={HeartPulse}>
            {i.health_checks.length ? i.health_checks.map((h) => (
              <div key={h.id} className="mb-4 last:mb-0">
                <div className="mb-2 flex items-center gap-2 text-sm">
                  {h.passed ? <CheckCircle2 className="h-4 w-4 text-emerald-400" /> : <XCircle className="h-4 w-4 text-rose-400" />}
                  <span className={h.passed ? "text-emerald-200" : "text-rose-200"}>{h.passed ? "Recovery verified" : "Verification failed"}</span>
                  <span className="ml-auto text-xs text-slate-500">{clock(h.checked_at)}</span>
                </div>
                <ul className="space-y-1">
                  {h.checks.map((c) => (
                    <li key={c.name} className="flex justify-between text-xs">
                      <span className="text-slate-400">{c.name}</span>
                      <span className={`font-mono ${c.passed ? "text-emerald-300" : "text-rose-300"}`}>{typeof c.value === "number" ? +c.value.toFixed(3) : c.value}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )) : <p className="text-sm text-slate-500">Runs after a remediation action executes.</p>}
          </Card>
          {i.knowledge.length > 0 && (
            <Card title="Learned" subtitle="Feedback Engine output" icon={Library}>
              {i.knowledge.map((k) => (
                <div key={k.id} className="space-y-1.5 text-xs">
                  <p className="text-slate-300">Stored as verified case <span className="font-mono text-violet-300">#{k.id}</span></p>
                  <p className="text-slate-400">Module risk raised: <Badge tone="amber">{k.affected_module}</Badge></p>
                  <p className="text-slate-400">Regression test added:</p>
                  <p className="font-mono text-cyan-300">{k.regression_test}</p>
                </div>
              ))}
            </Card>
          )}
          <Card title="Workflow log" icon={CircleDashed} bodyClass="px-5 py-4">
            <ol className="relative space-y-3 border-l border-white/10 pl-4">
              {i.workflow.map((w, idx) => (
                <li key={idx} className="relative">
                  <span className={`absolute -left-[21px] top-1.5 h-2 w-2 rounded-full ${w.status === "done" ? "bg-emerald-400" : w.status === "failed" ? "bg-rose-400" : w.status === "running" ? "bg-sky-400" : "bg-slate-500"}`} />
                  <p className="text-xs text-slate-500">{clock(w.ts)} · {titleCase(w.step)}</p>
                  <p className="text-[13px] text-slate-300">{w.detail}</p>
                </li>
              ))}
            </ol>
          </Card>
        </div>
      </div>
      <Toast toast={toast} />
    </>
  );
}
