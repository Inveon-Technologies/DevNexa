"""Fix Agent: live issue -> patch on a new branch -> pull request -> test URL -> merge on approval.

1. Clone the repo at the live commit and pick the files most related to the issue (page route, section selector,
   names in the error and stack).
2. Propose a patch: the configured LLM, or a mechanical fix for known server errors (e.g. a missing dependency).
3. Commit it to `devnexa/issue-<app>-<issue>`, open a pull request, and deploy that branch as its own app at
   https://<app>-pr-<N>.<domain> so a person can try the fix before it reaches the live URL.
4. Approve: merge the PR, remove the test deployment and redeploy the app (a new version, v<N+1>, with the old one
   kept as a backup). Reject: close the PR and remove the test deployment.

Any branch can get a test URL the same way (`preview_branch`), e.g. a feature a developer pushed by hand.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import session_scope
from ..models import AppDeployment, AppIssue, AppPreview
from ..services import app_doctor, github, merger, releases
from ..services.analyzer import LLMAnalyzer, get_analyzer
from ..services.audit import audit

log = logging.getLogger("devnexa.fixer")
CODE_EXT = {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".py", ".html", ".htm", ".css", ".scss", ".go",
            ".rb", ".php", ".java", ".kt", ".json", ".jinja", ".j2", ".ejs", ".hbs"}
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", "out", "coverage", "__pycache__", ".venv", "venv",
             "vendor", ".cache"}
AUTO_FIX_MIN_COUNT = 3
AUTO_FIX_PER_HOUR = 3


def _token(db: Session, app: str) -> str:
    """GitHub access for work on `app`: its owner's own token (admins' shared connection for older apps)."""
    from ..services import owners

    return github.get_token(db, owners.token_login(db, app))


def now() -> datetime:
    return datetime.now(timezone.utc)


class FixError(Exception):
    pass


# ------------------------------------------------------------ file context
def _tokens(issue: dict) -> list[str]:
    words = []
    words += [w for w in re.split(r"[/_\-.]+", issue.get("page") or "") if len(w) >= 3]
    for sel in (issue.get("selector") or "", issue.get("section") or ""):
        words += re.findall(r"[#.]([A-Za-z][\w-]{2,})", sel)
    words += re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\b", issue.get("message") or "")
    # bundled file names: /assets/Checkout-3f2a9c.js -> Checkout
    words += [re.sub(r"[-.][0-9a-f]{6,}$", "", m) for m in
              re.findall(r"/([A-Za-z][\w-]+?)(?:\.[0-9a-f]{6,})?\.(?:js|jsx|ts|tsx|py|vue)", issue.get("stack") or "")]
    stop = {"error", "failed", "returned", "undefined", "cannot", "read", "properties", "null", "with", "from",
            "this", "that", "function", "object", "uncaught", "typeerror", "referenceerror", "assets", "index",
            "main", "http", "https", "section", "page", "type", "promise", "unhandled", "rejection", "script"}
    out = []
    for w in words:
        lw = w.lower()
        if lw not in stop and lw not in out:
            out.append(lw)
    return out[:25]


def related_files(root: Path, issue: dict, limit: int = 6, max_bytes: int = 30000) -> dict[str, str]:
    toks = _tokens(issue)
    scored = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in CODE_EXT or any(d in SKIP_DIRS for d in p.parts):
            continue
        if p.stat().st_size > max_bytes or p.name.endswith((".lock", "-lock.json", ".min.js")):
            continue
        rel = str(p.relative_to(root))
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        low, name = text.lower(), rel.lower()
        score = sum(5 for t in toks if t in name) + sum(min(3, low.count(t)) for t in toks)
        if score:
            scored.append((score, rel, text))
    scored.sort(key=lambda x: -x[0])
    out = {rel: text for _s, rel, text in scored[:limit]}
    for key in ("package.json", "requirements.txt"):
        if (root / key).is_file() and key not in out and (root / key).stat().st_size < max_bytes:
            out[key] = (root / key).read_text(errors="replace")
    return out


LLM_SYSTEM = """You are the Fix Agent of DevNexa, a DevOps platform. A deployed web app has a live issue,
captured from visitors' browsers, the server logs, a page scan, or a person who pointed at a broken section.
Find the cause in the given files and fix it with the smallest safe change.
Reply with one JSON object and nothing else, with keys: title (short, imperative, e.g. "Handle empty cart on
checkout"), root_cause (1-2 sentences), suggestion (what the change does), confidence (0..1),
patch (list of {path, content} with the FULL new content of each changed file; at most 3 files; [] if you
cannot see the cause in the files given). Only edit files that were given to you."""


def propose(issue: dict, root: Path | None, hint: str | None = None) -> dict:
    """Return {title, root_cause, suggestion, confidence, provider, patch: {path: content} | None}."""
    files = related_files(root, issue) if root else {}
    analyzer = get_analyzer()
    if isinstance(analyzer, LLMAnalyzer) and files:
        try:
            text = analyzer._complete(LLM_SYSTEM, json.dumps({
                "issue": {k: issue.get(k) for k in ("kind", "message", "page", "section", "selector", "stack",
                                                     "count", "sample")},
                "developer_hint": hint, "files": files,
            })[:80000])
            d = analyzer._parse(text)
            patch = {}
            for item in (d.get("patch") or [])[:3]:
                path = str(item.get("path", "")).lstrip("/")
                if path in files and isinstance(item.get("content"), str) and item["content"] != files[path]:
                    patch[path] = item["content"]
            return {"title": str(d.get("title") or f"Fix {issue['kind']} on {issue['page']}")[:200],
                    "root_cause": str(d.get("root_cause") or ""), "suggestion": str(d.get("suggestion") or ""),
                    "confidence": max(0.0, min(1.0, float(d.get("confidence") or 0.5))), "provider": analyzer.name,
                    "patch": patch or None, "files_considered": list(files)}
        except Exception as e:  # noqa: BLE001 - fall back to the heuristic diagnosis
            log.warning("LLM fix proposal failed: %s", e)
    text = "\n".join(filter(None, [issue.get("stack"), issue.get("message")]))
    diag = app_doctor.heuristic("runtime", text)
    patch = app_doctor.heuristic_patch(diag, root) if root else None
    reason = "" if patch else (" Set DEVNEXA_LLM_PROVIDER (anthropic or openai) so the Fix Agent can write code "
                               "changes for issues like this." if not isinstance(analyzer, LLMAnalyzer) else
                               " The agent could not locate the cause in the repository.")
    return {"title": diag["title"] if patch else f"Fix {issue['kind'].replace('_', ' ')} on {issue['page']}",
            "root_cause": diag["root_cause"] if diag["category"] != "unknown" else "Not determined automatically.",
            "suggestion": (diag["suggestion"] if diag["category"] != "unknown" else "A developer needs to look.") + reason,
            "confidence": diag["confidence"], "provider": "heuristic", "patch": patch, "files_considered": list(files)}


# --------------------------------------------------------------- fix flow
def _clone(token: str, repo: str, ref: str, dest: Path) -> None:
    from . import deploy

    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    auth = f"x-access-token:{token}@" if token else ""
    code, text = deploy.executor.run(["git", "clone", "--depth", "1", "--branch", ref,
                                      f"https://{auth}github.com/{repo}.git", str(dest)], timeout=300)
    if code != 0:
        raise FixError("git clone failed: " + (text.replace(token, "***") if token else text)[-300:])


def _set_fix(issue_id: int, **fields) -> None:
    with session_scope() as db:
        issue = db.get(AppIssue, issue_id)
        issue.fix = {**(issue.fix or {}), **fields}
        if "issue_status" in fields:
            issue.status = issue.fix.pop("issue_status")


def start_fix(issue_id: int, actor: str = "operator", hint: str | None = None) -> None:
    """Background job: diagnose the issue, open a fix PR and deploy it to a test URL."""
    s = get_settings()
    with session_scope() as db:
        issue = db.get(AppIssue, issue_id)
        live = releases.live_deployment(db, issue.app)
        base = live or db.scalars(select(AppDeployment).where(AppDeployment.app == issue.app)
                                  .order_by(AppDeployment.id.desc())).first()
        if not base:
            issue.fix = {"status": "failed", "error": "The app has no deployment to fix.", "at": now().isoformat()}
            return
        data = {k: getattr(issue, k) for k in ("id", "app", "kind", "message", "page", "section", "selector",
                                               "stack", "count", "sample")}
        repo, branch, root_dir, sha = base.repo, base.branch, base.root_dir, base.commit_sha
        token = _token(db, issue.app)
        issue.status = "fixing"
        issue.fix = {"status": "diagnosing", "started_by": actor, "at": now().isoformat(), "hint": hint}
        audit(db, actor, "app.issue.fix", "app_issue", issue_id, f"Fix Agent started on '{issue.message[:80]}'")
    work = Path(s.apps_workdir) / f"fix-{issue_id}"
    try:
        _clone(token, repo, branch, work)
        root = work / root_dir if root_dir else work
        prop = propose(data, root, hint)
        public = {k: v for k, v in prop.items() if k != "patch"}
        if not prop.get("patch"):
            _set_fix(issue_id, status="no_patch", diagnosis=public, issue_status="open")
            with session_scope() as db:
                audit(db, "fix-agent", "app.issue.no_patch", "app_issue", issue_id, prop["suggestion"][:200])
            return
        patch = {(f"{root_dir}/{p}" if root_dir else p): c for p, c in prop["patch"].items()}
        _set_fix(issue_id, status="opening_pr", diagnosis=public, files=list(patch))
        if not sha:
            sha = github.branch_sha(token, repo, branch)
        fix_branch = f"devnexa/issue-{data['app']}-{issue_id}"
        github.commit_files(token, repo, sha, fix_branch, patch, f"fix({data['app']}): {prop['title']}")
        body = (f"DevNexa's Fix Agent found this issue on the live app **{data['app']}**:\n\n"
                f"- **What:** {data['kind'].replace('_', ' ')}: `{data['message'][:300]}`\n"
                f"- **Where:** page `{data['page']}`" + (f", section `{data['section'] or data['selector']}`"
                                                          if (data['section'] or data['selector']) else "") +
                f"\n- **Seen:** {data['count']} times\n\n**Root cause:** {prop['root_cause']}\n\n"
                f"**Change:** {prop['suggestion']}\n\n**Confidence:** {prop['confidence']:.2f} ({prop['provider']})\n\n"
                "DevNexa deploys this branch to a test URL. Approving it in DevNexa merges this PR and redeploys the app.")
        pr = github.find_or_open_pr(token, repo, fix_branch, branch, f"fix: {prop['title']} (DevNexa)", body)
        _set_fix(issue_id, status="preview", branch=fix_branch, pr_number=pr["number"], pr_url=pr["url"])
        preview_id = _create_preview(data["app"], repo, branch, fix_branch, pr, prop["title"], "fix-agent", issue_id)
        _set_fix(issue_id, preview_id=preview_id, issue_status="in_preview")
        with session_scope() as db:
            db.get(AppIssue, issue_id).preview_id = preview_id
    except (FixError, github.GitHubError) as e:
        _set_fix(issue_id, status="failed", error=str(e), issue_status="open")
    except Exception as e:  # pragma: no cover - shown to the user rather than lost
        log.exception("fix %s crashed", issue_id)
        _set_fix(issue_id, status="failed", error=f"internal error: {e}", issue_status="open")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def preview_app_name(app: str, pr_number: int | None, preview_id: int) -> str:
    return f"{app[:40].rstrip('-')}-pr-{pr_number}" if pr_number else f"{app[:40].rstrip('-')}-test-{preview_id}"


def _create_preview(app: str, repo: str, base_branch: str, branch: str, pr: dict | None, title: str, actor: str,
                    issue_id: int | None = None) -> int:
    from . import deploy, runner

    with session_scope() as db:
        base = releases.live_deployment(db, app) or db.scalars(
            select(AppDeployment).where(AppDeployment.app == app).order_by(AppDeployment.id.desc())).first()
        pv = AppPreview(app=app, preview_app="pending", repo=repo, base_branch=base_branch, branch=branch,
                        issue_id=issue_id, pr_number=(pr or {}).get("number"), pr_url=(pr or {}).get("url"),
                        title=title[:300], created_by=actor)
        db.add(pv)
        db.flush()
        pv.preview_app = preview_app_name(app, pv.pr_number, pv.id)
        opts = {k: v for k, v in (base.options or {}).items() if k not in ("reuse_image", "source")}
        opts.update(preview_of=app, preview_id=pv.id)
        dep = deploy.create_deployment(db, repo, branch, pv.preview_app, base.root_dir, dict(base.env or {}), opts,
                                       actor=actor)
        pv.deployment_id = dep.id
        audit(db, actor, "app.preview", "app_preview", pv.id, f"Test URL for {branch} ({pv.preview_app})")
        pv_id, dep_id = pv.id, dep.id
    runner.submit(deploy.run_deploy, dep_id)
    return pv_id


def preview_branch(db: Session, app: str, branch: str, actor: str) -> int:
    """Deploy any branch of the app's repo to its own test URL, with a PR so approval can merge it."""
    base = releases.live_deployment(db, app) or db.scalars(
        select(AppDeployment).where(AppDeployment.app == app).order_by(AppDeployment.id.desc())).first()
    if not base:
        raise KeyError("app not found")
    if branch == base.branch:
        raise ValueError(f"{branch} is the app's own branch; pick another branch to test.")
    token = _token(db, app)
    repo, base_branch = base.repo, base.branch
    try:
        github.branch_sha(token, repo, branch)
    except github.GitHubError as e:
        if e.status == 404:
            raise ValueError(f"There is no branch called {branch} in {repo}. Push it to GitHub first, "
                             "or check the spelling (branch names are case-sensitive).") from e
        raise
    try:
        pr = github.find_or_open_pr(token, repo, branch, base_branch, f"{branch} (DevNexa test URL)",
                                    f"Opened by DevNexa to test `{branch}` at its own URL before merging into "
                                    f"`{base_branch}`.")
    except github.GitHubError as e:
        if e.status != 422:
            raise
        # GitHub will not open a PR when the branch has nothing new ("No commits between ..."): still give it a
        # test URL, there is just nothing to merge yet.
        pr = None
    return _create_preview(app, repo, base_branch, branch, pr, f"Test {branch}", actor)


def sync(db: Session, pv: AppPreview) -> AppPreview:
    """Derive a preview's status from its latest deployment (pushes to the branch redeploy it)."""
    if pv.status in ("merged", "rejected"):
        return pv
    dep = db.scalars(select(AppDeployment).where(AppDeployment.app == pv.preview_app)
                     .order_by(AppDeployment.id.desc())).first()
    if pv.status in ("resolving", "conflict") and (not dep or dep.id == pv.deployment_id):
        return pv  # the Merge Agent is working on (or gave up on) this branch; a new push redeploys it
    if dep:
        pv.deployment_id = dep.id
        pv.status = {"running": "ready", "failed": "failed", "stopped": "stopped"}.get(dep.status, "deploying")
    return pv


def preview_dict(db: Session, pv: AppPreview) -> dict:
    from ..api.serialize import to_dict

    sync(db, pv)
    dep = db.get(AppDeployment, pv.deployment_id) if pv.deployment_id else None
    out = to_dict(pv)
    out["url"] = dep.url if dep and dep.status == "running" else None
    out["deployment"] = {k: getattr(dep, k) for k in ("id", "status", "health", "error", "commit_sha")} if dep else None
    out["issue_message"] = db.get(AppIssue, pv.issue_id).message[:200] if pv.issue_id and db.get(AppIssue, pv.issue_id) else None
    return out


def _remove_preview_deployments(db: Session, pv: AppPreview) -> None:
    from . import deploy

    for d in db.scalars(select(AppDeployment).where(AppDeployment.app == pv.preview_app,
                                                    AppDeployment.status.in_(("running", "queued", "cloning",
                                                                              "building", "starting")))):
        if d.container:
            from ..services import servers

            try:
                with servers.use_for(d):
                    deploy.executor.run(["docker", "rm", "-f", d.container])
            except servers.ServerError:
                pass
        d.status, d.health = "stopped", "stopped"


def approve(db: Session, preview_id: int, actor: str) -> dict:
    """Merge the tested branch, remove its test deployment and redeploy the app as a new version.

    If the branch conflicts with the base branch, the Merge Agent resolves it first (`resolve_and_merge`, run by
    the caller in the background): the merged branch is re-tested on the test URL and merged once it is healthy."""
    from . import deploy

    pv = db.get(AppPreview, preview_id)
    if not pv:
        raise KeyError("preview not found")
    sync(db, pv)
    if pv.status == "resolving":
        raise ValueError("The Merge Agent is already resolving conflicts on this branch.")
    if pv.status not in ("ready", "conflict"):
        raise ValueError(f"The test deployment is {pv.status}; approve it once its URL works.")
    token = _token(db, pv.app)
    if not pv.pr_number:
        pr = github.find_or_open_pr(token, pv.repo, pv.branch, pv.base_branch, pv.title or pv.branch, "Merged from DevNexa.")
        pv.pr_number, pv.pr_url = pr["number"], pr["url"]
    try:
        state = github.pr_state(token, pv.repo, pv.pr_number)
    except github.GitHubError:  # cannot read it: let the merge call decide
        state = {"merged": False, "mergeable": None, "mergeable_state": None, "head_sha": None}
    merge_sha = state["head_sha"] if state["merged"] else None
    if not state["merged"]:
        if state["mergeable"] is False or state["mergeable_state"] == "dirty":
            return _start_resolving(db, pv, actor, "the branch conflicts with " + pv.base_branch)
        try:
            merge_sha = github.merge_pr(token, pv.repo, pv.pr_number, f"{pv.title or pv.branch} (#{pv.pr_number})")
        except github.GitHubError as e:
            if e.status != 409:
                raise
            return _start_resolving(db, pv, actor, str(e))
    _remove_preview_deployments(db, pv)
    if pv.branch.startswith("devnexa/"):
        github.delete_branch(token, pv.repo, pv.branch)
    pv.status, pv.decided_by, pv.decided_at = "merged", actor, now()
    if pv.issue_id and (issue := db.get(AppIssue, pv.issue_id)):
        issue.status = "resolved"
        issue.fix = {**(issue.fix or {}), "status": "merged", "merged_by": actor, "merged_at": now().isoformat(),
                     "merge_sha": merge_sha}
    base = releases.live_deployment(db, pv.app) or db.scalars(
        select(AppDeployment).where(AppDeployment.app == pv.app).order_by(AppDeployment.id.desc())).first()
    opts = {k: v for k, v in (base.options or {}).items() if k not in ("reuse_image", "source")}
    opts["source"] = "fix-merge" if pv.issue_id else "merge"
    dep = deploy.create_deployment(db, base.repo, base.branch, pv.app, base.root_dir, dict(base.env or {}), opts,
                                   actor=actor)
    audit(db, actor, "app.preview.merged", "app_preview", pv.id,
          f"Merged PR #{pv.pr_number} ({pv.branch}) into {pv.base_branch}; redeploying {pv.app} as deploy #{dep.id}")
    return {"status": "merged", "deployment_id": dep.id, "pr_url": pv.pr_url}


def _start_resolving(db: Session, pv: AppPreview, actor: str, why: str) -> dict:
    pv.status, pv.note = "resolving", f"Merge blocked ({why}). The Merge Agent is merging {pv.base_branch} into " \
                                      f"{pv.branch} and resolving conflicts; the result is re-tested before it merges."
    audit(db, "merge-agent", "app.preview.resolving", "app_preview", pv.id, f"{pv.branch}: {why}")
    return {"status": "resolving_conflicts", "resolve": pv.id, "pr_url": pv.pr_url}


def resolve_and_merge(preview_id: int, actor: str, attempt: int = 1) -> None:
    """Background: merge the base into the PR branch with conflict resolution, push it, re-test it on the test
    URL; `run_deploy` calls `auto_merge` once that test deploy is healthy."""
    from . import deploy, runner

    with session_scope() as db:
        pv = db.get(AppPreview, preview_id)
        if attempt > 3:  # the base branch keeps moving, or GitHub keeps reporting a conflict we cannot see
            pv.status, pv.note = "conflict", "Gave up after 3 merge attempts; merge this pull request on GitHub."
            return
        repo, head, base, number = pv.repo, pv.branch, pv.base_branch, pv.pr_number
        token = _token(db, pv.app)
    try:
        res = merger.update_branch(token, repo, head, base)
    except merger.MergeError as e:
        with session_scope() as db:
            pv = db.get(AppPreview, preview_id)
            pv.status, pv.note = "conflict", f"Could not merge automatically: {e}"
            audit(db, "merge-agent", "app.preview.conflict", "app_preview", preview_id, str(e)[:500])
        if number:
            try:
                github.comment_pr(token, repo, number, _merge_comment(
                    f"DevNexa could not merge `{base}` into `{head}` automatically. A developer needs to resolve:",
                    [f"`{p}`" for p in e.unresolved] or [str(e)],
                    [f"`{r['path']}`: {r['strategy']}" for r in e.resolved]))
            except github.GitHubError:
                pass
        return
    except Exception as e:  # noqa: BLE001 - shown on the preview instead of failing silently
        log.exception("merge agent crashed on preview %s", preview_id)
        with session_scope() as db:
            pv = db.get(AppPreview, preview_id)
            pv.status, pv.note = "conflict", f"Merge Agent error: {e}"
        return
    if res.status == "up_to_date":  # GitHub's mergeability was stale: try the merge again
        with session_scope() as db:
            pv = db.get(AppPreview, preview_id)
            pv.status, pv.note = "ready", res.summary()
        return auto_merge(preview_id, actor, attempt)
    if number and res.files:
        try:
            github.comment_pr(token, repo, number, _merge_comment(
                f"DevNexa merged `{base}` into `{head}` and resolved the conflicts below. The branch is being "
                "re-tested on its test URL and merges automatically once it is healthy.", [],
                [f"`{f['path']}`: {f['strategy']} ({f['detail']})" for f in res.files]))
        except github.GitHubError:
            pass
    with session_scope() as db:
        pv = db.get(AppPreview, preview_id)
        last = db.get(AppDeployment, pv.deployment_id) if pv.deployment_id else None
        opts = {k: v for k, v in ((last.options if last else None) or {}).items() if k not in ("reuse_image", "source")}
        opts.update(preview_of=pv.app, preview_id=pv.id, merge_after={"preview_id": pv.id, "actor": actor,
                                                                         "attempt": attempt})
        dep = deploy.create_deployment(db, repo, head, pv.preview_app, last.root_dir if last else "",
                                       dict((last.env if last else None) or {}), opts, actor="merge-agent")
        pv.deployment_id, pv.status, pv.note = dep.id, "deploying", res.summary() + " Re-testing before merging."
        audit(db, "merge-agent", "app.preview.resolved", "app_preview", pv.id, res.summary()[:500])
        dep_id = dep.id
    runner.submit(deploy.run_deploy, dep_id)


def _merge_comment(intro: str, todo: list[str], done: list[str]) -> str:
    parts = [intro]
    if todo:
        parts.append("\n".join(f"- {x}" for x in todo))
    if done:
        parts.append("Resolved automatically:\n" + "\n".join(f"- {x}" for x in done))
    parts.append("---\n_DevNexa Merge Agent_")
    return "\n\n".join(parts)


def auto_merge(preview_id: int, actor: str, attempt: int = 1) -> None:
    """The re-tested branch is healthy: merge it and roll the app out (the person already approved it)."""
    from . import deploy, runner

    try:
        with session_scope() as db:
            res = approve(db, preview_id, actor)
    except (ValueError, KeyError, github.GitHubError) as e:
        with session_scope() as db:
            pv = db.get(AppPreview, preview_id)
            if pv:
                pv.note = f"Re-tested after resolving conflicts, but the merge failed: {e}"
        return
    if res.get("resolve"):
        runner.submit(resolve_and_merge, preview_id, actor, attempt + 1)  # base moved again meanwhile
    elif res.get("deployment_id"):
        runner.submit(deploy.run_deploy, res["deployment_id"])


def reject(db: Session, preview_id: int, actor: str, reason: str = "") -> dict:
    pv = db.get(AppPreview, preview_id)
    if not pv:
        raise KeyError("preview not found")
    if pv.status in ("merged", "rejected"):
        raise ValueError(f"preview is already {pv.status}")
    token = _token(db, pv.app)
    if pv.pr_number and pv.branch.startswith("devnexa/"):
        github.close_pr(token, pv.repo, pv.pr_number, f"Rejected in DevNexa by {actor}." + (f" {reason}" if reason else ""))
        github.delete_branch(token, pv.repo, pv.branch)
    _remove_preview_deployments(db, pv)
    pv.status, pv.decided_by, pv.decided_at, pv.note = "rejected", actor, now(), reason or None
    if pv.issue_id and (issue := db.get(AppIssue, pv.issue_id)):
        issue.status = "open"
        issue.fix = {**(issue.fix or {}), "status": "rejected", "rejected_by": actor}
    audit(db, actor, "app.preview.rejected", "app_preview", pv.id, f"Rejected {pv.branch}" + (f": {reason}" if reason else ""))
    return {"status": "rejected"}


# ------------------------------------------------------------ automation
def should_auto_fix(db: Session, issue: AppIssue) -> bool:
    """Repeated errors on a live app get a fix branch and test URL without anyone asking (merging still needs a
    person). Limited per app and hour so a noisy app cannot run up LLM or build costs."""
    if issue.status != "open" or issue.fix or issue.severity != "error" or issue.count < AUTO_FIX_MIN_COUNT:
        return False
    if re.search(r"-(pr|test|fix)-\d+$", issue.app):
        return False  # issues on test URLs belong to the fix being tested
    if not releases.get_settings(db, issue.app)["steps"]["auto_fix"]:
        return False
    if not _token(db, issue.app):
        return False
    since = (now() - timedelta(hours=1)).isoformat()
    started = [i for i in db.scalars(select(AppIssue).where(AppIssue.app == issue.app))
               if (i.fix or {}).get("started_by") == "fix-agent" and (i.fix or {}).get("at", "") >= since]
    return len(started) < AUTO_FIX_PER_HOUR
