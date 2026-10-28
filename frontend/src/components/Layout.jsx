import { Boxes, ChevronDown, Menu, X } from "lucide-react";
import { useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { usePoll } from "../hooks";
import { NAV } from "../nav";

const MORE_KEY = "devnexa.nav.more";

function readMoreOpen(pathname) {
  if (NAV.more.some((it) => pathname.startsWith(it.to))) return true;
  try {
    return localStorage.getItem(MORE_KEY) === "1";
  } catch {
    return false;
  }
}

function NavItem({ item, counts, onNavigate }) {
  const count = item.badge ? counts?.[item.badge] : 0;
  return (
    <li>
      <NavLink
        to={item.to}
        end={item.to === "/"}
        onClick={onNavigate}
        className={({ isActive }) =>
          `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${
            isActive ? "bg-white/[0.08] text-white" : "text-slate-400 hover:bg-white/[0.04] hover:text-slate-200"
          }`
        }
      >
        <item.icon className="h-4 w-4" />
        <span className="flex-1">{item.label}</span>
        {count > 0 && (
          <span className="rounded-full bg-amber-500/20 px-1.5 text-[11px] font-semibold text-amber-300">{count}</span>
        )}
      </NavLink>
    </li>
  );
}

function Sidebar({ onNavigate }) {
  const { data } = usePoll("/api/overview", 5000);
  const loc = useLocation();
  const [moreOpen, setMoreOpen] = useState(() => readMoreOpen(loc.pathname));
  const toggleMore = () => {
    setMoreOpen((v) => {
      try {
        localStorage.setItem(MORE_KEY, v ? "0" : "1");
      } catch {
        /* storage unavailable */
      }
      return !v;
    });
  };
  const env = data?.environment;
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 px-5 py-5">
        <div className="grid h-9 w-9 place-items-center rounded-xl bg-indigo-500">
          <Boxes className="h-5 w-5 text-white" />
        </div>
        <div>
          <p className="text-[15px] font-semibold tracking-tight text-white">DevNexa</p>
          <p className="text-[11px] text-slate-400">DevOps monitoring</p>
        </div>
      </div>
      <nav className="flex-1 overflow-y-auto px-3 pb-4">
        <ul className="space-y-0.5">
          {NAV.main.map((it) => <NavItem key={it.to} item={it} counts={data?.kpis} onNavigate={onNavigate} />)}
        </ul>
        <button
          onClick={toggleMore}
          aria-expanded={moreOpen}
          className="mt-5 flex w-full items-center justify-between px-3 pb-1.5 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500 hover:text-slate-300"
        >
          More tools
          <ChevronDown className={`h-3.5 w-3.5 transition ${moreOpen ? "rotate-180" : ""}`} />
        </button>
        {moreOpen && (
          <ul className="space-y-0.5">
            {NAV.more.map((it) => <NavItem key={it.to} item={it} counts={data?.kpis} onNavigate={onNavigate} />)}
          </ul>
        )}
      </nav>
      <div className="m-3 flex items-center justify-between rounded-xl border border-white/[0.06] bg-white/[0.02] px-3 py-2.5">
        <span className="text-xs text-slate-400">{env?.mode === "live" ? "Live stack" : "Simulated"}</span>
        <span className={`flex items-center gap-1.5 text-xs font-medium ${env?.healthy ? "text-emerald-300" : "text-rose-300"}`}>
          <span className={`h-1.5 w-1.5 rounded-full ${env?.healthy ? "bg-emerald-400" : "animate-pulseDot bg-rose-400"}`} />
          {env ? (env.healthy ? "Healthy" : "Degraded") : "…"}
        </span>
      </div>
    </div>
  );
}

export default function Layout({ children }) {
  const [open, setOpen] = useState(false);
  const loc = useLocation();
  return (
    <div className="min-h-screen bg-ink-950">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-60 border-r border-white/[0.06] bg-ink-900 lg:block">
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
      <div className="relative lg:pl-60">
        <div className="sticky top-0 z-20 flex items-center gap-3 border-b border-white/[0.06] bg-ink-950/90 px-4 py-3 backdrop-blur lg:hidden">
          <button onClick={() => setOpen(true)} aria-label="Open menu" className="text-slate-300">
            <Menu className="h-5 w-5" />
          </button>
          <span className="font-semibold text-white">DevNexa</span>
        </div>
        <main key={loc.pathname} className="mx-auto min-w-0 max-w-[1200px] px-4 py-6 sm:px-6 lg:px-8 lg:py-8">{children}</main>
      </div>
    </div>
  );
}
