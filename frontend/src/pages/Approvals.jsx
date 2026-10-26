import { UserCheck } from "lucide-react";
import { Link } from "react-router-dom";
import ActionCard from "../components/ActionCard";
import { Badge, Card, Empty, Loading, PageHeader, StatusBadge, Toast } from "../components/ui";
import { ago, titleCase } from "../format";
import { usePoll, useToast } from "../hooks";

export default function Approvals() {
  const { data: pending, reload } = usePoll("/api/actions?status=pending_approval", 2500);
  const { data: all } = usePoll("/api/actions", 4000);
  const [toast, show] = useToast();
  const decided = (all || []).filter((a) => a.decided_by && a.status !== "pending_approval").slice(0, 15);
  return (
    <>
      <PageHeader
        eyebrow="Human in the loop"
        title="Approvals"
        description="Medium-risk actions are recommended with evidence and wait for the configured policy; high-risk actions always need an explicit human decision."
      />
      <div className="grid gap-6 xl:grid-cols-3">
        <div className="space-y-4 xl:col-span-2">
          {!pending ? <Loading /> : !pending.length ? (
            <Card><Empty title="Nothing waiting for you" icon={UserCheck}>Actions that need approval show up here with the evidence behind them.</Empty></Card>
          ) : pending.map((a) => (
            <Card key={a.id} title={<Link to={`/incidents/${a.incident.id}`} className="hover:text-indigo-200">Incident #{a.incident.id} · {a.incident.title}</Link>}
                  subtitle={`Requested ${ago(a.requested_at)}`} action={<Badge tone={a.incident.severity === "critical" ? "red" : "amber"}>{a.incident.severity}</Badge>}>
              {a.diagnosis && (
                <div className="mb-4 rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
                  <div className="flex items-center justify-between">
                    <p className="text-sm font-medium text-slate-100">{a.diagnosis.title}</p>
                    <span className="font-mono text-xs text-slate-400">{Math.round(a.diagnosis.confidence * 100)}% confidence</span>
                  </div>
                  <p className="mt-1 text-sm text-slate-400">{a.diagnosis.root_cause}</p>
                  <ul className="mt-3 space-y-1">
                    {a.diagnosis.supporting_evidence.map((e, i) => (
                      <li key={i} className="font-mono text-[11.5px] text-slate-400">• {e.detail}</li>
                    ))}
                  </ul>
                  <p className="mt-2 text-xs text-amber-200/80">{a.diagnosis.uncertainty}</p>
                </div>
              )}
              <ActionCard action={a} onDone={(v) => { show(v === "approve" ? "Approved. The Incident Agent is executing and verifying." : "Rejected and logged.", v === "approve" ? "success" : "info"); reload(); }} />
            </Card>
          ))}
        </div>
        <Card title="Recent decisions" bodyClass="px-2 py-2">
          <ul className="divide-y divide-white/[0.04]">
            {decided.map((a) => (
              <li key={a.id} className="px-3 py-3">
                <div className="flex items-center justify-between gap-2">
                  <p className="truncate text-sm text-slate-200">{a.title}</p>
                  <StatusBadge status={a.status} />
                </div>
                <p className="mt-0.5 text-xs text-slate-500">#{a.incident.id} · {titleCase(a.risk_level)} risk · by {a.decided_by} · {ago(a.decided_at)}</p>
              </li>
            ))}
            {!decided.length && <p className="px-3 py-4 text-sm text-slate-500">No decisions yet.</p>}
          </ul>
        </Card>
      </div>
      <Toast toast={toast} />
    </>
  );
}
