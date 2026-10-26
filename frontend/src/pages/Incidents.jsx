import { Siren } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Card, Empty, ErrorBox, Loading, Meter, PageHeader, StatusBadge, Tabs } from "../components/ui";
import { ago, duration } from "../format";
import { usePoll } from "../hooks";

const OPEN = ["open", "investigating", "diagnosed", "awaiting_approval", "remediating", "verifying"];

export default function Incidents() {
  const { data, error } = usePoll("/api/incidents", 2500);
  const [tab, setTab] = useState("all");
  const list = data || [];
  const filtered = list.filter((i) =>
    tab === "all" ? true : tab === "open" ? OPEN.includes(i.status) : i.status === tab);
  const count = (f) => list.filter(f).length;
  return (
    <>
      <PageHeader
        eyebrow="AI incident handling"
        title="Incidents"
        description="Alerts open incidents; the Incident Agent collects evidence, retrieves similar verified cases, diagnoses with LLM + RAG, and acts within the risk policy."
        actions={<Link to="/fault-lab" className="btn-primary">Inject a controlled fault</Link>}
      />
      <ErrorBox error={error} />
      <Card bodyClass="" title="All incidents" action={
        <Tabs value={tab} onChange={setTab} tabs={[
          { value: "all", label: "All", count: list.length },
          { value: "open", label: "Open", count: count((i) => OPEN.includes(i.status)) },
          { value: "awaiting_approval", label: "Needs approval", count: count((i) => i.status === "awaiting_approval") },
          { value: "resolved", label: "Resolved", count: count((i) => i.status === "resolved") },
          { value: "escalated", label: "Escalated", count: count((i) => i.status === "escalated") },
        ]} />}>
        {!data ? <Loading /> : !filtered.length ? <Empty title="No incidents here" icon={Siren}>Inject a fault from the Fault lab to watch the loop.</Empty> : (
          <ul className="divide-y divide-white/[0.04]">
            {filtered.map((i) => (
              <li key={i.id}>
                <Link to={`/incidents/${i.id}`} className="grid gap-3 px-5 py-4 transition hover:bg-white/[0.02] md:grid-cols-12 md:items-center">
                  <div className="md:col-span-5">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs text-slate-500">#{i.id}</span>
                      <Badge tone={i.severity === "critical" ? "red" : "amber"}>{i.severity}</Badge>
                      <span className="truncate font-medium text-slate-100">{i.title}</span>
                    </div>
                    <p className="mt-1 truncate text-xs text-slate-500">{i.summary}</p>
                  </div>
                  <div className="md:col-span-3">
                    {i.diagnosis ? (
                      <>
                        <p className="text-sm text-slate-200">{i.diagnosis.title}</p>
                        <div className="mt-1 flex items-center gap-2">
                          <Meter value={i.diagnosis.confidence} className="w-24" tone={i.diagnosis.confidence > 0.7 ? "green" : "amber"} />
                          <span className="font-mono text-[11px] text-slate-400">{Math.round(i.diagnosis.confidence * 100)}%</span>
                        </div>
                      </>
                    ) : <span className="text-xs text-slate-500">Diagnosing…</span>}
                  </div>
                  <div className="text-xs text-slate-400 md:col-span-2">
                    <p>MTTR {duration(i.mttr_s)}</p>
                    <p className="text-slate-500">{i.attempts} attempt{i.attempts === 1 ? "" : "s"} · {i.human_interventions} human step{i.human_interventions === 1 ? "" : "s"}</p>
                  </div>
                  <div className="flex items-center justify-between gap-2 md:col-span-2 md:justify-end">
                    <StatusBadge status={i.status} />
                    <span className="text-xs text-slate-500">{ago(i.detected_at)}</span>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}
