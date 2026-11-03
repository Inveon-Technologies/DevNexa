import {
  CheckCircle2, Copy, Cpu, Download, HardDrive, KeyRound, MemoryStick, Play, Plus, RefreshCw, RotateCw, Server as ServerIcon,
  Square, Terminal, Trash2, Wrench,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, operatorName } from "../api";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, StatusBadge, Toast } from "../components/ui";
import { ago } from "../format";
import { usePoll, useToast } from "../hooks";

const STATUS = {
  online: { tone: "green", label: "Online" },
  no_docker: { tone: "amber", label: "No Docker" },
  offline: { tone: "red", label: "Unreachable" },
  new: { tone: "slate", label: "Not checked yet" },
};

function ServerStatus({ status }) {
  const s = STATUS[status] || STATUS.new;
  return <Badge tone={s.tone} dot>{s.label}</Badge>;
}

function copy(text, show) {
  try {
    navigator.clipboard.writeText(text);
    show("Copied", "success");
  } catch {
    show("Copy failed; select the text instead", "error");
  }
}

function AddServer({ onAdded, show }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ name: "", host: "", username: "ubuntu", ssh_port: "22", key: "", address: "", port_base: "20000" });
  const [pasteKey, setPasteKey] = useState(false);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const s = await api.post("/api/servers", {
        name: form.name.trim().toLowerCase(), host: form.host.trim(), username: form.username.trim(),
        ssh_port: Number(form.ssh_port) || 22, private_key: pasteKey ? form.key : null,
        address: form.address.trim(), port_base: Number(form.port_base) || 20000, actor: operatorName(),
      });
      show(`Added ${s.name}`, "success");
      setOpen(false);
      setForm({ ...form, name: "", host: "", key: "", address: "" });
      onAdded(s.id);
    } catch (err) { show(err.message, "error"); } finally { setBusy(false); }
  };
  if (!open) {
    return <button className="btn-primary w-full justify-center" onClick={() => setOpen(true)}><Plus className="h-4 w-4" /> Add a server</button>;
  }
  return (
    <Card title="Add a server" icon={Plus}
      subtitle="Any Linux machine you can reach over SSH: another EC2 instance, a VPS, or a machine in your lab.">
      <form onSubmit={submit} className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <label><span className="label">Name</span>
            <input className="input font-mono" placeholder="worker-1" value={form.name} onChange={set("name")} required />
          </label>
          <label><span className="label">Host (IP or hostname)</span>
            <input className="input font-mono" placeholder="203.0.113.10" value={form.host} onChange={set("host")} required />
          </label>
          <label><span className="label">SSH user</span>
            <input className="input font-mono" value={form.username} onChange={set("username")} required />
          </label>
          <label><span className="label">SSH port</span>
            <input className="input" inputMode="numeric" value={form.ssh_port} onChange={set("ssh_port")} />
          </label>
        </div>
        <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-3 text-sm">
          <label className="flex items-center gap-2 text-slate-300">
            <input type="radio" checked={!pasteKey} onChange={() => setPasteKey(false)} /> Generate a key for DevNexa (recommended)
          </label>
          <label className="mt-1 flex items-center gap-2 text-slate-300">
            <input type="radio" checked={pasteKey} onChange={() => setPasteKey(true)} /> Use my own private key (e.g. the EC2 .pem file)
          </label>
          {pasteKey && (
            <textarea className="input mt-2 h-28 font-mono text-xs" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
              value={form.key} onChange={set("key")} required />
          )}
        </div>
        <details className="text-sm text-slate-400">
          <summary className="cursor-pointer text-xs text-slate-400">Advanced</summary>
          <div className="mt-2 grid gap-3 sm:grid-cols-2">
            <label><span className="label">App address (if different from host)</span>
              <input className="input font-mono" placeholder="same as host" value={form.address} onChange={set("address")} />
            </label>
            <label><span className="label">First app port</span>
              <input className="input" inputMode="numeric" value={form.port_base} onChange={set("port_base")} />
            </label>
          </div>
        </details>
        <div className="flex gap-2">
          <button className="btn-primary" disabled={busy}><Plus className="h-4 w-4" /> {busy ? "Adding…" : "Add server"}</button>
          <button type="button" className="btn-ghost" onClick={() => setOpen(false)}>Cancel</button>
        </div>
      </form>
    </Card>
  );
}

