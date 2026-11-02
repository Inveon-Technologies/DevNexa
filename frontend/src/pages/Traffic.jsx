import { Pause, Play, X } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { LiveDot, ms, num, secsAgo, StatusCode, TrafficChart } from "../components/traffic";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, Tabs, Toggle } from "../components/ui";
import { pct } from "../format";
import { usePoll } from "../hooks";

const WINDOWS = [{ value: "15", label: "15 min" }, { value: "30", label: "30 min" }, { value: "60", label: "1 hour" }];

function Summary({ s }) {
  const items = [
    ["Visitors (5 min)", num(s.visitors_5m)],
    ["Requests / min", s.requests_per_min],
    ["Error rate (5 min)", pct(s.error_rate_5m, 1), s.error_rate_5m >= 0.05 ? "text-rose-300" : ""],
    ["p95 (5 min)", ms(s.p95_ms_5m)],
    ["Server errors", num(s.errors), s.errors ? "text-rose-300" : ""],
    ["Client errors", num(s.client_errors), s.client_errors ? "text-amber-200" : ""],
  ];
  return (
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-2xl border border-white/[0.06] bg-white/[0.06] sm:grid-cols-3 lg:grid-cols-6">
      {items.map(([label, value, cls]) => (
        <div key={label} className="bg-ink-900 px-4 py-3">
          <p className="text-[11px] text-slate-400">{label}</p>
          <p className={`mt-1 text-xl font-semibold text-white ${cls || ""}`}>{value ?? "—"}</p>
        </div>
      ))}
    </div>
  );
}

export default function Traffic() {
  const [params, setParams] = useSearchParams();
  const app = params.get("app") || "";
  const path = params.get("path") || "";
  const errorsOnly = params.get("errors") === "1";
  const minutes = params.get("minutes") || "30";
  const paused = params.get("paused") === "1";
  const set = (k, v) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };

  const q = new URLSearchParams({ minutes, limit: "100" });
  if (app) q.set("app", app);
  if (path) q.set("path", path);
  if (errorsOnly) q.set("errors_only", "true");
  const { data: t, error } = usePoll(`/api/traffic?${q}`, paused ? 0 : 2000);
  // App names for the picker come from the unfiltered view so the list does not collapse to one entry.
  const { data: all } = usePoll(`/api/traffic?minutes=60&limit=1`, 15000);
  const appNames = [...new Set([...(all?.apps || []).map((a) => a.app), ...(app ? [app] : [])])];

  return (
    <>
      <PageHeader
        title="Live traffic"
        description="Every request to your deployed apps as it happens. Filter to one app or to errors to find what is breaking."
        actions={
          <>
            {t?.simulated && <Badge tone="violet">Simulated</Badge>}
            <LiveDot paused={paused} />
            <button className="btn-ghost" onClick={() => set("paused", paused ? "" : "1")}>
              {paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />} {paused ? "Resume" : "Pause"}
            </button>
          </>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <select className="input w-auto" value={app} onChange={(e) => set("app", e.target.value)} aria-label="App">
          <option value="">All apps</option>
          {appNames.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <Tabs tabs={WINDOWS} value={minutes} onChange={(v) => set("minutes", v === "30" ? "" : v)} />
        <label className="flex items-center gap-2 text-sm text-slate-300">
          <Toggle checked={errorsOnly} onChange={(v) => set("errors", v ? "1" : "")} label="Errors only" /> Errors only
        </label>
        {path && (
          <button onClick={() => set("path", "")} className="flex items-center gap-1.5 rounded-full bg-indigo-500/15 px-3 py-1 font-mono text-xs text-indigo-200">
            {path} <X className="h-3 w-3" />
          </button>
        )}
      </div>

      <ErrorBox error={error} />
      {t?.note && !t.simulated && (
        <p className="mb-4 rounded-xl border border-white/[0.06] bg-white/[0.02] px-4 py-2.5 text-xs text-slate-400">{t.note}</p>
      )}
      {!t ? <Loading /> : (
        <>
          <Summary s={t.summary} />

          <Card className="mt-4" title="Requests per minute" subtitle={app ? `${app} only` : "All apps"}>
            <TrafficChart series={t.series} />
          </Card>

          <Card className="mt-4" title="Failing pages" subtitle="Grouped by page and status code. Click one to see its requests."
                bodyClass="py-1">
            {!t.problems.length ? (
              <p className="px-5 py-6 text-sm text-slate-400">No failing requests in this window.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[560px]">
                  <thead>
                    <tr className="border-b border-white/[0.06]">
                      <th className="table-head">Status</th>
                      <th className="table-head">Page</th>
                      <th className="table-head">App</th>
                      <th className="table-head text-right">Count</th>
                      <th className="table-head text-right">Last seen</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-white/[0.04]">
                    {t.problems.map((p) => (
                      <tr key={`${p.app}${p.method}${p.path}${p.status}`} className="cursor-pointer hover:bg-white/[0.03]"
                          onClick={() => {
                            const next = new URLSearchParams(params);
                            next.set("app", p.app);
                            next.set("path", p.path);
                            next.set("errors", "1");
                            setParams(next, { replace: true });
                          }}>
                        <td className="table-cell"><StatusCode status={p.status} /></td>
                        <td className="table-cell font-mono text-[12.5px] text-slate-100">{p.method} {p.path}</td>
                        <td className="table-cell text-slate-300">{p.app}</td>
                        <td className="table-cell text-right font-medium text-slate-100">{p.count}</td>
                        <td className="table-cell text-right text-xs text-slate-500">{secsAgo(p.last_seen)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card className="mt-4" title="Request log" subtitle={`Newest first · ${t.recent.length} shown · client IPs are masked`} bodyClass="py-1">
            {!t.recent.length ? (
              <Empty title="No requests match">Open one of your app URLs, or clear the filters.</Empty>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[720px] font-mono text-[12px]">
                  <thead>
                    <tr className="border-b border-white/[0.06] font-sans">
                      <th className="table-head">Time</th>
                      <th className="table-head">Status</th>
                      <th className="table-head">Request</th>
                      <th className="table-head">App</th>
                      <th className="table-head text-right">Duration</th>
                      <th className="table-head">Client</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-white/[0.04]">
                    {t.recent.map((r, i) => (
                      <tr key={`${r.ts}-${i}`} className={r.status >= 500 ? "bg-rose-500/[0.04]" : ""}>
                        <td className="whitespace-nowrap px-4 py-2 text-slate-500">
                          {new Date(r.ts * 1000).toLocaleTimeString(undefined, { hour12: false })}
                        </td>
                        <td className="px-4 py-2"><StatusCode status={r.status} /></td>
                        <td className="max-w-[320px] truncate px-4 py-2 text-slate-200" title={r.path}>
                          <span className="text-slate-500">{r.method}</span> {r.path}
                        </td>
                        <td className="px-4 py-2 text-slate-400">{r.app}</td>
                        <td className={`px-4 py-2 text-right ${r.ms >= 1000 ? "text-amber-200" : "text-slate-300"}`}>{ms(r.ms)}</td>
                        <td className="px-4 py-2 text-slate-500" title={r.ua}>{r.client}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </>
  );
}
