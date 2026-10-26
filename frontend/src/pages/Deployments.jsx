import { Rocket, Undo2 } from "lucide-react";
import { Link } from "react-router-dom";
import { api, operatorName } from "../api";
import { Badge, Card, Loading, PageHeader, StatusBadge, Toast } from "../components/ui";
import { ago, time } from "../format";
import { usePoll, useToast } from "../hooks";

export default function Deployments() {
  const { data, reload } = usePoll("/api/deployments", 3000);
  const [toast, show] = useToast();
  const rollback = async (d) => {
    if (!window.confirm(`Roll back orders-service to ${d.version}?`)) return;
    try {
      await api.post(`/api/deployments/${d.id}/rollback?actor=${encodeURIComponent(operatorName())}`);
      show(`Rolled back to ${d.version}`, "success");
      reload();
    } catch (e) { show(e.message, "error"); }
  };
  return (
    <>
      <PageHeader eyebrow="Deployment Agent" title="Deployments"
        description="Every release of orders-service with its health and stability. The Incident Agent rolls back to the most recent stable image when a release causes an incident." />
      <Card bodyClass="" title="History" icon={Rocket}>
        {!data ? <Loading /> : (
          <ol className="divide-y divide-white/[0.04]">
            {data.map((d) => (
              <li key={d.id} className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-center">
                <div className={`grid h-9 w-9 shrink-0 place-items-center rounded-xl ring-1 ring-inset ${d.status === "active" ? "bg-emerald-500/10 ring-emerald-400/30" : "bg-white/[0.03] ring-white/10"}`}>
                  <Rocket className={`h-4 w-4 ${d.status === "active" ? "text-emerald-300" : "text-slate-500"}`} />
                </div>
                <div className="flex-1">
                  <p className="flex items-center gap-2 font-medium text-slate-100">{d.version} {d.is_stable && <Badge tone="green">stable</Badge>}</p>
                  <p className="font-mono text-xs text-slate-500">{d.image} · {d.commit_sha.slice(0, 7)}</p>
                  <p className="text-xs text-slate-500">{time(d.deployed_at)} ({ago(d.deployed_at)}) {d.run_id && <>· <Link className="text-indigo-300" to={`/pipelines/${d.run_id}`}>pipeline #{d.run_id}</Link></>}</p>
                </div>
                <div className="flex items-center gap-2">
                  <StatusBadge status={d.health} />
                  <StatusBadge status={d.status} />
                  {d.status !== "active" && d.status !== "failed" && <button className="btn-ghost py-1.5 text-xs" onClick={() => rollback(d)}><Undo2 className="h-3.5 w-3.5" /> Roll back here</button>}
                </div>
              </li>
            ))}
          </ol>
        )}
      </Card>
      <Toast toast={toast} />
    </>
  );
}
