import { AlertTriangle, CheckCircle2, CircleDashed, Info, Loader2, MinusCircle, XCircle } from "lucide-react";
import { titleCase } from "../format";

const TONES = {
  green: "bg-emerald-500/10 text-emerald-300 ring-emerald-500/25",
  red: "bg-rose-500/10 text-rose-300 ring-rose-500/25",
  amber: "bg-amber-500/10 text-amber-300 ring-amber-500/25",
  blue: "bg-sky-500/10 text-sky-300 ring-sky-500/25",
  indigo: "bg-indigo-500/10 text-indigo-300 ring-indigo-500/25",
  violet: "bg-violet-500/10 text-violet-300 ring-violet-500/25",
  slate: "bg-slate-500/10 text-slate-300 ring-slate-500/25",
  cyan: "bg-cyan-500/10 text-cyan-300 ring-cyan-500/25",
};

export function Badge({ tone = "slate", children, className = "", dot = false, pulse = false }) {
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${TONES[tone]} ${className}`}>
      {dot && <span className={`h-1.5 w-1.5 rounded-full bg-current ${pulse ? "animate-pulseDot" : ""}`} />}
      {children}
    </span>
  );
}

const STATUS_TONE = {
  success: "green", passed: "green", resolved: "green", verified: "green", healthy: "green", active: "green",
  confirmed: "green", approved: "green", completed: "green", executed: "cyan", auto_execute: "green",
  failed: "red", unhealthy: "red", rejected: "red", verification_failed: "red", escalated: "red", critical: "red",
  blocked: "red", rolled_back: "amber",
  running: "blue", investigating: "blue", remediating: "blue", verifying: "blue", executing: "blue", queued: "slate",
  diagnosed: "violet", recommend: "amber", approval_required: "amber", awaiting_approval: "amber",
  pending_approval: "amber", warning: "amber", degraded: "amber", open: "amber",
  pending: "slate", skipped: "slate", superseded: "slate", closed: "slate", proposed: "slate",
  low: "green", medium: "amber", high: "red",
};
const LIVE = new Set(["running", "investigating", "remediating", "verifying", "executing", "awaiting_approval", "pending_approval"]);

export function StatusBadge({ status, label }) {
  return (
    <Badge tone={STATUS_TONE[status] || "slate"} dot pulse={LIVE.has(status)}>
      {label || titleCase(status)}
    </Badge>
  );
}

export function RiskBadge({ risk }) {
  return <Badge tone={STATUS_TONE[risk] || "slate"}>{titleCase(risk)} risk</Badge>;
}

export function StatusIcon({ status, className = "h-4 w-4" }) {
  if (["success", "passed", "done", "resolved", "verified"].includes(status))
    return <CheckCircle2 className={`${className} text-emerald-400`} />;
  if (["failed", "verification_failed"].includes(status)) return <XCircle className={`${className} text-rose-400`} />;
  if (["running"].includes(status)) return <Loader2 className={`${className} animate-spin text-sky-400`} />;
  if (["skipped"].includes(status)) return <MinusCircle className={`${className} text-slate-500`} />;
  return <CircleDashed className={`${className} text-slate-500`} />;
}

export function Card({ title, subtitle, action, children, className = "", bodyClass = "card-pad", icon: Icon }) {
  return (
    <section className={`card ${className}`}>
      {(title || action) && (
        <header className="flex items-start justify-between gap-3 border-b border-white/[0.06] px-5 py-4">
          <div className="flex min-w-0 items-start gap-3">
            {Icon && (
              <div className="mt-0.5 rounded-lg bg-indigo-500/10 p-1.5 text-indigo-300 ring-1 ring-inset ring-indigo-500/20">
                <Icon className="h-4 w-4" />
              </div>
            )}
            <div className="min-w-0">
              <h3 className="text-sm font-semibold text-slate-100">{title}</h3>
              {subtitle && <p className="mt-0.5 text-xs text-slate-400">{subtitle}</p>}
            </div>
          </div>
          {action}
        </header>
      )}
      <div className={bodyClass}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, hint, icon: Icon, tone = "indigo" }) {
  const ring = {
    indigo: "from-indigo-500/20 text-indigo-300", emerald: "from-emerald-500/20 text-emerald-300",
    amber: "from-amber-500/20 text-amber-300", rose: "from-rose-500/20 text-rose-300",
    cyan: "from-cyan-500/20 text-cyan-300", violet: "from-violet-500/20 text-violet-300",
  }[tone];
  return (
    <div className="card relative overflow-hidden p-4">
      <div className={`pointer-events-none absolute -right-6 -top-6 h-24 w-24 rounded-full bg-gradient-to-br ${ring} to-transparent blur-2xl`} />
      <div className="flex items-center justify-between">
        <p className="text-xs font-medium text-slate-400">{label}</p>
        {Icon && <Icon className={`h-4 w-4 ${ring.split(" ")[1]}`} />}
      </div>
      <p className="mt-2 text-2xl font-semibold tracking-tight text-white">{value ?? "—"}</p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

export function PageHeader({ title, description, actions, eyebrow }) {
  return (
    <div className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div>
        {eyebrow && <p className="mb-1 text-xs font-semibold uppercase tracking-[0.18em] text-indigo-300/80">{eyebrow}</p>}
        <h1 className="text-2xl font-semibold tracking-tight text-white">{title}</h1>
        {description && <p className="mt-1 max-w-3xl text-sm text-slate-400">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Empty({ title, children, icon: Icon = Info }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
      <div className="rounded-full bg-white/[0.04] p-3 ring-1 ring-white/10">
        <Icon className="h-5 w-5 text-slate-400" />
      </div>
      <p className="mt-3 text-sm font-medium text-slate-200">{title}</p>
      {children && <div className="mt-1 max-w-md text-sm text-slate-500">{children}</div>}
    </div>
  );
}

export function Loading({ label = "Loading" }) {
  return (
    <div className="flex items-center gap-2 px-5 py-10 text-sm text-slate-400">
      <Loader2 className="h-4 w-4 animate-spin" /> {label}…
    </div>
  );
}

export function ErrorBox({ error }) {
  if (!error) return null;
  return (
    <div className="mb-4 flex items-center gap-2 rounded-xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
      <AlertTriangle className="h-4 w-4" /> {error}
    </div>
  );
}

export function Meter({ value, tone = "indigo", className = "" }) {
  const color = { indigo: "bg-indigo-400", green: "bg-emerald-400", amber: "bg-amber-400", red: "bg-rose-400", cyan: "bg-cyan-400" }[tone];
  return (
    <div className={`h-1.5 w-full overflow-hidden rounded-full bg-white/[0.06] ${className}`}>
      <div className={`h-full rounded-full ${color} transition-all`} style={{ width: `${Math.max(0, Math.min(1, value || 0)) * 100}%` }} />
    </div>
  );
}

export function Toggle({ checked, onChange, disabled, label }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition disabled:opacity-40 ${checked ? "bg-indigo-500" : "bg-white/10"}`}
    >
      <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition ${checked ? "translate-x-4" : "translate-x-0.5"}`} />
    </button>
  );
}

export function Toast({ toast }) {
  if (!toast) return null;
  const tone = { info: "border-indigo-400/30 bg-indigo-500/15", success: "border-emerald-400/30 bg-emerald-500/15", error: "border-rose-400/30 bg-rose-500/15" }[toast.tone];
  return (
    <div className="fixed bottom-6 right-6 z-50 max-w-sm">
      <div className={`rounded-xl border px-4 py-3 text-sm text-white shadow-2xl backdrop-blur ${tone}`}>{toast.message}</div>
    </div>
  );
}

export function Tabs({ tabs, value, onChange }) {
  return (
    <div className="flex flex-wrap gap-1 rounded-xl bg-white/[0.03] p-1 ring-1 ring-white/[0.06]">
      {tabs.map((t) => (
        <button
          key={t.value}
          onClick={() => onChange(t.value)}
          className={`rounded-lg px-3 py-1.5 text-xs font-medium transition ${
            value === t.value ? "bg-white/10 text-white shadow" : "text-slate-400 hover:text-slate-200"
          }`}
        >
          {t.label}
          {t.count !== undefined && <span className="ml-1.5 text-slate-500">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

export const chartTooltip = {
  contentStyle: { background: "#0f1529", border: "1px solid rgba(255,255,255,.1)", borderRadius: 10, fontSize: 12 },
  labelStyle: { color: "#cbd5e1" },
  itemStyle: { color: "#e2e8f0" },
  cursor: { fill: "rgba(255,255,255,0.04)" },
};
