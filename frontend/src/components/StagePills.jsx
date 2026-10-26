import { StatusIcon } from "./ui";

export default function StagePills({ stages }) {
  return (
    <div className="flex items-center gap-1">
      {stages.map((s, i) => (
        <div key={s.name} className="flex items-center gap-1">
          <span
            title={`${s.name}: ${s.status}`}
            className={`flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] capitalize ring-1 ring-inset ${
              s.status === "success" ? "bg-emerald-500/10 text-emerald-300 ring-emerald-500/20"
                : s.status === "failed" ? "bg-rose-500/10 text-rose-300 ring-rose-500/20"
                : s.status === "running" ? "bg-sky-500/10 text-sky-300 ring-sky-500/20"
                : "bg-white/[0.03] text-slate-500 ring-white/10"
            }`}
          >
            <StatusIcon status={s.status} className="h-3 w-3" />
            {s.name}
          </span>
          {i < stages.length - 1 && <span className="h-px w-2 bg-white/10" />}
        </div>
      ))}
    </div>
  );
}
