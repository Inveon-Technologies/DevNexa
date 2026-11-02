"""GitHub account connection (Sign in with GitHub, or a pasted personal access token), repo/branch listing,
and pushing fix branches / opening pull requests on the user's repo. Nothing is ever pushed to the base branch."""

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


def user_key(login: str) -> str:
    return f"{KV_KEY}:user:{login.lower()}"


def get_token(db: Session, login: str | None = None) -> str:
    """The signed-in user's OAuth token if there is one, else the shared connection (pasted token / .env)."""
    if login:
        row = db.get(KeyValue, user_key(login))
        if row and row.value.get("token"):
            return row.value["token"]
    row = db.get(KeyValue, KV_KEY)
    return (row.value or {}).get("token", "") if row else get_settings().github_token


def profile(token: str) -> tuple[dict, str | None]:
    r = _call(token, "GET", "/user")
    if r.status_code != 200:
        raise GitHubError(f"GitHub returned {r.status_code} for /user.")
    return r.json(), r.headers.get("x-oauth-scopes")


def save_user_token(db: Session, token: str) -> dict:
    """Store an OAuth token for the account it belongs to (Sign in with GitHub). Returns the profile."""
    u, scopes = profile(token)
    value = {"token": token, "login": u.get("login"), "name": u.get("name"), "avatar_url": u.get("avatar_url"),
             "scopes": scopes, "connected_at": datetime.now(timezone.utc).isoformat(), "source": "oauth"}
    row = db.get(KeyValue, user_key(value["login"]))
    if row:
        row.value = value
    else:
        db.add(KeyValue(key=user_key(value["login"]), value=value))
    # Background work (monitoring fixes, push-to-redeploy) uses the shared connection when it has no user,
    # so the first sign-in also fills it if nothing was connected before.
    if not db.get(KeyValue, KV_KEY) and not get_settings().github_token:
        db.add(KeyValue(key=KV_KEY, value=dict(value)))
    db.flush()
    return value


def connection(db: Session, login: str | None = None) -> dict:
    if login:
        row = db.get(KeyValue, user_key(login))
        if row and row.value.get("token"):
            v = row.value
            return {"connected": True, "login": v.get("login"), "name": v.get("name"),
                    "avatar_url": v.get("avatar_url"), "scopes": v.get("scopes"), "source": "oauth",
                    "connected_at": v.get("connected_at"), "token_hint": mask(v["token"])}
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
    u, scopes = profile(token)
    value = {"token": token, "login": u.get("login"), "name": u.get("name"), "avatar_url": u.get("avatar_url"),
             "scopes": scopes, "connected_at": datetime.now(timezone.utc).isoformat()}
    row = db.get(KeyValue, KV_KEY)
    if row:
        row.value = value
    else:
        db.add(KeyValue(key=KV_KEY, value=value))
    db.flush()
    return connection(db)


def disconnect(db: Session, login: str | None = None) -> None:
    row = db.get(KeyValue, user_key(login)) if login else None
    row = row or db.get(KeyValue, KV_KEY)
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


def branch_sha(token: str, full_name: str, branch: str) -> str:
    r = _call(token, "GET", f"/repos/{full_name}/branches/{branch}")
    if r.status_code != 200:
        raise GitHubError(f"GitHub returned {r.status_code} for branch {branch} of {full_name}.", r.status_code)
    return r.json()["commit"]["sha"]


