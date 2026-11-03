import { Ban, Check, Github, Shield, ShieldOff, UserPlus, Users as UsersIcon } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, StatusBadge, Toast } from "../components/ui";
import { ago } from "../format";
import { usePoll, useToast } from "../hooks";

const SIGNUP = {
  approval: "Anyone can sign in with GitHub. New accounts wait here until you approve them.",
  open: "Anyone can sign in with GitHub and deploy straight away.",
  invite: "Only logins you add here (or in DEVNEXA_GITHUB_ALLOWED_USERS) can sign in.",
};

function Invite({ onDone, show }) {
  const [login, setLogin] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const u = await api.post("/api/users", { login: login.trim() });
      show(`@${u.login} can now sign in and deploy`, "success");
      setLogin("");
      onDone();
    } catch (err) { show(err.message, "error"); } finally { setBusy(false); }
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2 sm:flex-row">
      <input className="input" placeholder="GitHub login, e.g. octocat" value={login} onChange={(e) => setLogin(e.target.value)} />
      <button className="btn-primary shrink-0" disabled={!login.trim() || busy}><UserPlus className="h-4 w-4" /> Add user</button>
    </form>
  );
}

function UserRow({ u, act }) {
  const set = (body, msg) => act(() => api.patch(`/api/users/${u.key}`, body), msg);
  return (
    <li className="flex flex-col gap-3 px-5 py-3 sm:flex-row sm:items-center">
      <div className="flex min-w-0 flex-1 items-center gap-3">
        {u.avatar_url ? <img src={u.avatar_url} alt="" className="h-9 w-9 shrink-0 rounded-full" />
          : <div className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-white/[0.04]"><Github className="h-4 w-4 text-slate-500" /></div>}
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2 text-sm text-slate-100">
            <a href={`https://github.com/${u.login}`} target="_blank" rel="noreferrer" className="font-medium hover:underline">@{u.login}</a>
            {u.name && <span className="text-slate-400">{u.name}</span>}
            <StatusBadge status={u.status} />
            {u.role === "admin" && <Badge tone="violet">Admin{u.built_in_admin ? " · owner" : ""}</Badge>}
          </p>
          <p className="truncate text-xs text-slate-500">
            {u.apps} app{u.apps === 1 ? "" : "s"} · {u.servers} server{u.servers === 1 ? "" : "s"} ·{" "}
            {u.last_login_at ? `last signed in ${ago(u.last_login_at)}` : "has not signed in yet"}
            {u.decided_by ? ` · last changed by @${u.decided_by}` : ""}
          </p>
        </div>
      </div>
      {!u.built_in_admin && (
        <div className="flex flex-wrap gap-1">
          {u.status !== "active" && (
            <button className="btn-primary py-1.5 text-xs" onClick={() => set({ status: "active" }, `@${u.login} approved`)}>
              <Check className="h-3.5 w-3.5" /> {u.status === "blocked" ? "Unblock" : "Approve"}
            </button>
          )}
          {u.status === "active" && (u.role === "admin" ? (
            <button className="btn-ghost py-1.5 text-xs" onClick={() => set({ role: "member" }, `@${u.login} is now a member`)}>
              <ShieldOff className="h-3.5 w-3.5" /> Make member
            </button>
          ) : (
            <button className="btn-ghost py-1.5 text-xs"
              onClick={() => window.confirm(`Make @${u.login} an admin? Admins see and manage every user's apps, servers and backups.`) && set({ role: "admin" }, `@${u.login} is now an admin`)}>
              <Shield className="h-3.5 w-3.5" /> Make admin
            </button>
          ))}
          {u.status !== "blocked" && (
            <button className="btn-ghost py-1.5 text-xs text-rose-300"
              onClick={() => window.confirm(`Block @${u.login}? They are signed out at once. Their running apps keep running.`) && set({ status: "blocked" }, `@${u.login} blocked`)}>
              <Ban className="h-3.5 w-3.5" /> {u.status === "pending" ? "Decline" : "Block"}
            </button>
          )}
        </div>
      )}
    </li>
  );
}

export default function UsersPage() {
  const { data, error, reload } = usePoll("/api/users", 10000);
  const [toast, show] = useToast();
  const act = async (fn, msg) => { try { await fn(); show(msg, "success"); reload(); } catch (e) { show(e.message, "error"); } };
  const pending = data?.users.filter((u) => u.status === "pending") || [];
  const others = data?.users.filter((u) => u.status !== "pending") || [];
  return (
    <>
      <PageHeader eyebrow="Admin" title="Users"
        description="Everyone who signed in with GitHub. Members see and deploy only their own repos, apps, servers and backups. Admins see everything." />
      <ErrorBox error={error} />
      {!data ? (!error && <Loading />) : (
        <div className="space-y-4">
          <Card title="Who can sign in" icon={Github}
            subtitle={`${SIGNUP[data.signup]} Change it with DEVNEXA_GITHUB_SIGNUP (approval, open or invite) in the server's .env.`}>
            <Invite onDone={reload} show={show} />
            <p className="mt-2 text-xs text-slate-500">
              Each member can own up to {data.max_apps || "any number of"} apps and {data.max_servers || "any number of"} servers
              (DEVNEXA_USER_MAX_APPS, DEVNEXA_USER_MAX_SERVERS).
            </p>
          </Card>
          {pending.length > 0 && (
            <Card title={`Waiting for approval (${pending.length})`} icon={UserPlus} bodyClass="">
              <ul className="divide-y divide-white/[0.06]">{pending.map((u) => <UserRow key={u.key} u={u} act={act} />)}</ul>
            </Card>
          )}
          <Card title="Accounts" icon={UsersIcon} bodyClass="">
            {others.length ? <ul className="divide-y divide-white/[0.06]">{others.map((u) => <UserRow key={u.key} u={u} act={act} />)}</ul>
              : <Empty title="Nobody else yet" icon={UsersIcon}>When people sign in with GitHub they show up here.</Empty>}
          </Card>
        </div>
      )}
      <Toast toast={toast} />
    </>
  );
}