function Fact({ icon: Icon, label, value, hint }) {
  return (
    <div className="rounded-xl bg-white/[0.03] p-3">
      <p className="flex items-center gap-1 text-[11px] text-slate-500">{Icon && <Icon className="h-3 w-3" />}{label}</p>
      <p className="truncate text-sm font-semibold text-white">{value ?? "—"}</p>
      {hint && <p className="truncate text-[11px] text-slate-500">{hint}</p>}
    </div>
  );
}

function Console({ server, show, preset }) {
  const [cmd, setCmd] = useState("");
  const [history, setHistory] = useState([]);
  const [busy, setBusy] = useState(false);
  const box = useRef(null);
  useEffect(() => { if (preset) setCmd(preset.cmd); }, [preset]);
  useEffect(() => { if (box.current) box.current.scrollTop = box.current.scrollHeight; }, [history]);
  const run = async (e) => {
    e.preventDefault();
    if (!cmd.trim()) return;
    setBusy(true);
    try {
      const r = await api.post(`/api/servers/${server.id}/exec`, { command: cmd, actor: operatorName() });
      setHistory((h) => h.concat([{ cmd, ...r }]).slice(-20));
      setCmd("");
    } catch (err) { show(err.message, "error"); } finally { setBusy(false); }
  };
  return (
    <Card title="Remote terminal" icon={Terminal} bodyClass=""
      subtitle={`Runs one command at a time as ${server.username}@${server.host}. Every command is written to the audit log.`}>
      <div ref={box} className="max-h-[360px] min-h-[120px] overflow-auto bg-black/40 px-4 py-3 font-mono text-[12px] leading-5">
        {history.length ? history.map((h, i) => (
          <div key={i} className="mb-2">
            <div className="text-indigo-300">$ {h.command}</div>
            <div className={`whitespace-pre-wrap break-all ${h.exit_code ? "text-rose-300" : "text-slate-300"}`}>{h.output || "(no output)"}</div>
            {h.exit_code !== 0 && <div className="text-[11px] text-rose-400">exit code {h.exit_code}</div>}
          </div>
        )) : <p className="text-slate-500">Try <span className="text-slate-300">docker ps</span>, <span className="text-slate-300">df -h</span> or <span className="text-slate-300">journalctl -u docker -n 50</span>.</p>}
      </div>
      <form onSubmit={run} className="flex gap-2 border-t border-white/[0.06] p-3">
        <input className="input font-mono" placeholder="command" value={cmd} onChange={(e) => setCmd(e.target.value)} />
        <button className="btn-primary" disabled={busy || !cmd.trim()}><Play className="h-4 w-4" /> {busy ? "Running…" : "Run"}</button>
      </form>
    </Card>
  );
}

