"""GitHub account connection: a personal access token stored in the kv_store, repo/branch listing,
and opening a fix pull request on the user's repo (only after a human approves it)."""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import KeyValue

API = "https://api.github.com"
KV_KEY = "github"


class GitHubError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "DevNexa"}


def _call(token: str, method: str, path: str, **kw) -> httpx.Response:
    try:
        r = httpx.request(method, f"{API}{path}", headers=_headers(token), timeout=20.0, **kw)
    except httpx.HTTPError as e:
        raise GitHubError(f"GitHub is unreachable: {e}", 502) from e
    if r.status_code == 401:
        raise GitHubError("GitHub rejected the token (expired or revoked).", 401)
    return r


def get_token(db: Session) -> str:
    row = db.get(KeyValue, KV_KEY)
    return (row.value or {}).get("token", "") if row else get_settings().github_token


def connection(db: Session) -> dict:
    row = db.get(KeyValue, KV_KEY)
    if row and row.value.get("token"):
        v = row.value
        return {"connected": True, "login": v.get("login"), "name": v.get("name"), "avatar_url": v.get("avatar_url"),
                "scopes": v.get("scopes"), "connected_at": v.get("connected_at"), "token_hint": mask(v["token"])}
    if get_settings().github_token:
        return {"connected": True, "login": None, "source": "env", "token_hint": mask(get_settings().github_token)}
    return {"connected": False}


def mask(token: str) -> str:
    return f"{token[:4]}…{token[-4:]}" if len(token) > 10 else "…"


def connect(db: Session, token: str) -> dict:
    token = token.strip()
    if not token:
        raise GitHubError("Paste a GitHub personal access token.")
    r = _call(token, "GET", "/user")
    if r.status_code != 200:
        raise GitHubError(f"GitHub returned {r.status_code} for /user.")
    u = r.json()
    value = {"token": token, "login": u.get("login"), "name": u.get("name"), "avatar_url": u.get("avatar_url"),
             "scopes": r.headers.get("x-oauth-scopes"), "connected_at": datetime.now(timezone.utc).isoformat()}
    row = db.get(KeyValue, KV_KEY)
    if row:
        row.value = value
    else:
        db.add(KeyValue(key=KV_KEY, value=value))
    db.flush()
    return connection(db)


def disconnect(db: Session) -> None:
    row = db.get(KeyValue, KV_KEY)
    if row:
        db.delete(row)
        db.flush()


def list_repos(token: str, pages: int = 3) -> list[dict]:
    out = []
    for page in range(1, pages + 1):
        r = _call(token, "GET", "/user/repos",
                  params={"per_page": 100, "page": page, "sort": "updated",
                          "affiliation": "owner,collaborator,organization_member"})
        if r.status_code != 200:
            raise GitHubError(f"GitHub returned {r.status_code} listing repositories.", r.status_code)
        batch = r.json()
        out += [{"full_name": x["full_name"], "name": x["name"], "private": x["private"],
                 "default_branch": x.get("default_branch") or "main", "language": x.get("language"),
                 "description": x.get("description"), "updated_at": x.get("pushed_at") or x.get("updated_at"),
                 "html_url": x.get("html_url")} for x in batch]
        if len(batch) < 100:
            break
    return out


def list_branches(token: str, full_name: str) -> list[str]:
    r = _call(token, "GET", f"/repos/{full_name}/branches", params={"per_page": 100})
    if r.status_code != 200:
        raise GitHubError(f"GitHub returned {r.status_code} listing branches of {full_name}.", r.status_code)
    return [b["name"] for b in r.json()]


def open_fix_pr(token: str, full_name: str, base_branch: str, base_sha: str, files: dict[str, str],
                title: str, body: str, branch: str) -> str:
    """Commit `files` (path -> new content) to a new branch and open a pull request. Returns its URL."""
    r = _call(token, "POST", f"/repos/{full_name}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha})
    if r.status_code not in (201, 422):  # 422: branch already exists from an earlier attempt
        raise GitHubError(f"Could not create branch {branch}: GitHub {r.status_code} {r.text[:200]}")
    for path, content in files.items():
        cur = _call(token, "GET", f"/repos/{full_name}/contents/{path}", params={"ref": branch})
        payload = {"message": f"fix: {path} (DevNexa agent)", "branch": branch,
                   "content": base64.b64encode(content.encode()).decode()}
        if cur.status_code == 200:
            payload["sha"] = cur.json()["sha"]
        w = _call(token, "PUT", f"/repos/{full_name}/contents/{path}", json=payload)
        if w.status_code not in (200, 201):
            raise GitHubError(f"Could not write {path}: GitHub {w.status_code} {w.text[:200]}")
    pr = _call(token, "POST", f"/repos/{full_name}/pulls",
               json={"title": title, "head": branch, "base": base_branch, "body": body})
    if pr.status_code != 201:
        raise GitHubError(f"Could not open the pull request: GitHub {pr.status_code} {pr.text[:200]}")
    return pr.json()["html_url"]


def recent_commits(token: str, full_name: str, branch: str = "", limit: int = 5) -> list[dict]:
    """The latest commits of a repo with their changed files (incident change evidence)."""
    params = {"per_page": limit, **({"sha": branch} if branch else {})}
    r = _call(token, "GET", f"/repos/{full_name}/commits", params=params)
    if r.status_code != 200:
        raise GitHubError(f"GitHub returned {r.status_code} listing commits of {full_name}.", r.status_code)
    out = []
    for c in r.json()[:limit]:
        d = _call(token, "GET", f"/repos/{full_name}/commits/{c['sha']}")
        files = [f["filename"] for f in d.json().get("files", [])] if d.status_code == 200 else []
        info = c.get("commit") or {}
        out.append({"sha": c["sha"][:7], "message": (info.get("message") or "").splitlines()[0][:200],
                    "author": (c.get("author") or {}).get("login") or (info.get("author") or {}).get("name"),
                    "time": (info.get("author") or {}).get("date"), "files": files,
                    "url": c.get("html_url")})
    return out