def push_branch(token: str, full_name: str, base_sha: str, files: dict[str, str], branch: str,
                message: str = "fix: {path} (DevNexa agent)") -> None:
    """Create `branch` from `base_sha` (or reuse it) and commit `files` (path -> new content) to it."""
    r = _call(token, "POST", f"/repos/{full_name}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha})
    if r.status_code not in (201, 422):  # 422: branch already exists from an earlier attempt
        raise GitHubError(f"Could not create branch {branch}: GitHub {r.status_code} {r.text[:200]}")
    for path, content in files.items():
        cur = _call(token, "GET", f"/repos/{full_name}/contents/{path}", params={"ref": branch})
        payload = {"message": message.format(path=path), "branch": branch,
                   "content": base64.b64encode(content.encode()).decode()}
        if cur.status_code == 200:
            payload["sha"] = cur.json()["sha"]
        w = _call(token, "PUT", f"/repos/{full_name}/contents/{path}", json=payload)
        if w.status_code not in (200, 201):
            raise GitHubError(f"Could not write {path}: GitHub {w.status_code} {w.text[:200]}")


def open_pr(token: str, full_name: str, base_branch: str, branch: str, title: str, body: str,
            draft: bool = False) -> str:
    """Open a pull request from `branch`, or return the one already open for it. Returns its URL."""
    owner = full_name.split("/")[0]
    cur = _call(token, "GET", f"/repos/{full_name}/pulls", params={"head": f"{owner}:{branch}", "state": "open"})
    if cur.status_code == 200 and cur.json():
        return cur.json()[0]["html_url"]
    pr = _call(token, "POST", f"/repos/{full_name}/pulls",
               json={"title": title, "head": branch, "base": base_branch, "body": body, "draft": draft})
    if pr.status_code == 422 and draft and "draft" in pr.text.lower():  # drafts need a paid plan on private repos
        pr = _call(token, "POST", f"/repos/{full_name}/pulls",
                   json={"title": title, "head": branch, "base": base_branch, "body": body})
    if pr.status_code != 201:
        raise GitHubError(f"Could not open the pull request: GitHub {pr.status_code} {pr.text[:200]}")
    return pr.json()["html_url"]


def open_fix_pr(token: str, full_name: str, base_branch: str, base_sha: str, files: dict[str, str],
                title: str, body: str, branch: str) -> str:
    """Commit `files` (path -> new content) to a new branch and open a pull request. Returns its URL."""
    push_branch(token, full_name, base_sha, files, branch)
    return open_pr(token, full_name, base_branch, branch, title, body)


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


# ------------------------------------------------- fix branches and test URLs
def commit_files(token: str, full_name: str, base_sha: str, branch: str, files: dict[str, str], message: str) -> None:
    """Create `branch` at `base_sha` (if missing) and commit `files` (path -> full new content) to it."""
    r = _call(token, "POST", f"/repos/{full_name}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha})
    if r.status_code not in (201, 422):  # 422: branch already exists from an earlier attempt
        raise GitHubError(f"Could not create branch {branch}: GitHub {r.status_code} {r.text[:200]}")
    for path, content in files.items():
        cur = _call(token, "GET", f"/repos/{full_name}/contents/{path}", params={"ref": branch})
        payload = {"message": f"{message}: {path}", "branch": branch,
                   "content": base64.b64encode(content.encode()).decode()}
        if cur.status_code == 200:
            payload["sha"] = cur.json()["sha"]
        w = _call(token, "PUT", f"/repos/{full_name}/contents/{path}", json=payload)
        if w.status_code not in (200, 201):
            raise GitHubError(f"Could not write {path}: GitHub {w.status_code} {w.text[:200]}")


def find_or_open_pr(token: str, full_name: str, head: str, base: str, title: str, body: str) -> dict:
    """Return the open PR for `head` -> `base`, opening one if there is none. {number, url}."""
    owner = full_name.split("/")[0]
    r = _call(token, "GET", f"/repos/{full_name}/pulls", params={"head": f"{owner}:{head}", "base": base, "state": "open"})
    if r.status_code == 200 and r.json():
        pr = r.json()[0]
        return {"number": pr["number"], "url": pr["html_url"]}
    pr = _call(token, "POST", f"/repos/{full_name}/pulls", json={"title": title, "head": head, "base": base, "body": body})
    if pr.status_code != 201:
        raise GitHubError(f"Could not open the pull request: GitHub {pr.status_code} {pr.text[:200]}")
    return {"number": pr.json()["number"], "url": pr.json()["html_url"]}


def merge_pr(token: str, full_name: str, number: int, title: str, method: str = "squash") -> str:
    r = _call(token, "PUT", f"/repos/{full_name}/pulls/{number}/merge",
              json={"merge_method": method, "commit_title": title})
    if r.status_code != 200:
        msg = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("message")
        raise GitHubError(f"GitHub could not merge PR #{number}: {msg or r.status_code}", 409 if r.status_code in (405, 409) else r.status_code)
    return r.json().get("sha", "")


def close_pr(token: str, full_name: str, number: int, comment: str | None = None) -> None:
    if comment:
        _call(token, "POST", f"/repos/{full_name}/issues/{number}/comments", json={"body": comment})
    _call(token, "PATCH", f"/repos/{full_name}/pulls/{number}", json={"state": "closed"})


def delete_branch(token: str, full_name: str, branch: str) -> None:
    _call(token, "DELETE", f"/repos/{full_name}/git/refs/heads/{branch}")
