import { ScrollText } from "lucide-react";
import { useState } from "react";
import { Badge, Card, Loading, PageHeader, Tabs } from "../components/ui";
import { time } from "../format";
import { usePoll } from "../hooks";

const TYPES = ["all", "incident", "remediation_action", "pipeline_run", "deployment", "knowledge", "policy", "environment", "experiment"];

export default function Audit() {
  const [type, setType] = useState("all");
  const { data } = usePoll(`/api/audit?limit=300${type !== "all" ? `&entity_type=${type}` : ""}`, 4000, [type]);
  return (
    <>
      <PageHeader eyebrow="Governance" title="Audit log"
        description="Every agent decision, policy outcome, approval, execution and verification is recorded with its actor." />
      <Card bodyClass="" icon={ScrollText} title="Events" action={<Tabs value={type} onChange={setType} tabs={TYPES.map((t) => ({ value: t, label: t === "all" ? "All" : t.replace("_", " ") }))} />}>
        {!data ? <Loading /> : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="border-b border-white/[0.06]"><tr><th className="table-head">Time</th><th className="table-head">Actor</th><th className="table-head">Event</th><th className="table-head">Entity</th><th className="table-head">Message</th></tr></thead>
              <tbody className="divide-y divide-white/[0.04]">
                {data.map((a) => (
                  <tr key={a.id}>
                    <td className="table-cell whitespace-nowrap text-xs text-slate-400">{time(a.ts)}</td>
                    <td className="table-cell"><Badge tone={a.actor.includes("agent") || a.actor.includes("engine") || a.actor === "policy-engine" ? "violet" : "slate"}>{a.actor}</Badge></td>
                    <td className="table-cell font-mono text-xs text-slate-300">{a.action}</td>
                    <td className="table-cell text-xs text-slate-400">{a.entity_type}{a.entity_id && ` #${a.entity_id}`}</td>
                    <td className="table-cell text-sm text-slate-300">{a.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
