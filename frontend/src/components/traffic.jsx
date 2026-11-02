import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { chartTooltip } from "./ui";

const hhmm = (t) => new Date(t * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });

/** Requests and server errors per minute. The minute in progress is left out so it does not read as a drop. */
export function TrafficChart({ series, height = "h-56" }) {
  return (
    <div className={height}>
      <ResponsiveContainer>
        <AreaChart data={series.slice(0, -1)} margin={{ left: -18, right: 8, top: 8 }}>
          <defs>
            <linearGradient id="reqFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#818cf8" stopOpacity={0.35} />
              <stop offset="100%" stopColor="#818cf8" stopOpacity={0} />
            </linearGradient>
            <linearGradient id="errFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#fb7185" stopOpacity={0.45} />
              <stop offset="100%" stopColor="#fb7185" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid vertical={false} />
          <XAxis dataKey="t" tickFormatter={hhmm} axisLine={false} tickLine={false} minTickGap={40} />
          <YAxis allowDecimals={false} axisLine={false} tickLine={false} />
          <Tooltip {...chartTooltip} cursor={{ stroke: "rgba(255,255,255,0.15)" }} labelFormatter={hhmm} />
          <Area type="monotone" dataKey="requests" name="Requests" stroke="#818cf8" strokeWidth={2} fill="url(#reqFill)"
                isAnimationActive={false} />
          <Area type="monotone" dataKey="errors" name="Server errors" stroke="#fb7185" strokeWidth={2} fill="url(#errFill)"
                isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

export function StatusCode({ status }) {
  const tone = status >= 500 ? "bg-rose-500/15 text-rose-300" : status >= 400 ? "bg-amber-500/15 text-amber-300"
    : status >= 300 ? "bg-sky-500/15 text-sky-300" : "bg-emerald-500/10 text-emerald-300";
  return <span className={`inline-block w-11 rounded-md py-0.5 text-center font-mono text-[11px] font-semibold ${tone}`}>{status}</span>;
}

export function LiveDot({ paused }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-300">
      <span className={`h-2 w-2 rounded-full ${paused ? "bg-slate-500" : "animate-pulseDot bg-emerald-400"}`} />
      {paused ? "Paused" : "Live"}
    </span>
  );
}

export const ms = (v) => (v === null || v === undefined ? "—" : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);
export const num = (v) => (v === null || v === undefined ? "—" : v >= 10000 ? `${(v / 1000).toFixed(1)}k` : `${v}`);
export const secsAgo = (ts) => {
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 60 ? `${Math.floor(s)}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : `${Math.floor(s / 3600)}h ago`;
};
