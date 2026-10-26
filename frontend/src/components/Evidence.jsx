import { GitCommitHorizontal, Rocket } from "lucide-react";
import { useState } from "react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Badge, chartTooltip, Empty, StatusBadge, Tabs } from "./ui";
import { clock } from "../format";

const LEVEL = { ERROR: "text-rose-300", WARN: "text-amber-300", INFO: "text-sky-300", DEBUG: "text-slate-500" };

function Metrics({ content }) {
  const anomalies = content.anomalies || [];
  const keys = anomalies.length ? anomalies.map((a) => a.metric) : ["http_error_rate", "http_p95_latency_ms"];
  if (!content.series?.length) return <Empty title="No metric samples">{content.error}</Empty>;
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {keys.slice(0, 4).map((k) => {
        const a = anomalies.find((x) => x.metric === k);
        return (
          <div key={k} className="rounded-xl border border-white/[0.06] bg-black/20 p-3">
            <div className="mb-2 flex items-center justify-between">
              <p className="text-xs font-medium text-slate-300">{content.labels?.[k] || k}</p>
              {a && <Badge tone="red">{+a.baseline.toFixed(3)} → {+a.current.toFixed(3)}</Badge>}
            </div>
            <div className="h-32">
              <ResponsiveContainer>
                <LineChart data={content.series} margin={{ left: 0, right: 4, top: 4 }}>
                  <CartesianGrid vertical={false} />
                  <XAxis dataKey="t" tickFormatter={(t) => clock(t).slice(0, 5)} axisLine={false} tickLine={false} minTickGap={40} />
                  <YAxis axisLine={false} tickLine={false} width={44} tickFormatter={(v) => (v >= 1000 ? `${+(v / 1000).toFixed(1)}k` : +v.toFixed(2))} />
                  <Tooltip {...chartTooltip} labelFormatter={clock} />
                  {a && <ReferenceLine y={a.baseline} stroke="#64748b" strokeDasharray="3 3" />}
                  <Line dataKey={k} stroke={a ? "#fb7185" : "#818cf8"} strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Logs({ content }) {
  const lines = content.lines || [];
  return (
    <div>
      <p className="mb-2 font-mono text-[11px] text-slate-500">{content.query}</p>
      <div className="max-h-96 overflow-auto rounded-xl bg-black/40 p-3 ring-1 ring-white/5">
        {lines.length ? lines.map((l, i) => (
          <div key={i} className="flex gap-3 font-mono text-[12px] leading-6">
            <span className="shrink-0 text-slate-600">{clock(l.ts)}</span>
            <span className={`w-12 shrink-0 ${LEVEL[l.level] || ""}`}>{l.level}</span>
            <span className="text-slate-300">{l.message}</span>
          </div>
        )) : <p className="text-sm text-slate-500">{content.error || "No matching log lines."}</p>}
      </div>
    </div>
  );
}

function Traces({ content }) {
  const traces = content.traces || [];
  if (!traces.length) return <Empty title="No sampled traces">{content.error || "This request path is not instrumented."}</Empty>;
  return (
    <div className="space-y-4">
      {traces.map((t) => {
        const max = Math.max(...t.spans.map((s) => s.duration_ms), 1);
        return (
          <div key={t.trace_id} className="rounded-xl border border-white/[0.06] bg-black/20 p-3">
            <div className="mb-2 flex items-center justify-between text-xs">
              <span className="font-medium text-slate-200">{t.root}</span>
              <span className="font-mono text-slate-500">{t.trace_id} · {t.duration_ms} ms</span>
            </div>
            {t.spans.map((s, i) => (
              <div key={i} className="flex items-center gap-3 py-1">
                <span className={`w-44 shrink-0 truncate font-mono text-[11.5px] ${s.depth ? "pl-4 text-slate-400" : "text-slate-200"}`}>{s.name}</span>
                <div className="h-3 flex-1 rounded bg-white/[0.03]">
                  <div className={`h-full rounded ${s.error ? "bg-rose-400/80" : s.duration_ms > 300 ? "bg-amber-400/80" : "bg-indigo-400/70"}`}
                       style={{ width: `${Math.max(2, (s.duration_ms / max) * 100)}%` }} />
                </div>
                <span className="w-16 text-right font-mono text-[11px] text-slate-500">{s.duration_ms} ms</span>
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

function Changes({ commits, deployments }) {
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <div>
        <p className="label">Recent commits</p>
        <ul className="space-y-2">
          {(commits?.content.commits || []).map((c) => (
            <li key={c.sha + c.pipeline_run} className="rounded-lg border border-white/[0.06] bg-black/20 p-3">
              <div className="flex items-center gap-2 text-sm text-slate-200"><GitCommitHorizontal className="h-3.5 w-3.5 text-slate-500" />{c.message}</div>
              <p className="mt-1 text-xs text-slate-500"><span className="font-mono">{c.sha}</span> · {c.author} · run #{c.pipeline_run} <StatusBadge status={c.status} /></p>
              <p className="mt-1 font-mono text-[11px] text-slate-400">{c.files.join(", ")}</p>
            </li>
          ))}
        </ul>
      </div>
      <div>
        <p className="label">Deployment history</p>
        <ul className="space-y-2">
          {(deployments?.content.deployments || []).map((d) => (
            <li key={d.id} className="flex items-center justify-between rounded-lg border border-white/[0.06] bg-black/20 p-3">
              <div>
                <p className="flex items-center gap-2 text-sm text-slate-200"><Rocket className="h-3.5 w-3.5 text-slate-500" />{d.version}{d.is_stable && <Badge tone="green">stable</Badge>}</p>
                <p className="mt-0.5 text-xs text-slate-500">{d.minutes_before_alert} min before alert · <span className="font-mono">{d.commit_sha}</span></p>
              </div>
              <StatusBadge status={d.status} />
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

export default function Evidence({ items }) {
  const [tab, setTab] = useState("metric");
  const by = Object.fromEntries(items.map((e) => [e.kind, e]));
  const tabs = [
    { value: "metric", label: "Metrics" }, { value: "log", label: "Logs" }, { value: "trace", label: "Traces" },
    { value: "changes", label: "Changes" },
  ];
  if (!items.length) return <Empty title="Collecting evidence…" />;
  return (
    <div>
      <Tabs tabs={tabs} value={tab} onChange={setTab} />
      <div className="mt-4">
        {tab === "metric" && by.metric && <Metrics content={by.metric.content} />}
        {tab === "log" && by.log && <Logs content={by.log.content} />}
        {tab === "trace" && by.trace && <Traces content={by.trace.content} />}
        {tab === "changes" && <Changes commits={by.commit} deployments={by.deployment} />}
      </div>
    </div>
  );
}
