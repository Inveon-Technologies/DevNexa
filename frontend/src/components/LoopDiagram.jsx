import { BookOpenCheck, Brain, Eye, GitCommitHorizontal, RefreshCcw, Workflow } from "lucide-react";

const LAYERS = [
  { key: "source", title: "Source & trigger", icon: GitCommitHorizontal, items: ["Git push", "GitHub Actions", "Webhook"] },
  { key: "cicd", title: "Agentic CI/CD", icon: Workflow, items: ["Build", "Test", "Release", "Deploy"] },
  { key: "obs", title: "Observability", icon: Eye, items: ["Prometheus", "Loki", "Jaeger", "Alertmanager"] },
  { key: "ai", title: "AI incident handling", icon: Brain, items: ["Evidence", "LLM + RAG", "Policy", "Verify"] },
  { key: "learn", title: "Knowledge & learning", icon: BookOpenCheck, items: ["pgvector memory", "Risk scores", "Regression tests"] },
];

export default function LoopDiagram({ stats = {} }) {
  return (
    <div className="relative">
      <div className="grid gap-3 md:grid-cols-5">
        {LAYERS.map((l, i) => (
          <div key={l.key} className="relative">
            <div className="h-full rounded-xl border border-white/[0.07] bg-gradient-to-b from-white/[0.04] to-transparent p-3.5">
              <div className="flex items-center gap-2">
                <span className="grid h-6 w-6 place-items-center rounded-md bg-indigo-500/15 text-[11px] font-semibold text-indigo-300">
                  {i + 1}
                </span>
                <l.icon className="h-4 w-4 text-slate-400" />
              </div>
              <p className="mt-2 text-sm font-semibold text-slate-100">{l.title}</p>
              <div className="mt-2 flex flex-wrap gap-1">
                {l.items.map((x) => (
                  <span key={x} className="rounded-md bg-white/[0.05] px-1.5 py-0.5 text-[10.5px] text-slate-400">{x}</span>
                ))}
              </div>
              {stats[l.key] && <p className="mt-3 text-xs font-medium text-cyan-300">{stats[l.key]}</p>}
            </div>
            {i < LAYERS.length - 1 && (
              <div className="absolute -right-2.5 top-1/2 z-10 hidden h-5 w-5 -translate-y-1/2 place-items-center rounded-full border border-white/10 bg-ink-900 text-slate-400 md:grid">
                <span className="text-[10px]">›</span>
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="mt-3 flex items-center justify-center gap-2 rounded-lg border border-dashed border-cyan-400/25 bg-cyan-400/[0.04] px-3 py-2 text-xs text-cyan-200/90">
        <RefreshCcw className="h-3.5 w-3.5" />
        Continuous feedback: verified incidents update risk scores and add regression tests for the next pipeline
      </div>
    </div>
  );
}