function Containers({ server, containers, onChanged, show }) {
  const [logs, setLogs] = useState(null);
  const act = async (name, action) => {
    if (action === "stop" && !window.confirm(`Stop ${name} on ${server.name}?`)) return;
    try {
      const r = await api.post(`/api/servers/${server.id}/containers/${encodeURIComponent(name)}/${action}`, { actor: operatorName() });
      if (action === "logs") setLogs({ name, text: r.output });
      else { show(r.exit_code === 0 ? `${action} ${name}: done` : r.output.slice(-200), r.exit_code === 0 ? "success" : "error"); onChanged(); }
    } catch (e) { show(e.message, "error"); }
  };
  if (!containers) return null;
  return (
    <Card title={`Containers on ${server.name}`} icon={ServerIcon} bodyClass="">
      {!containers.length ? <Empty title="No containers" icon={ServerIcon}>Deploy an app to this server from the Deploy page.</Empty> : (
        <ul className="divide-y divide-white/[0.04]">
          {containers.map((c) => (
            <li key={c.id} className="flex flex-col gap-2 px-5 py-3 sm:flex-row sm:items-center">
              <div className="min-w-0 flex-1">
                <p className="flex flex-wrap items-center gap-2 text-sm text-slate-100">
                  <span className="truncate font-mono">{c.name}</span>
                  <StatusBadge status={c.state === "running" ? "healthy" : c.state === "exited" ? "failed" : "pending"} label={c.state} />
                  {c.devnexa && <Badge tone="indigo">DevNexa app</Badge>}
                </p>
                <p className="truncate text-xs text-slate-500">{c.image} · {c.status}{c.ports ? ` · ${c.ports}` : ""}</p>
              </div>
              <div className="flex flex-wrap gap-1.5">
                <button className="btn-ghost py-1 text-xs" onClick={() => act(c.name, "logs")}><Terminal className="h-3.5 w-3.5" /> Logs</button>
                <button className="btn-ghost py-1 text-xs" onClick={() => act(c.name, "restart")}><RotateCw className="h-3.5 w-3.5" /> Restart</button>
                {c.state === "running"
                  ? <button className="btn-ghost py-1 text-xs" onClick={() => act(c.name, "stop")}><Square className="h-3.5 w-3.5" /> Stop</button>
                  : <button className="btn-ghost py-1 text-xs" onClick={() => act(c.name, "start")}><Play className="h-3.5 w-3.5" /> Start</button>}
              </div>
            </li>
          ))}
        </ul>
      )}
      {logs && (
        <div className="border-t border-white/[0.06]">
          <div className="flex items-center justify-between px-5 py-2 text-xs text-slate-400">
            <span>Last 300 lines of <b className="font-mono text-slate-200">{logs.name}</b></span>
            <button className="text-indigo-300" onClick={() => setLogs(null)}>Close</button>
          </div>
          <pre className="max-h-80 overflow-auto bg-black/40 px-4 py-3 font-mono text-[11.5px] text-slate-300">{logs.text || "(empty)"}</pre>
        </div>
      )}
    </Card>
  );
}

