import {
  Activity, BookOpenCheck, Boxes, FlaskConical, FlaskRound, GitBranch, Gauge, Layers, ListChecks, Menu,
  Rocket, ScrollText, ShieldCheck, Siren, UserCheck, X,
} from "lucide-react";
import { useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { usePoll } from "../hooks";

const NAV = [
  { group: "Overview", items: [{ to: "/", label: "Command center", icon: Gauge }] },
  {
    group: "Delivery",
    items: [
      { to: "/pipelines", label: "Pipelines", icon: GitBranch },
      { to: "/tests", label: "Test intelligence", icon: ListChecks },
      { to: "/deployments", label: "Deployments", icon: Rocket },
    ],
  },
  {
    group: "Operations",
    items: [
      { to: "/incidents", label: "Incidents", icon: Siren, badge: "open" },
      { to: "/approvals", label: "Approvals", icon: UserCheck, badge: "approvals" },
      { to: "/fault-lab", label: "Fault lab", icon: FlaskConical },
    ],
  },
  {
    group: "Learning",
    items: [
      { to: "/knowledge", label: "Incident memory", icon: BookOpenCheck },
      { to: "/evaluation", label: "Evaluation", icon: FlaskRound },
    ],
  },
  {
    group: "Governance",
    items: [
      { to: "/policies", label: "Risk policy", icon: ShieldCheck },
      { to: "/audit", label: "Audit log", icon: ScrollText },
      { to: "/system", label: "Architecture", icon: Layers },
    ],
  },
];

function Sidebar({ onNavigate }) {
  const { data } = usePoll("/api/overview", 5000);
  const counts = { open: data?.kpis.open_incidents, approvals: data?.kpis.pending_approvals };
  const env = data?.environment;
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 px-5 py-5">
        <div className="grid h-9 w-9 place-items-center rounded-xl bg-gradient-to-br from-indigo-500 to-cyan-400 shadow-glow">
          <Boxes className="h-5 w-5 text-white" />
        </div>
        <div>
          <p className="text-[15px] font-semibold tracking-tight text-white">DevNexa</p>
          <p className="text-[11px] text-slate-400">Agentic DevOps Intelligence</p>
        </div>
      </div>
      <nav className="flex-1 space-y-5 overflow-y-auto px-3 pb-4">
        {NAV.map((g) => (
          <div key={g.group}>
            <p className="px-3 pb-1.5 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">{g.group}</p>
            <ul className="space-y-0.5">
              {g.items.map((it) => (
                <li key={it.to}>
                  <NavLink
                    to={it.to}
                    end={it.to === "/"}
                    onClick={onNavigate}
                    className={({ isActive }) =>
                      `group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${
                        isActive ? "bg-white/[0.08] text-white" : "text-slate-400 hover:bg-white/[0.04] hover:text-slate-200"
                      }`
                    }
                  >
                    <it.icon className="h-4 w-4" />
                    <span className="flex-1">{it.label}</span>
                    {it.badge && counts[it.badge] > 0 && (
                      <span className="rounded-full bg-amber-500/20 px-1.5 text-[11px] font-semibold text-amber-300">
                        {counts[it.badge]}
                      </span>
                    )}
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>
      <div className="m-3 rounded-xl border border-white/[0.06] bg-white/[0.02] p-3">
        <div className="flex items-center justify-between">
          <span className="text-[11px] uppercase tracking-wider text-slate-500">orders-service</span>
          <span className={`flex items-center gap-1.5 text-[11px] font-medium ${env?.healthy ? "text-emerald-300" : "text-rose-300"}`}>
            <span className={`h-1.5 w-1.5 animate-pulseDot rounded-full ${env?.healthy ? "bg-emerald-400" : "bg-rose-400"}`} />
            {env ? (env.healthy ? "Healthy" : "Degraded") : "…"}
          </span>
        </div>
        <p className="mt-1 font-mono text-xs text-slate-300">{env?.version || "—"}</p>
        <p className="mt-0.5 flex items-center gap-1 text-[11px] text-slate-500">
          <Activity className="h-3 w-3" /> {env?.mode === "live" ? "Live stack" : "Simulated environment"}
        </p>
      </div>
    </div>
  );
}

export default function Layout({ children }) {
  const [open, setOpen] = useState(false);
  const loc = useLocation();
  return (
    <div className="min-h-screen bg-ink-950 bg-grid">
      <div className="pointer-events-none fixed inset-x-0 top-0 h-72 bg-gradient-to-b from-indigo-600/10 via-transparent to-transparent" />
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 border-r border-white/[0.06] bg-ink-900/80 backdrop-blur lg:block">
        <Sidebar />
      </aside>
      {open && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-black/60" onClick={() => setOpen(false)} />
          <aside className="absolute inset-y-0 left-0 w-72 border-r border-white/10 bg-ink-900">
            <button className="absolute right-3 top-5 text-slate-400" onClick={() => setOpen(false)} aria-label="Close menu">
              <X className="h-5 w-5" />
            </button>
            <Sidebar onNavigate={() => setOpen(false)} />
          </aside>
        </div>
      )}
      <div className="relative lg:pl-64">
        <div className="sticky top-0 z-20 flex items-center gap-3 border-b border-white/[0.06] bg-ink-950/80 px-4 py-3 backdrop-blur lg:hidden">
          <button onClick={() => setOpen(true)} aria-label="Open menu" className="text-slate-300">
            <Menu className="h-5 w-5" />
          </button>
          <span className="font-semibold text-white">DevNexa</span>
        </div>
        <main key={loc.pathname} className="mx-auto min-w-0 max-w-[1400px] px-4 py-6 sm:px-6 lg:px-8 lg:py-8">{children}</main>
      </div>
    </div>
  );
}
