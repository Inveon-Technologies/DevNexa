import { Check, ShieldCheck, X } from "lucide-react";
import { useState } from "react";
import { api, operatorName } from "../api";
import { RiskBadge, StatusBadge } from "./ui";
import { titleCase } from "../format";

export default function ActionCard({ action, onDone, compact = false }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const pending = action.status === "pending_approval";
  const decide = async (verb) => {
    setBusy(true);
    setErr(null);
    try {
      await api.post(`/api/actions/${action.id}/${verb}`, { actor: operatorName(), note });
      onDone?.(verb);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={`rounded-xl border p-4 ${pending ? "border-amber-400/30 bg-amber-500/[0.06]" : "border-white/[0.06] bg-white/[0.02]"}`}>
      <div className="flex flex-wrap items-center gap-2">
        <ShieldCheck className="h-4 w-4 text-slate-400" />
        <p className="font-medium text-slate-100">{action.title}</p>
        <RiskBadge risk={action.risk_level} />
        <StatusBadge status={action.decision} label={titleCase(action.decision)} />
        <span className="ml-auto"><StatusBadge status={action.status} /></span>
      </div>
      {!compact && <p className="mt-2 text-sm text-slate-400">{action.description}</p>}
      {action.decision_note && <p className="mt-1.5 text-xs text-slate-500">Policy: {action.decision_note}</p>}
      {action.decided_by && <p className="mt-1 text-xs text-slate-500">Decided by <span className="text-slate-300">{action.decided_by}</span></p>}
      {action.result?.output && <p className="mt-2 rounded-md bg-black/30 px-2 py-1.5 font-mono text-[11.5px] text-slate-300">{action.result.output}</p>}
      {action.result?.error && <p className="mt-2 rounded-md bg-rose-500/10 px-2 py-1.5 font-mono text-[11.5px] text-rose-300">{action.result.error}</p>}
      {pending && (
        <div className="mt-4 flex flex-col gap-2 sm:flex-row">
          <input className="input flex-1" placeholder="Optional note for the audit log" value={note} onChange={(e) => setNote(e.target.value)} />
          <button disabled={busy} className="btn-success" onClick={() => decide("approve")}><Check className="h-4 w-4" /> Approve & execute</button>
          <button disabled={busy} className="btn-danger" onClick={() => decide("reject")}><X className="h-4 w-4" /> Reject</button>
        </div>
      )}
      {err && <p className="mt-2 text-xs text-rose-300">{err}</p>}
    </div>
  );
}
