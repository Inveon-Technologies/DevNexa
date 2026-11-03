import { Archive, Database, RotateCcw, Trash2, XCircle } from "lucide-react";
import { Link } from "react-router-dom";
import { api, operatorName } from "../api";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, Toast } from "../components/ui";
import { ago, time } from "../format";
import { usePoll, useToast } from "../hooks";

const actor = () => encodeURIComponent(operatorName());

function Row({ icon: Icon, title, detail, item, kind, act }) {
  return (
    <li className="flex flex-col gap-2 px-5 py-3 sm:flex-row sm:items-center">
      <Icon className="hidden h-4 w-4 shrink-0 text-slate-500 sm:block" />
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-2 text-sm text-slate-100">
          <Link to={`/live/${item.app}`} className="text-indigo-300 hover:underline">{item.app}</Link> {title}
        </p>
        <p className="truncate text-xs text-slate-500">{detail} · deleted {ago(item.trashed_at)} by {item.trashed_by} · gone for good {time(item.expires_at)}</p>
      </div>
      <div className="flex gap-1">
        <button className="btn-ghost py-1.5 text-xs" onClick={() => act(() => api.post(`/api/live/trash/${kind}/${item.id}/restore`, { actor: operatorName() }), "Restored from the trash")}>
          <RotateCcw className="h-3.5 w-3.5" /> Restore
        </button>
        <button className="btn-ghost px-2 py-1.5 text-xs text-rose-300" title="Delete permanently"
          onClick={() => window.confirm("Delete this permanently? It cannot be brought back.") && act(() => api.del(`/api/live/trash/${kind}/${item.id}?actor=${actor()}`), "Deleted permanently")}>
          <XCircle className="h-3.5 w-3.5" />
        </button>
      </div>
    </li>
  );
}

export default function Trash() {
  const { data, error, reload } = usePoll("/api/live/trash", 5000);
  const [toast, show] = useToast();
  const act = async (fn, msg) => { try { await fn(); show(msg, "success"); reload(); } catch (e) { show(e.message, "error"); } };
  const count = data ? data.backups.length + data.databases.length : 0;
  return (
    <>
      <PageHeader eyebrow="Data safety" title="Trash"
        description={`Deleted databases and backups (yours, and the ones smart retention pruned) wait here for ${data?.days ?? 14} days before they are removed for good. A deleted database keeps its volume, so restoring brings it back exactly as it was.`}
        actions={count > 0 && <button className="btn-ghost text-rose-300" onClick={() => window.confirm(`Permanently delete all ${count} items in the trash?`) && act(() => api.del(`/api/live/trash?actor=${actor()}`), "Trash emptied")}><Trash2 className="h-4 w-4" /> Empty trash</button>} />
      {error ? <ErrorBox error={error} /> : !data ? <Loading /> : !count ? (
        <Card><Empty title="The trash is empty" icon={Trash2}>Deleted databases and backups show up here.</Empty></Card>
      ) : (
        <div className="space-y-4">
          {data.databases.length > 0 && (
            <Card title="Databases" icon={Database} bodyClass="">
              <ul className="divide-y divide-white/[0.04]">
                {data.databases.map((d) => <Row key={d.id} kind="database" item={d} icon={Database} act={act}
                  title={<><b>{d.engine_title}</b> <Badge tone="amber">volume kept</Badge></>} detail={d.volume} />)}
              </ul>
            </Card>
          )}
          {data.backups.length > 0 && (
            <Card title="Backups" icon={Archive} bodyClass="">
              <ul className="divide-y divide-white/[0.04]">
                {data.backups.map((b) => <Row key={b.id} kind="backup" item={b} icon={Archive} act={act}
                  title={<><b>{b.label}</b> {b.trash_reason === "retention" && <Badge tone="cyan">pruned by retention</Badge>}</>}
                  detail={`${b.parts.map((p) => p.type === "db" ? `${p.engine} dump` : "/data files").join(" + ")} · ${b.size}`} />)}
              </ul>
            </Card>
          )}
        </div>
      )}
      <Toast toast={toast} />
    </>
  );
}
