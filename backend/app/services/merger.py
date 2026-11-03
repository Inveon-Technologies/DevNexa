"""Merge Agent: bring a pull request up to date with its base branch and resolve merge conflicts.

GitHub refuses to merge a PR whose branch conflicts with the base. DevNexa then clones the PR branch, merges the
base into it (diff3 style, so each conflict shows both sides *and* the common ancestor) and resolves every
conflicted file with the safest rule that fits:

1. Lockfiles (package-lock.json, yarn.lock, poetry.lock, ...): take the base branch's file. The next build
   installs from the merged manifest; if the lockfile is then out of sync the Deploy Agent rebuilds without it.
2. JSON manifests (package.json, composer.json, tsconfig.json ...): a three-way merge key by key. Both sides'
   additions are kept; only a key that both sides changed differently is a real conflict.
3. Line lists (.gitignore, .dockerignore, requirements.txt, .env.example): union of both sides. In
   requirements.txt a package pinned by both sides keeps the PR's pin.
4. Any other text file, hunk by hunk: a side equal to the ancestor loses to the side that changed; identical
   changes or whitespace-only differences are merged; when one side only added lines to the other, the longer one
   wins; two sides that only added imports keep both.
5. Hunks still left go to the configured LLM with the whole file, which must return it without conflict markers.

If anything stays unresolved the merge is aborted and nothing is pushed: the PR is left for a developer with the
list of files. Otherwise the merge commit is pushed to the PR branch (never to the base branch) and the caller
re-tests the branch on its test URL before merging it.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import get_settings
from .analyzer import LLMAnalyzer, get_analyzer

log = logging.getLogger("devnexa.merger")

LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock",
             "poetry.lock", "Pipfile.lock", "uv.lock", "Gemfile.lock", "composer.lock", "Cargo.lock", "go.sum",
             "packages.lock.json", "gradle.lockfile"}
UNION_FILES = {".gitignore", ".dockerignore", ".env.example", ".env.sample", "requirements.txt",
               "requirements-dev.txt", "CODEOWNERS", ".npmignore", ".prettierignore", ".eslintignore"}
JSON_FILES = {"package.json", "composer.json", "tsconfig.json", "jsconfig.json", "deno.json", "app.json",
              "manifest.json", "appsettings.json"}
IMPORT_LINE = re.compile(r"^\s*(import\s|from\s+\S+\s+import\s|const\s+\w+\s*=\s*require\(|#include\s|using\s+[\w.]+;|"
                         r"require\s|require_once\s|use\s+[\w\\]+;|\"[\w./-]+\"\s*$)")
CONFLICT = re.compile(r"^<<<<<<< [^\n]*\n(.*?)^\|\|\|\|\|\|\| [^\n]*\n(.*?)^=======\n(.*?)^>>>>>>> [^\n]*\n?",
                      re.S | re.M)
MARKER = re.compile(r"^(<<<<<<<|=======|>>>>>>>)( |$)", re.M)

LLM_SYSTEM = """You are the Merge Agent of DevNexa. A pull request branch conflicts with its base branch.
You get one file containing git diff3 conflict markers: <<<<<<< ours (the PR branch), ||||||| base (common
ancestor), ======= then theirs (the base branch) >>>>>>>. Resolve every conflict so that BOTH sides' intended
changes are kept and the code stays correct (no duplicate definitions, consistent names, valid syntax).
Reply with one JSON object and nothing else: {"content": "<the full resolved file, no conflict markers>",
"explanation": "<one or two sentences>", "confidence": 0..1}. If the two changes truly cannot both be kept,
set content to "" and explain why."""


class MergeError(Exception):
    """The merge could not be completed; `unresolved` lists files a developer has to fix."""

    def __init__(self, message: str, unresolved: list[str] | None = None, resolved: list[dict] | None = None):
        super().__init__(message)
        self.unresolved = unresolved or []
        self.resolved = resolved or []


@dataclass
class MergeResult:
    status: str  # "up_to_date" | "merged_clean" | "resolved"
    head: str
    base: str
    sha: str | None = None
    files: list[dict] = field(default_factory=list)  # [{path, strategy, detail}]

    def summary(self) -> str:
        if self.status == "up_to_date":
            return f"{self.head} already contains {self.base}."
        if self.status == "merged_clean":
            return f"Merged {self.base} into {self.head} (no conflicts)."
        return f"Merged {self.base} into {self.head} and resolved {len(self.files)} conflicted file(s): " + \
            "; ".join(f"{f['path']} ({f['strategy']})" for f in self.files)

    def to_dict(self) -> dict:
        return {"status": self.status, "head": self.head, "base": self.base, "sha": self.sha, "files": self.files,
                "summary": self.summary()}


# ------------------------------------------------------------------ git plumbing
def remote_url(token: str, repo: str) -> str:
    """Overridden in tests with a local bare repository."""
    auth = f"x-access-token:{token}@" if token else ""
    return f"https://{auth}github.com/{repo}.git"


def _git(work: Path | None, *args: str, token: str = "", timeout: float = 300, check: bool = True) -> str:
    cmd = ["git"] + (["-C", str(work)] if work else []) + ["-c", "user.name=DevNexa Merge Agent",
                                                            "-c", "user.email=merge-agent@devnexa.local",
                                                            "-c", "merge.conflictStyle=diff3", *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired as e:
        raise MergeError(f"git {args[0]} timed out") from e
    out = (p.stdout + p.stderr).replace(token, "***") if token else p.stdout + p.stderr
    if check and p.returncode != 0:
        raise MergeError(f"git {args[0]} failed: {out.strip()[-400:]}")
    return p.stdout if p.returncode == 0 else out


def _stage(work: Path, n: int, path: str) -> str | None:
    p = subprocess.run(["git", "-C", str(work), "show", f":{n}:{path}"], capture_output=True, text=True,
                       errors="replace")
    return p.stdout if p.returncode == 0 else None


# ------------------------------------------------------------------ strategies
def _norm(lines: str) -> str:
    return re.sub(r"\s+", " ", lines).strip()


def resolve_hunk(ours: str, base: str, theirs: str) -> tuple[str | None, str]:
    """Return (resolved text or None, rule used)."""
    if ours == theirs:
        return ours, "same change on both sides"
    if ours == base:
        return theirs, "only the base branch changed it"
    if theirs == base:
        return ours, "only the PR changed it"
    if _norm(ours) == _norm(theirs):
        return ours, "whitespace-only difference"
    o, t = ours.splitlines(keepends=True), theirs.splitlines(keepends=True)
    if not base.strip():  # both sides only added lines here
        if all(IMPORT_LINE.match(x) or not x.strip() for x in o + t):
            merged = o + [x for x in t if x not in o]
            return "".join(merged), "kept both sides' imports"
    if _contains(o, t):
        return ours, "the PR's lines include the base branch's"
    if _contains(t, o):
        return theirs, "the base branch's lines include the PR's"
    return None, "both sides changed the same lines"


def _contains(big: list[str], small: list[str]) -> bool:
    """True when `big` is `small` plus inserted lines (order preserved)."""
    if len(big) <= len(small):
        return False
    it = iter(big)
    return all(any(b == s for b in it) for s in small)


def resolve_text(text: str) -> tuple[str, int]:
    """Resolve the hunks the rules can; returns (text, number of hunks left with markers)."""
    left = 0

    def sub(m: re.Match) -> str:
        nonlocal left
        res, _rule = resolve_hunk(m.group(1), m.group(2), m.group(3))
        if res is None:
            left += 1
            return m.group(0)
        return res

    return CONFLICT.sub(sub, text), left


def merge_json(base, ours, theirs):
    """Three-way merge of parsed JSON. Raises ValueError on a key both sides changed differently."""
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if isinstance(ours, dict) and isinstance(theirs, dict):
        base = base if isinstance(base, dict) else {}
        out = {}
        for k in list(ours) + [k for k in theirs if k not in ours]:
            missing = object()
            b, o, t = base.get(k, missing), ours.get(k, missing), theirs.get(k, missing)
            if o is missing and t is missing:
                continue
            if o is missing:  # deleted by the PR (or added by base)
                if b is missing:
                    out[k] = t
                elif t != b:
                    raise ValueError(k)
                continue
            if t is missing:
                if b is missing:
                    out[k] = o
                elif o != b:
                    raise ValueError(k)
                continue
            try:
                out[k] = merge_json(None if b is missing else b, o, t)
            except ValueError as e:
                raise ValueError(k if e.args[0] == "value" else f"{k}.{e.args[0]}") from None
        return out
    if isinstance(ours, list) and isinstance(theirs, list):
        return ours + [x for x in theirs if x not in ours]
    raise ValueError("value")


def _req_name(line: str) -> str:
    return re.split(r"[\s<>=!~\[;@]", line.strip(), maxsplit=1)[0].lower().replace("_", "-")


def union_lines(ours: str, theirs: str, requirements: bool = False) -> str:
    out = ours.splitlines()
    names = {_req_name(x) for x in out if x.strip() and not x.lstrip().startswith("#")} if requirements else set()
    for line in theirs.splitlines():
        if line in out:
            continue
        if requirements and line.strip() and not line.lstrip().startswith("#") and _req_name(line) in names:
            continue  # pinned by the PR already: keep the PR's pin
        out.append(line)
    return "\n".join(out) + "\n"


def llm_resolve(path: str, text: str) -> tuple[str | None, str]:
    analyzer = get_analyzer()
    if not isinstance(analyzer, LLMAnalyzer):
        return None, "needs a developer (set DEVNEXA_LLM_PROVIDER to let the Merge Agent resolve code conflicts)"
    if len(text) > 60000:
        return None, "file too large for automatic resolution"
    try:
        d = analyzer._parse(analyzer._complete(LLM_SYSTEM, json.dumps({"path": path, "file": text}), max_tokens=16000))
    except Exception as e:  # noqa: BLE001
        log.warning("LLM merge of %s failed: %s", path, e)
        return None, f"LLM could not resolve it ({type(e).__name__})"
    content = d.get("content")
    if not isinstance(content, str) or not content.strip() or MARKER.search(content):
        return None, f"LLM declined: {str(d.get('explanation') or 'no clean result')[:200]}"
    if float(d.get("confidence") or 0) < 0.5:
        return None, f"LLM was not confident: {str(d.get('explanation') or '')[:200]}"
    return content, f"resolved by {analyzer.name}: {str(d.get('explanation') or '')[:200]}"


def resolve_file(work: Path, path: str) -> dict:
    """Resolve one conflicted file in the work tree. Returns {path, strategy, detail, ok}."""
    name = Path(path).name
    f = work / path
    ours, theirs, base = _stage(work, 2, path), _stage(work, 3, path), _stage(work, 1, path)
    if ours is None or theirs is None:  # deleted on one side, changed on the other
        keep = ours if ours is not None else theirs
        side = "PR" if ours is not None else "base branch"
        if keep is None:
            return {"path": path, "strategy": "delete", "detail": "deleted on both sides", "ok": True, "delete": True}
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(keep)
        return {"path": path, "strategy": "keep changed copy",
                "detail": f"deleted on one side, changed on the other: kept the {side}'s version", "ok": True}
    if name in LOCKFILES:
        f.write_text(theirs)
        return {"path": path, "strategy": "lockfile from base",
                "detail": "took the base branch's lockfile; the build reinstalls from the merged manifest", "ok": True}
    json_reason = ""
    if name in JSON_FILES:
        try:
            merged = merge_json(json.loads(base) if base else {}, json.loads(ours), json.loads(theirs))
        except json.JSONDecodeError:  # comments or broken JSON: fall through to the hunk rules
            pass
        except ValueError as e:
            json_reason = f"both sides changed {e.args[0]!r} differently" if e.args[0] != "value" else ""
        else:
            indent = 4 if re.search(r"^ {4}\"", ours, re.M) else 2
            f.write_text(json.dumps(merged, indent=indent, ensure_ascii=False) + "\n")
            return {"path": path, "strategy": "JSON three-way merge", "detail": "kept both sides' keys", "ok": True}
    if name in UNION_FILES:
        f.write_text(union_lines(ours, theirs, requirements=name.startswith("requirements")))
        return {"path": path, "strategy": "union of lines", "detail": "kept every line from both sides", "ok": True}
    text = f.read_text(errors="replace")
    resolved, left = resolve_text(text)
    if left == 0 and not MARKER.search(resolved):
        f.write_text(resolved)
        return {"path": path, "strategy": "hunk rules", "detail": "each conflict had one side that clearly wins",
                "ok": True}
    content, why = llm_resolve(path, resolved)
    if content is not None:
        f.write_text(content)
        return {"path": path, "strategy": "LLM", "detail": why, "ok": True}
    return {"path": path, "strategy": "unresolved", "ok": False,
            "detail": "; ".join(x for x in (json_reason, f"{left} conflicting hunk(s) left", why) if x)}


# ---------------------------------------------------------------------- entry
def update_branch(token: str, repo: str, head: str, base: str, push: bool = True) -> MergeResult:
    """Merge `base` into the PR branch `head`, resolving conflicts, and push the result to `head`."""
    work = Path(get_settings().apps_workdir) / f"merge-{uuid.uuid4().hex[:10]}"
    work.parent.mkdir(parents=True, exist_ok=True)
    try:
        url = remote_url(token, repo)
        _git(None, "clone", "--filter=blob:none", "--no-tags", "--branch", head, url, str(work), token=token)
        _git(work, "fetch", "origin", base, token=token)
        if subprocess.run(["git", "-C", str(work), "merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD"],
                          capture_output=True).returncode == 0:
            return MergeResult("up_to_date", head, base, _git(work, "rev-parse", "HEAD").strip())
        p = subprocess.run(["git", "-C", str(work), "-c", "user.name=DevNexa Merge Agent",
                            "-c", "user.email=merge-agent@devnexa.local", "-c", "merge.conflictStyle=diff3",
                            "merge", "--no-ff", "--no-edit", "-m", f"Merge {base} into {head}", "FETCH_HEAD"],
                           capture_output=True, text=True, errors="replace")
        status, files = "merged_clean", []
        if p.returncode != 0:
            conflicted = [x for x in _git(work, "diff", "--name-only", "--diff-filter=U").splitlines() if x.strip()]
            if not conflicted:
                raise MergeError(f"git merge failed: {(p.stdout + p.stderr).strip()[-400:]}")
            results = [resolve_file(work, c) for c in conflicted]
            bad = [r["path"] for r in results if not r["ok"]]
            if bad:
                _git(work, "merge", "--abort", check=False)
                raise MergeError(f"{len(bad)} file(s) need a developer: " + "; ".join(
                    f"{r['path']} ({r['detail']})" for r in results if not r["ok"]), bad,
                    [r for r in results if r["ok"]])
            for r in results:
                _git(work, "rm" if r.get("delete") else "add", "--", r["path"])
            files = [{k: r[k] for k in ("path", "strategy", "detail")} for r in results]
            _git(work, "commit", "--no-edit", "-m",
                 f"Merge {base} into {head}\n\nDevNexa Merge Agent resolved conflicts in:\n"
                 + "\n".join(f"- {r['path']}: {r['strategy']} ({r['detail']})" for r in files))
            status = "resolved"
        sha = _git(work, "rev-parse", "HEAD").strip()
        if push:
            _git(work, "push", "origin", f"HEAD:refs/heads/{head}", token=token)
        return MergeResult(status, head, base, sha, files)
    finally:
        shutil.rmtree(work, ignore_errors=True)
