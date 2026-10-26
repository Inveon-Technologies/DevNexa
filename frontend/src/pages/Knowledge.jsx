import { BookOpenCheck, Search } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, operatorName } from "../api";
import { Badge, Card, Empty, Loading, Meter, PageHeader, Toggle } from "../components/ui";
import { duration, time, titleCase } from "../format";
import { usePoll } from "../hooks";

export default function Knowledge() {
  const { data, reload } = usePoll("/api/knowledge", 5000);
  const [q, setQ] = useState("TLS handshake failed, upstream auth errors after 3 retries");
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const search = async (e) => {
    e?.preventDefault();
    setBusy(true);
    try { setRes(await api.get(`/api/knowledge/search?q=${encodeURIComponent(q)}`)); } finally { setBusy(false); }
  };
  const toggle = async (k) => {
    await api.patch(`/api/knowledge/${k.id}`, { verified: !k.verified, actor: operatorName() });
    reload();
  };
  return (
    <>
      <PageHeader
        eyebrow="Feedback Engine"
        title="Incident memory"
        description="Only incidents whose fix passed health verification are stored. Each case is embedded for semantic retrieval and feeds module risk and regression-test recommendations."
      />
      <Card title="Semantic search" subtitle="Query the same index the Incident Agent uses for RAG" icon={Search}>
        <form onSubmit={search} className="flex flex-col gap-2 sm:flex-row">
          <input className="input flex-1" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Describe symptoms, paste a log line…" />
          <button className="btn-primary" disabled={busy}>Search</button>
        </form>
        {res && (
          <ul className="mt-4 space-y-2">
            {res.results.map((r) => (
              <li key={r.knowledge_id} className="grid gap-3 rounded-lg border border-white/[0.06] bg-white/[0.02] p-3 sm:grid-cols-12 sm:items-center">
                <div className="sm:col-span-6">
                  <p className="text-sm text-slate-100">{r.title}</p>
                  <p className="text-xs text-slate-500">{r.root_cause}</p>
                </div>
                <div className="text-xs text-slate-400 sm:col-span-3">Fix: {titleCase(r.action_type)}</div>
                <div className="sm:col-span-3"><div className="flex justify-between text-xs"><span className="text-slate-500">similarity</span><span className="font-mono text-violet-300">{r.similarity.toFixed(3)}</span></div><Meter value={r.similarity} className="mt-1" /></div>
              </li>
            ))}
            <p className="text-[11px] text-slate-500">Embedder: {res.embedder}</p>
          </ul>
        )}
      </Card>
      <Card className="mt-6" bodyClass="" title="Verified incidents" subtitle={`${data?.length ?? "…"} cases`} icon={BookOpenCheck}>
        {!data ? <Loading /> : !data.length ? <Empty title="Memory is empty">Resolve an incident to store its verified knowledge.</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="border-b border-white/[0.06]"><tr>
                <th className="table-head">Case</th><th className="table-head">Confirmed cause</th><th className="table-head">Fix</th>
                <th className="table-head">Regression test</th><th className="table-head">MTTR</th><th className="table-head">Used</th><th className="table-head">Verified</th></tr></thead>
              <tbody className="divide-y divide-white/[0.04]">
                {data.map((k) => (
                  <tr key={k.id}>
                    <td className="table-cell">
                      <p className="text-slate-100">{k.title}</p>
                      <p className="text-xs text-slate-500">{k.incident_id && <Link className="text-indigo-300" to={`/incidents/${k.incident_id}`}>incident #{k.incident_id}</Link>} · {time(k.created_at)}</p>
                    </td>
                    <td className="table-cell max-w-sm text-xs text-slate-300">{k.root_cause}<div className="mt-1"><Badge tone="indigo">{k.affected_module}</Badge></div></td>
                    <td className="table-cell text-xs">{titleCase(k.action_type)}</td>
                    <td className="table-cell font-mono text-[11px] text-cyan-300">{k.regression_test}</td>
                    <td className="table-cell text-xs">{duration(k.mttr_s)}</td>
                    <td className="table-cell text-xs">{k.times_retrieved}×</td>
                    <td className="table-cell"><Toggle checked={k.verified} onChange={() => toggle(k)} label="verified" /></td>
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