function Settings({ server, onChanged, onRemoved, show }) {
  const [form, setForm] = useState({});
  useEffect(() => {
    setForm({ host: server.host, username: server.username, ssh_port: String(server.ssh_port),
      address: server.custom_address ? server.address : "", port_base: String(server.port_base), key: "" });
  }, [server.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const save = async (extra = {}) => {
    try {
      await api.patch(`/api/servers/${server.id}`, {
        host: form.host, username: form.username, ssh_port: Number(form.ssh_port), address: form.address,
        port_base: Number(form.port_base), private_key: form.key || null, actor: operatorName(), ...extra,
      });
      show("Saved. Check the server again to confirm it still connects.", "success");
      setForm({ ...form, key: "" });
      onChanged();
    } catch (e) { show(e.message, "error"); }
  };
  const remove = async () => {
    if (!window.confirm(`Remove ${server.name} from DevNexa? Nothing on the server itself is deleted.`)) return;
    try { await api.del(`/api/servers/${server.id}?actor=${encodeURIComponent(operatorName())}`); onRemoved(); } catch (e) { show(e.message, "error"); }
  };
  return (
    <Card title="Connection settings" icon={Wrench}>
      <div className="grid gap-3 sm:grid-cols-2">
        <label><span className="label">Host</span><input className="input font-mono" value={form.host || ""} onChange={set("host")} /></label>
        <label><span className="label">SSH user</span><input className="input font-mono" value={form.username || ""} onChange={set("username")} /></label>
        <label><span className="label">SSH port</span><input className="input" value={form.ssh_port || ""} onChange={set("ssh_port")} /></label>
        <label><span className="label">First app port</span><input className="input" value={form.port_base || ""} onChange={set("port_base")} /></label>
        <label className="sm:col-span-2"><span className="label">App address (empty: same as host)</span><input className="input font-mono" value={form.address || ""} onChange={set("address")} /></label>
        <label className="sm:col-span-2"><span className="label">Replace the private key (optional)</span>
          <textarea className="input h-20 font-mono text-xs" placeholder="leave empty to keep the current key" value={form.key || ""} onChange={set("key")} />
        </label>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <button className="btn-primary" onClick={() => save()}>Save</button>
        <button className="btn-ghost" onClick={() => window.confirm("Generate a new key? You will need to authorize the new public key on the server.") && save({ regenerate_key: true })}><KeyRound className="h-4 w-4" /> New key</button>
        <button className="btn-danger ml-auto" onClick={remove} disabled={server.apps.length > 0} title={server.apps.length ? "Stop the apps on it first" : ""}><Trash2 className="h-4 w-4" /> Remove</button>
      </div>
    </Card>
  );
}

function Detail({ id, onRemoved, show }) {
  const { data: server, error, reload } = usePoll(`/api/servers/${id}`, 4000, [id]);
  const [check, setCheck] = useState(null);
  const [checking, setChecking] = useState(false);
  const [preset, setPreset] = useState(null);
  const run = async () => {
    setChecking(true);
    try { const r = await api.post(`/api/servers/${id}/check`); setCheck(r); reload(); } catch (e) { show(e.message, "error"); } finally { setChecking(false); }
  };
  useEffect(() => { setCheck(null); }, [id]);
  useEffect(() => { if (server && !check && server.status !== "new") run(); }, [server?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  if (error) return <ErrorBox error={error} />;
  if (!server) return <Loading />;
  const f = server.facts || {};
  const job = server.job;
  const installDocker = async () => {
    if (!window.confirm(`Install Docker on ${server.name} with get.docker.com? The SSH user needs root or passwordless sudo.`)) return;
    try { await api.post(`/api/servers/${id}/install-docker`, { actor: operatorName() }); show("Installing Docker…", "success"); reload(); } catch (e) { show(e.message, "error"); }
  };
  return (
    <div className="space-y-4">
      <Card bodyClass="p-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <p className="flex flex-wrap items-center gap-2 text-lg font-semibold text-white">{server.name} <ServerStatus status={server.status} /></p>
            <p className="font-mono text-xs text-slate-500">{server.username}@{server.host}:{server.ssh_port} · apps on {server.address}:{server.port_base}+</p>
            {server.checked_at && <p className="text-xs text-slate-500">Checked {ago(server.checked_at)}</p>}
            {server.error && <p className="mt-2 text-sm text-rose-300">{server.error}</p>}
          </div>
          <div className="flex flex-wrap gap-2">
            <button className="btn-primary py-1.5 text-xs" onClick={run} disabled={checking}><RefreshCw className={`h-3.5 w-3.5 ${checking ? "animate-spin" : ""}`} /> {checking ? "Checking…" : "Check connection"}</button>
            {server.status === "no_docker" && <button className="btn-ghost py-1.5 text-xs" onClick={installDocker} disabled={job?.status === "running"}><Download className="h-3.5 w-3.5" /> Install Docker</button>}
            {server.status === "online" && <Link className="btn-ghost py-1.5 text-xs" to="/deploy">Deploy an app here</Link>}
          </div>
        </div>
        {server.status !== "offline" && f.os && (
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Fact icon={ServerIcon} label="System" value={f.os} hint={`${f.hostname || ""} · ${f.arch || ""}`} />
            <Fact icon={Cpu} label="CPU" value={f.cpus ? `${f.cpus} cores` : null} hint={f.load ? `load ${f.load}` : null} />
            <Fact icon={MemoryStick} label="Memory" value={f.mem_total_mb ? `${(f.mem_used_mb / 1024).toFixed(1)} / ${(f.mem_total_mb / 1024).toFixed(1)} GB` : null} />
            <Fact icon={HardDrive} label="Disk /" value={f.disk_free ? `${f.disk_free} free` : null} hint={f.disk_total ? `of ${f.disk_total} (${f.disk_used_pct} used)` : null} />
            <Fact label="Docker" value={f.docker || "not usable"} hint={f.containers != null ? `${f.containers_running}/${f.containers} containers running` : null} />
            <Fact label="Uptime" value={f.uptime?.replace(/^up /, "")} hint={f.kernel} />
          </div>
        )}
      </Card>

      {server.status !== "online" && (
        <Card title="Let DevNexa log in" icon={KeyRound}
          subtitle={`Run this once on ${server.host} as ${server.username} (for example in the EC2 console or your current SSH session), then press Check connection.`}>
          <div className="relative">
            <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-lg bg-black/40 p-3 pr-10 font-mono text-[11.5px] text-slate-200">{server.authorize_command}</pre>
            <button className="absolute right-2 top-2 rounded p-1 text-slate-400 hover:text-white" onClick={() => copy(server.authorize_command, show)} aria-label="Copy"><Copy className="h-4 w-4" /></button>
          </div>
          <ul className="mt-3 list-disc space-y-1 pl-5 text-xs text-slate-400">
            <li>The firewall (or cloud security group) must allow SSH from the DevNexa server, and the app ports ({server.port_base} and up) if apps should be reachable.</li>
            <li>The SSH user must be able to run Docker (root, or a member of the docker group). If Docker is missing, use Install Docker after the first check.</li>
          </ul>
        </Card>
      )}

      {job && (
        <Card title={job.kind === "install_docker" ? "Install Docker" : "Job"} icon={Download}
          action={<StatusBadge status={job.status === "succeeded" ? "verified" : job.status} label={job.status} />}>
          <pre className="max-h-64 overflow-auto rounded-lg bg-black/40 p-3 font-mono text-[11.5px] text-slate-300">{job.output || "Starting…"}</pre>
        </Card>
      )}

      {server.status === "online" && <Containers server={server} containers={check?.containers} onChanged={run} show={show} />}

      {server.status !== "new" && (
        <>
          <div className="flex flex-wrap gap-2 text-xs">
            {[["Disk usage", "df -h"], ["Memory", "free -m"], ["Docker disk", "docker system df"], ["Top processes", "ps aux --sort=-%cpu | head -15"], ["Free Docker space", "docker image prune -f && docker builder prune -f"]].map(([label, cmd]) => (
              <button key={label} className="btn-ghost py-1 text-xs" onClick={() => setPreset({ cmd, at: Date.now() })}>{label}</button>
            ))}
          </div>
          <Console server={server} show={show} preset={preset} />
        </>
      )}

      <Settings server={server} onChanged={reload} onRemoved={onRemoved} show={show} />
    </div>
  );
}

export default function Servers() {
  const { data: list, error, reload } = usePoll("/api/servers", 8000);
  const [selected, setSelected] = useState(null);
  const [toast, show] = useToast();
  useEffect(() => {
    if (list?.length && !list.find((s) => s.id === selected)) setSelected(list[0].id);
  }, [list, selected]);
  const locked = error && /admin key|sign in/i.test(error);
  return (
    <>
      <PageHeader eyebrow="Deploy targets" title="Servers"
        description="Add other machines over SSH, deploy apps to them from the Deploy page, and manage them from here: health, containers, logs and a remote terminal. The Deploy, Monitoring and Fix agents work the same on every server." />
      {locked ? (
        <ErrorBox error={<>Servers can run code, so sign in first on the <Link className="underline" to="/deploy">Deploy page</Link>.</>} />
      ) : error ? <ErrorBox error={error} /> : !list ? <Loading /> : (
        <div className="grid gap-4 xl:grid-cols-[340px_minmax(0,1fr)]">
          <div className="space-y-4">
            <AddServer show={show} onAdded={(id) => { reload(); setSelected(id); }} />
            <Card title="Your servers" icon={ServerIcon} bodyClass="">
              <ul className="divide-y divide-white/[0.04]">
                <li className="flex items-center gap-3 px-5 py-3 text-sm">
                  <CheckCircle2 className="h-4 w-4 text-emerald-400" />
                  <div className="min-w-0 flex-1">
                    <p className="text-slate-100">This DevNexa server</p>
                    <p className="text-xs text-slate-500">Default deploy target</p>
                  </div>
                </li>
                {list.map((s) => (
                  <li key={s.id}>
                    <button onClick={() => setSelected(s.id)} className={`flex w-full items-center gap-3 px-5 py-3 text-left hover:bg-white/[0.03] ${selected === s.id ? "bg-white/[0.04]" : ""}`}>
                      <ServerIcon className="h-4 w-4 text-slate-400" />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm text-slate-100">{s.name}</p>
                        <p className="truncate font-mono text-xs text-slate-500">{s.username}@{s.host}{s.apps.length ? ` · ${s.apps.length} app${s.apps.length > 1 ? "s" : ""}` : ""}</p>
                      </div>
                      <ServerStatus status={s.status} />
                    </button>
                  </li>
                ))}
              </ul>
            </Card>
          </div>
          <div className="min-w-0">
            {selected ? <Detail id={selected} show={show} onRemoved={() => { setSelected(null); reload(); }} />
              : <Card><Empty title="No remote servers yet" icon={ServerIcon}>Add a server to deploy apps to more than one machine.</Empty></Card>}
          </div>
        </div>
      )}
      <Toast toast={toast} />
    </>
  );
}
