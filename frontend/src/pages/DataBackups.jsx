import {
  Archive, CheckCircle2, Database, Download, HardDrive, Pin, PinOff, Plus, RotateCcw, ShieldCheck, Trash2, Upload,
} from "lucide-react";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, downloadFile, operatorName, uploadFile } from "../api";
import { Badge, Card, Empty, Loading, Toggle } from "../components/ui";
import { ago, time } from "../format";
import { usePoll } from "../hooks";

const KIND = {
  scheduled: ["Scheduled", "cyan"], manual: ["Manual", "indigo"], "pre-deploy": ["Before new version", "violet"],
  "pre-restore": ["Before restore", "amber"], final: ["Final (DB deleted)", "red"], imported: ["Imported", "blue"],
};
const STATUS = { ready: "green", failed: "red", running: "blue" };
const actor = () => encodeURIComponent(operatorName());

export function bytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}

function Policy({ app, d, reload, show }) {
  const p = d.policy;
  const save = async (body, msg = "Saved") => {
    try { await api.put(`/api/live/apps/${app}/settings`, { ...body, actor: operatorName() }); show(msg, "success"); reload(); } catch (e) { show(e.message, "error"); }
  };
  const field = (key, label, options) => (
    <label><span className="label">{label}</span>
      <select className="input" value={p[key]} disabled={!p.enabled} onChange={(e) => save({ backup_policy: { [key]: Number(e.target.value) } })}>
        {options.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
      </select>
    </label>
  );
  return (
    <Card title="Smart backups" icon={ShieldCheck} action={<Toggle label="Smart backups" checked={p.enabled} onChange={(v) => save({ steps: { smart_backup: v } }, `Smart backups ${v ? "on" : "off"}`)} />}>
      <p className="text-xs text-slate-400">
        {p.enabled ? <>Next run {d.next_run ? time(d.next_run) : "within a minute"}{p.last_skipped && <> · last skipped {ago(p.last_skipped)} (no changes)</>}</> : "Off: only manual backups are taken."}
      </p>
      {p.last_error && <p className="mt-1 text-xs text-rose-300">Last scheduled run failed: {p.last_error}</p>}
      <div className="mt-3 grid grid-cols-2 gap-2">
        {field("every_hours", "Every", [[1, "hour"], [6, "6 hours"], [12, "12 hours"], [24, "day"], [168, "week"]])}
        {field("keep_last", "Keep newest", [1, 3, 5, 7, 14, 30].map((n) => [n, n]))}
        {field("keep_daily", "+ one per day for", [0, 3, 7, 14, 30].map((n) => [n, `${n} days`]))}
        {field("keep_weekly", "+ one per week for", [0, 2, 4, 8, 12].map((n) => [n, `${n} weeks`]))}
        {field("keep_monthly", "+ one per month for", [0, 1, 3, 6, 12].map((n) => [n, `${n} months`]))}
      </div>
      <div className="mt-3 space-y-2 text-sm">
        {[["skip_unchanged", "Skip when nothing changed"], ["before_changes", "Back up before a new version, a restore or deleting a database"]].map(([k, t]) => (
          <div key={k} className="flex items-center justify-between gap-3 text-slate-300">
            <span>{t}</span>
            <Toggle label={t} checked={!!p[k]} disabled={!p.enabled} onChange={(v) => save({ backup_policy: { [k]: v } })} />
          </div>
        ))}
        <div className="flex items-center justify-between gap-3 text-slate-300">
          <span>Persistent <code className="text-xs">/data</code> volume <span className="block text-xs text-slate-500">Files the app writes to $DATA_DIR survive redeploys. Applies on the next deploy.</span></span>
          <Toggle label="Persistent data" checked={p.persistent_data} onChange={(v) => save({ steps: { persistent_data: v } }, `Persistent /data ${v ? "on" : "off"}`)} />
        </div>
      </div>
      <p className="mt-3 text-xs text-slate-500">Pruned backups go to the <Link to="/trash" className="text-indigo-300 hover:underline">trash</Link> for {d.trash_days} days before they are deleted.</p>
    </Card>
  );
}

function DatabaseCard({ app, d, reload, show }) {
  const [engine, setEngine] = useState("postgres");
  const [busy, setBusy] = useState(false);
  const db = d.database;
  const act = async (fn, msg) => { setBusy(true); try { await fn(); show(msg, "success"); reload(); } catch (e) { show(e.message, "error"); } finally { setBusy(false); } };
  return (
    <Card title="Database" icon={Database}>
      {db ? (
        <>
          <p className="flex items-center gap-2 text-sm text-slate-100"><b>{db.engine_title}</b> <Badge tone="green" dot>running</Badge></p>
          <p className="mt-1 break-all font-mono text-[11px] text-slate-400">{db.url_masked}</p>
          <p className="mt-1 text-xs text-slate-500">The app gets {db.env_keys.join(", ")}. Data lives in volume {db.volume}.</p>
          <button className="btn-ghost mt-3 py-1.5 text-xs text-rose-300" disabled={busy}
            onClick={() => window.confirm(`Delete the ${db.engine_title} database of ${app}? A final backup is taken and the database stays in the trash for ${d.trash_days} days, so you can restore it.`) && act(() => api.del(`/api/live/apps/${app}/databases/${db.id}?actor=${actor()}`), "Database moved to the trash")}>
            <Trash2 className="h-3.5 w-3.5" /> Delete (to trash)
          </button>
        </>
      ) : (
        <>
          {d.sources.some((s) => s.type === "db")
            ? <p className="text-sm text-slate-300">Backing up the database the app's env points to: {d.sources.find((s) => s.type === "db").source}.</p>
            : <p className="text-sm text-slate-400">No database. Add one and DevNexa runs it next to the app, sets DATABASE_URL and restarts the app with no downtime.</p>}
          <div className="mt-3 grid grid-cols-[1fr_auto] gap-2">
            <select className="input" value={engine} onChange={(e) => setEngine(e.target.value)}>
              {d.engines.map((e) => <option key={e.key} value={e.key}>{e.title}</option>)}
            </select>
            <button className="btn-primary" disabled={busy} onClick={() => act(() => api.post(`/api/live/apps/${app}/databases`, { engine, actor: operatorName() }), "Database added; the app restarts with DATABASE_URL")}><Plus className="h-4 w-4" /> Add</button>
          </div>
        </>
      )}
    </Card>
  );
}

function ImportCard({ app, d, reload, show }) {
  const input = useRef(null);
  const [restoreNow, setRestoreNow] = useState(false);
  const [busy, setBusy] = useState(false);
  const pick = async (file) => {
    if (!file) return;
    if (file.size > d.import_max_mb * 1024 * 1024) { show(`The file is larger than ${d.import_max_mb} MB.`, "error"); return; }
    if (restoreNow && !window.confirm(`Import ${file.name} and restore it into ${app} right away? A safety backup of the current data is taken first.`)) return;
    setBusy(true);
    try {
      const r = await uploadFile(`/api/live/apps/${app}/data-backups/import?filename=${encodeURIComponent(file.name)}&restore_now=${restoreNow}&actor=${actor()}`, file);
      const errs = r.restore?.errors || [];
      show(errs.length ? `Imported; restore problems: ${errs.join("; ")}` : r.restore ? `Imported and restored ${r.restore.restored.join(" + ")}` : `Imported ${file.name}`, errs.length ? "error" : "success");
      reload();
    } catch (e) { show(e.message, "error"); } finally { setBusy(false); if (input.current) input.current.value = ""; }
  };
  return (
    <Card title="Import a backup" icon={Upload}>
      <p className="text-xs text-slate-400">A DevNexa backup (.tar.gz from Download, from this or another server), a database dump (.sql, .sql.gz, mongodump .archive), a folder archive for /data (.tar.gz, .zip) or a SQLite file. Up to {d.import_max_mb} MB.</p>
      <input ref={input} type="file" className="hidden" accept=".gz,.tgz,.tar,.zip,.sql,.archive,.agz,.sqlite,.sqlite3,.db" onChange={(e) => pick(e.target.files[0])} />
      <div className="mt-3 flex items-center justify-between gap-3">
        <label className="flex items-center gap-2 text-sm text-slate-300"><Toggle label="Restore right away" checked={restoreNow} onChange={setRestoreNow} /> Restore right away</label>
        <button className="btn-primary" disabled={busy} onClick={() => input.current?.click()}><Upload className="h-4 w-4" /> {busy ? "Uploading…" : "Choose file"}</button>
      </div>
    </Card>
  );
}

function BackupRow({ app, b, reload, show }) {
  const [busy, setBusy] = useState(false);
  const act = async (fn, msg) => { setBusy(true); try { const r = await fn(); show(typeof msg === "function" ? msg(r) : msg, r?.errors?.length ? "error" : "success"); reload(); } catch (e) { show(e.message, "error"); } finally { setBusy(false); } };
  const [kindLabel, kindTone] = KIND[b.kind] || [b.kind, "slate"];
  const restore = () => {
    const withCode = b.release_label && window.confirm(`Also put the code version ${b.release_label} back live? OK = data and code, Cancel = data only.`);
    if (!window.confirm(`Restore ${b.label} into ${app}? The current data is backed up first, so this can be undone.`)) return;
    act(() => api.post(`/api/live/apps/${app}/data-backups/${b.id}/restore`, { with_code: !!withCode, actor: operatorName() }),
      (r) => r.errors.length ? `Restored ${r.restored.join(" + ") || "nothing"}; ${r.errors.join("; ")}` : `Restored ${r.restored.join(" + ")}${r.deployment_id ? `, rolling back to ${b.release_label}` : ""}`);
  };
  return (
    <li className="flex flex-col gap-2 px-5 py-3 sm:flex-row sm:items-center">
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-2 text-sm text-slate-100">
          <b>#{b.id}</b> <Badge tone={kindTone}>{kindLabel}</Badge> <Badge tone={STATUS[b.status] || "slate"}>{b.status}</Badge>
          {b.verified && <span title="Read back and checked after it was written" className="inline-flex items-center gap-1 text-[11px] text-emerald-300"><CheckCircle2 className="h-3 w-3" /> verified</span>}
          {b.pinned && <Pin className="h-3 w-3 text-amber-300" />}
        </p>
        <p className="truncate text-xs text-slate-500">
          {b.parts.map((p) => p.type === "db" ? `${p.engine} dump` : "/data files").join(" + ") || "—"} · {b.size} · {b.release_label ? `${b.release_label} live · ` : ""}{time(b.created_at)} · {b.created_by}
        </p>
        {b.note && <p className="truncate text-xs text-slate-500" title={b.note}>{b.note}</p>}
      </div>
      {b.status === "ready" && (
        <div className="flex flex-wrap gap-1">
          <button className="btn-ghost py-1.5 text-xs" disabled={busy} onClick={restore}><RotateCcw className="h-3.5 w-3.5" /> Restore</button>
          <button className="btn-ghost px-2 py-1.5 text-xs" title="Download" onClick={() => downloadFile(`/api/live/apps/${app}/data-backups/${b.id}/download`, `${app}-backup-${b.id}.tar.gz`).catch((e) => show(e.message, "error"))}><Download className="h-3.5 w-3.5" /></button>
          <button className="btn-ghost px-2 py-1.5 text-xs" title={b.pinned ? "Unpin (retention may remove it)" : "Pin (keep forever)"} disabled={busy} onClick={() => act(() => api.post(`/api/live/apps/${app}/data-backups/${b.id}/pin`, { pinned: !b.pinned }), b.pinned ? "Unpinned" : "Pinned")}>{b.pinned ? <PinOff className="h-3.5 w-3.5" /> : <Pin className="h-3.5 w-3.5" />}</button>
          <button className="btn-ghost px-2 py-1.5 text-xs text-rose-300" title="Move to trash" disabled={busy} onClick={() => act(() => api.del(`/api/live/apps/${app}/data-backups/${b.id}?actor=${actor()}`), "Moved to the trash")}><Trash2 className="h-3.5 w-3.5" /></button>
        </div>
      )}
      {b.status === "failed" && (
        <button className="btn-ghost px-2 py-1.5 text-xs" title="Move to trash" onClick={() => act(() => api.del(`/api/live/apps/${app}/data-backups/${b.id}?actor=${actor()}`), "Moved to the trash")}><Trash2 className="h-3.5 w-3.5" /></button>
      )}
    </li>
  );
}

export default function DataBackups({ app, show }) {
  const { data: d, reload } = usePoll(`/api/live/apps/${app}/data`, 5000, [app]);
  const [busy, setBusy] = useState(false);
  if (!d) return <Loading />;
  const backupNow = async () => {
    setBusy(true);
    try { const b = await api.post(`/api/live/apps/${app}/data-backups`, { actor: operatorName() }); show(`Backup #${b.id} taken (${b.size})`, "success"); reload(); } catch (e) { show(e.message, "error"); } finally { setBusy(false); }
  };
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
      <Card title="Data backups" icon={Archive} bodyClass=""
        subtitle={<>
          {d.remote_server ? <>This app runs on a remote server; data backups and managed databases currently cover apps on the DevNexa host only. </> : null}
          {d.sources.length ? <>Each backup holds: {d.sources.map((s) => s.source).join(" + ")}, and notes which version was live.</> : "Nothing to back up yet: switch on the /data volume or add a database."}
          {" "}{bytes(d.total_bytes)} kept{d.trash_count ? <> · <Link to="/trash" className="text-indigo-300 hover:underline">{d.trash_count} in trash</Link></> : ""}.
        </>}
        action={<button className="btn-ghost whitespace-nowrap py-1.5 text-xs" disabled={busy || !d.sources.length} onClick={backupNow}><HardDrive className="h-3.5 w-3.5" /> {busy ? "Backing up…" : "Back up now"}</button>}>
        {!d.backups.length ? <Empty title="No data backups yet" icon={Archive}>The first scheduled backup runs within a minute of the app going live, and one is taken before every new version.</Empty> : (
          <ul className="divide-y divide-white/[0.04]">
            {d.backups.map((b) => <BackupRow key={b.id} app={app} b={b} reload={reload} show={show} />)}
          </ul>
        )}
      </Card>
      <div className="space-y-4">
        <Policy app={app} d={d} reload={reload} show={show} />
        <DatabaseCard app={app} d={d} reload={reload} show={show} />
        <ImportCard app={app} d={d} reload={reload} show={show} />
      </div>
    </div>
  );
}
