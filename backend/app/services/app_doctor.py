"""Diagnosis for deployed user repos: known failure patterns + optional LLM, a small memory of past
fixes (retrieved by similarity, like the incident RAG), and the graded action catalogue."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import KeyValue
from .analyzer import LLMAnalyzer, get_analyzer
from .embeddings import cosine, get_embedder


@dataclass(frozen=True)
class AppAction:
    title: str
    risk: str
    allowlisted: bool
    description: str


# Graded autonomy, same idea as the incident policy: low+allowlisted runs automatically,
# medium/high wait for a person.
APP_ACTIONS = {
    "retry_deploy": AppAction("Retry the deploy", "low", True, "Run the same deploy again (transient failures)."),
    "use_npm_install": AppAction("Install without the frozen lockfile", "low", True,
                                 "Lockfile is out of sync; rebuild with `npm install` / a non-frozen pnpm or yarn install."),
    "reroute_port": AppAction("Route to the detected port", "low", True,
                              "The app listens on a different port than expected; send traffic there."),
    "restart_app": AppAction("Restart the container", "low", True, "Restart a crashed or hung container."),
    "prune_docker": AppAction("Free Docker disk space", "low", True, "Remove dangling images and build cache."),
    "raise_memory": AppAction("Raise the memory limit", "medium", False,
                              "Redeploy with double the memory limit after an out-of-memory kill."),
    "rollback_app": AppAction("Roll back to the last good deploy", "medium", False,
                              "Point the app's URL back to the previous healthy deployment."),
    # Never touches the base branch or the running app: the change goes to a new branch, is checked on a
    # preview URL, and only then becomes a pull request that a person merges.
    "open_fix_pr": AppAction("Fix on a new branch and open a pull request", "medium", False,
                             "Commit the proposed change to a new branch, deploy it to a preview URL, and open a PR "
                             "once it is healthy."),
    "add_dockerfile": AppAction("Add the generated Dockerfile to the repo", "low", True,
                                "The repo had no Dockerfile; open a PR with the one DevNexa generated and deployed."),
    "change_runtime": AppAction("Rebuild with the runtime version the project needs", "low", True,
                                "The build asked for a newer (or older) Node, Python, Java or Go than the image had; "
                                "rebuild on the version from the error."),
    "escalate": AppAction("Needs a developer", "low", True, "No safe automatic fix; the evidence is attached."),
}

PIP_NAMES = {"cv2": "opencv-python-headless", "PIL": "pillow", "sklearn": "scikit-learn", "yaml": "pyyaml",
             "bs4": "beautifulsoup4", "dotenv": "python-dotenv", "jwt": "PyJWT", "dateutil": "python-dateutil",
             "psycopg2": "psycopg2-binary", "MySQLdb": "mysqlclient", "jose": "python-jose", "magic": "python-magic",
             "multipart": "python-multipart", "Crypto": "pycryptodome", "docx": "python-docx"}

# (category, pattern, title, explanation, suggestion, action, confidence)
RULES: list[tuple[str, str, str, str, str, str, float]] = [
    ("docker_unavailable", r"Cannot connect to the Docker daemon|docker: (command )?not found|Docker is not available",
     "Docker is not reachable", "The DevNexa backend cannot talk to the Docker engine on the host.",
     "Run the backend with /var/run/docker.sock mounted (docker compose does this).", "escalate", 0.95),
    ("repo_access", r"Authentication failed|Repository not found|could not read Username|returned error: 403",
     "No access to the repository", "GitHub refused the clone with the saved token.",
     "Reconnect GitHub with a token that has read access to this repository.", "escalate", 0.95),
    ("branch_missing", r"Remote branch .* not found|couldn't find remote ref",
     "Branch not found", "The selected branch does not exist on GitHub.", "Pick an existing branch.", "escalate", 0.95),
    ("not_buildable", r"Could not detect how to build|no start script|without a recognisable entry point",
     "Unknown project type", "DevNexa could not tell how to build or start this repo.",
     "Add a Dockerfile to the repo (or a start script / main.py) and deploy again.", "escalate", 0.9),
    ("disk_full", r"no space left on device", "Host disk is full", "The Docker host ran out of disk space.",
     "Prune unused images and build cache, then retry.", "prune_docker", 0.9),
    ("network", r"Could not resolve host|Temporary failure in name resolution|ETIMEDOUT|ECONNRESET|"
                r"Read timed out|TLS handshake timeout|i/o timeout|Could not transfer artifact|Connection reset by peer",
     "Network hiccup during the build", "A download failed because of a network error.",
     "Retrying usually fixes this.", "retry_deploy", 0.75),
    ("pnpm_ignored_builds", r"ERR_PNPM_IGNORED_BUILDS|Ignored build scripts",
     "pnpm blocked package build scripts",
     "Newer pnpm versions refuse to run dependencies' install scripts (e.g. sharp) until they are approved.",
     "Add the packages to pnpm.onlyBuiltDependencies in package.json. DevNexa's generated build now allows them; "
     "a repo Dockerfile needs `--config.dangerously-allow-all-builds=true` on `pnpm install`.",
     "retry_deploy", 0.85),
    ("npm_lockfile", r"npm ci` can only install packages when|npm ERR! code EUSAGE|npm error code EUSAGE|"
                     r"ERR_PNPM_OUTDATED_LOCKFILE|ERR_PNPM_LOCKFILE_CONFIG_MISMATCH|"
                     r"lockfile.*(out of date|not in sync)|Missing: .* from lock file",
     "Lockfile is out of sync", "The frozen install refuses to run because package.json and the lockfile differ.",
     "Run `npm install` locally and commit package-lock.json. DevNexa can build with `npm install` meanwhile.",
     "use_npm_install", 0.9),
    ("npm_missing_script", r'Missing script: "?(\w+)', "npm script missing",
     "The build or start command calls an npm script that package.json does not define.",
     "Add the script to package.json, or add a Dockerfile with the right start command.", "open_fix_pr", 0.8),
    ("node_module_missing", r"Cannot find module '([^'./][^']*)'|Module not found: Error: Can't resolve '([^'./][^']*)'",
     "Node dependency missing", "The code imports a package that is not in package.json.",
     "Add the package to dependencies in package.json.", "open_fix_pr", 0.8),
    ("python_module_missing", r"ModuleNotFoundError: No module named '([\w.]+)'",
     "Python dependency missing", "The code imports a module that is not installed.",
     "Add the package to requirements.txt.", "open_fix_pr", 0.85),
    ("pip_resolution", r"ResolutionImpossible|Could not find a version that satisfies|No matching distribution",
     "Python dependencies cannot be installed", "pip could not resolve the pinned versions.",
     "Relax or fix the version pins in requirements.txt.", "open_fix_pr", 0.8),
    ("java_runtime", r"release version (\d+) not supported|invalid target release:? (\d+)|"
                     r"class file version (\d+)\.0|Unsupported class file major version (\d+)",
     "Java version mismatch", "The code targets a different Java version than the JDK in the image.",
     "Rebuild on the JDK the code targets (DevNexa does this automatically), or set java.version in the build file.",
     "change_runtime", 0.85),
    ("node_runtime", r"engine \"node\" is incompatible.*?Expected version \"[^\d\"]*(\d+)|"
                     r"Unsupported engine.*?node:\s*'[^\d']*(\d+)|requires Node(?:\.js)?(?: version)? [>=^~ v]*(\d{2})",
     "Node.js version mismatch", "A package or the project needs a newer Node.js than the image had.",
     "Rebuild on that Node.js version, and set engines.node in package.json.", "change_runtime", 0.85),
    ("python_runtime", r"requires a different Python: [\d.]+ not in '[>=~^ ]*(3\.\d+)|"
                       r"Requires-Python [>=~ ]*(3\.\d+)",
     "Python version mismatch", "A dependency or the project needs a different Python version.",
     "Rebuild on that Python version, and pin it in .python-version.", "change_runtime", 0.85),
    ("go_runtime", r"go\.mod requires go >= ?(1\.\d+)|requires go(?: version)? >= ?(1\.\d+)",
     "Go version mismatch", "go.mod (or a module) needs a newer Go toolchain than the image had.",
     "Rebuild on that Go version.", "change_runtime", 0.85),
    ("jar_no_main", r"no main manifest attribute",
     "JAR has no main class", "The built JAR cannot be started with `java -jar`: its manifest has no Main-Class.",
     "Use the Spring Boot plugin, or configure maven-jar/shade (Gradle: the application plugin) with the main class.",
     "open_fix_pr", 0.8),
    ("deps_resolution", r"Could not resolve dependencies for project|Your requirements could not be resolved|"
                        r"Could not find gem|Bundler could not find compatible versions|failed to select a version for|"
                        r"Unable to find package|NU1101|missing go\.sum entry",
     "Dependencies cannot be installed", "The build tool could not resolve the declared dependency versions.",
     "Fix the dependency name or version in the build file (or commit the missing lock/sum entries).",
     "open_fix_pr", 0.8),
    ("syntax_error", r"SyntaxError|IndentationError|error TS\d+|Failed to compile|COMPILATION ERROR|"
                     r"error: cannot find symbol|Compilation failed|error\[E\d{4}\]|: undefined: \w+|"
                     r"PHP Parse error|error CS\d{4}|syntax error, unexpected",
     "Code does not compile", "The build stops on a syntax or type error.",
     "Fix the error at the line shown in the log.", "open_fix_pr", 0.8),
    ("oom", r"OOMKilled|JavaScript heap out of memory|MemoryError|Killed\s*$",
     "Out of memory", "The process was killed for using more memory than the container limit.",
     "Raise the memory limit or reduce memory use.", "raise_memory", 0.8),
    ("bind_localhost", r"(listening|running|serving|started).{0,40}(127\.0\.0\.1|localhost):\d+",
     "App only listens on localhost", "The server binds to 127.0.0.1, so nothing outside the container can reach it.",
     "Bind to 0.0.0.0, or read the HOST env var (DevNexa sets HOST=0.0.0.0).", "open_fix_pr", 0.75),
    ("port_mismatch", r"__port_mismatch__", "App listens on another port",
     "Health checks found nothing on the expected port, but the logs show the app on another one.",
     "Expose the real port in the Dockerfile, or read the PORT env var.", "reroute_port", 0.8),
    ("crash", r"Traceback \(most recent call last\)|Unhandled|uncaughtException|panic:|Error: |Exception",
     "App crashed on start or at runtime", "The process exited with an error.",
     "Read the stack trace in the logs; restart helps only if it was transient.", "restart_app", 0.6),
]

PORT_IN_LOGS = re.compile(r"(?:listening|running|serving|started|available)\b.{0,60}?(?:port\s*|:)(\d{2,5})\b", re.I)


def detected_port(log: str) -> int | None:
    for m in PORT_IN_LOGS.finditer(log):
        p = int(m.group(1))
        if 80 <= p <= 65535:
            return p
    return None


# Java class-file major versions -> Java release
CLASS_FILE_JAVA = {52: "8", 55: "11", 61: "17", 65: "21", 69: "25"}


def runtime_version(cat: str, m: re.Match) -> str | None:
    """The runtime version a *_runtime failure asks for (LTS for Java, major for Node, 3.x for Python)."""
    groups = [g for g in m.groups() if g]
    if not groups:
        return None
    v = groups[0]
    if cat == "java_runtime":
        if m.group(3) or m.group(4):  # class file version: 65 -> Java 21
            major = int(m.group(3) or m.group(4))
            v = next((j for c, j in sorted(CLASS_FILE_JAVA.items()) if major <= c), "25")
        else:
            v = next((str(lts) for lts in (8, 11, 17, 21, 25) if int(v) <= lts), "25")
    if cat == "node_runtime":
        v = next((str(n) for n in (18, 20, 22, 24) if int(v) <= n), "24")
    return v


def _evidence(log: str, pattern: str, n: int = 8) -> list[str]:
    lines = log.splitlines()
    rx = re.compile(pattern, re.I)
    hits = [i for i, ln in enumerate(lines) if rx.search(ln)]
    if not hits:
        return lines[-n:]
    i = hits[0]
    return lines[max(0, i - 2): i + n]


def heuristic(stage: str, log: str) -> dict:
    for cat, pat, title, why, fix, action, conf in RULES:
        m = re.search(pat, log, re.I | re.M)
        if m:
            subject = next((g for g in m.groups() if g), None) if m.groups() else None
            if cat == "crash" and stage == "build":
                title, action = "Build step failed", "escalate"
            extra = {}
            if cat.endswith("_runtime") and subject:
                version = runtime_version(cat, m)
                if version:
                    extra = {"runtime_version": version}
                    title = f"{title}: needs {cat.split('_')[0].title()} {version}"
                else:
                    action = "escalate"
            return {**extra, "category": cat, "title": title + (f": {subject}" if subject and cat.endswith("missing") else ""),
                    "root_cause": why, "suggestion": fix, "action": action, "confidence": conf,
                    "subject": subject, "evidence": _evidence(log, pat), "stage": stage, "provider": "heuristic"}
    return {"category": "unknown", "title": f"{stage.title()} failed", "subject": None,
            "root_cause": "No known failure pattern matched the logs.",
            "suggestion": "Read the last log lines; a developer needs to look.", "action": "escalate",
            "confidence": 0.3, "evidence": log.splitlines()[-12:], "stage": stage, "provider": "heuristic"}


def dockerfile_patch(diag: dict, repo_dir: Path | None, build_kind: str | None) -> dict[str, str] | None:
    """The repo's own Dockerfile does not build, and no more specific fix is known: if DevNexa can build this
    stack itself, propose its generated Dockerfile instead (it is checked on a preview before any PR)."""
    if not repo_dir or build_kind != "dockerfile" or diag.get("stage") != "build" \
            or diag["category"] not in ("unknown", "crash", "npm_missing_script", "not_buildable"):
        return None
    from . import buildplan

    try:
        plan = buildplan.detect(repo_dir, {"ignore_dockerfile": True})
    except buildplan.NotBuildable:
        return None
    diag.update(category="dockerfile_broken", title="The repo's Dockerfile does not build",
                root_cause="The build fails inside the repo's own Dockerfile.",
                suggestion=f"Replace it with a Dockerfile DevNexa generated for this {plan.kind} app "
                           f"(port {plan.port}); it is tested on a preview URL first.", confidence=0.7)
    return {"Dockerfile": plan.generated}


def heuristic_patch(diag: dict, repo_dir: Path | None) -> dict[str, str] | None:
    """Mechanical fixes we can write without an LLM."""
    if not repo_dir or not diag.get("subject"):
        return None
    mod = diag["subject"]
    if diag["category"] == "python_module_missing":
        top = mod.split(".")[0]
        req = repo_dir / "requirements.txt"
        if not req.exists() or (repo_dir / top).exists() or (repo_dir / f"{top}.py").exists():
            return None
        text = req.read_text()
        name = PIP_NAMES.get(top, top.replace("_", "-"))
        if re.search(rf"^{re.escape(name)}\b", text, re.M | re.I):
            return None
        return {"requirements.txt": text.rstrip("\n") + f"\n{name}\n"}
    if diag["category"] == "node_module_missing":
        pkg_path = repo_dir / "package.json"
        if not pkg_path.exists():
            return None
        name = mod if mod.startswith("@") else mod.split("/")[0]
        if mod.startswith("@"):
            name = "/".join(mod.split("/")[:2])
        pkg = json.loads(pkg_path.read_text())
        if name in pkg.get("dependencies", {}) or name.startswith("node:"):
            return None
        pkg.setdefault("dependencies", {})[name] = "*"
        return {"package.json": json.dumps(pkg, indent=2) + "\n"}
    return None


LLM_SYSTEM = """You are the Deploy Agent of DevNexa, an agentic DevOps platform.
A user's GitHub repo failed to build, start, or stay healthy. Diagnose ONLY from the log and files given.
Reply with one JSON object and nothing else, with keys: category, title, root_cause (1-2 sentences),
suggestion (what the developer should change), confidence (0..1), action (one of the allowed actions),
patch (list of {path, content} with the FULL new content of each file to change; [] if unsure; at most 3 files,
only when the fix is clear from the evidence)."""


def _key_files(repo_dir: Path, log: str) -> dict[str, str]:
    names = ["Dockerfile", "package.json", "requirements.txt", "pyproject.toml", "main.py", "app.py",
             "server.js", "index.js", "pom.xml", "build.gradle", "build.gradle.kts", "go.mod", "main.go", "Cargo.toml",
             "Gemfile", "composer.json", "src/main/resources/application.properties"]
    names += re.findall(r'File "/app/([^"]+)"', log)[-3:]
    names += re.findall(r"/app/([\w./-]+\.(?:js|ts|py|go)):\d+", log)[-3:]
    names += re.findall(r"/src/([\w./-]+\.(?:java|kt|go|rs|cs|php|rb))[:\[(]+\d+", log)[-3:]
    out = {}
    for n in dict.fromkeys(names):
        p = repo_dir / n
        if p.is_file() and p.stat().st_size < 30000:
            out[n] = p.read_text(errors="replace")
    return out


def llm_diagnose(analyzer: LLMAnalyzer, stage: str, log: str, repo_dir: Path | None, base: dict) -> dict | None:
    files = _key_files(repo_dir, log) if repo_dir and repo_dir.exists() else {}
    listing = sorted(str(p.relative_to(repo_dir)) for p in repo_dir.rglob("*")
                     if p.is_file() and ".git" not in p.parts)[:200] if files else []
    try:
        text = analyzer._complete(LLM_SYSTEM, json.dumps({
            "stage": stage, "allowed_actions": {k: a.title for k, a in APP_ACTIONS.items()},
            "pattern_match": {k: base[k] for k in ("category", "title", "root_cause")},
            "log_tail": log[-8000:], "files": files, "repo_files": listing,
        })[:60000])
        d = analyzer._parse(text)
    except Exception as e:  # network/parse problems keep the heuristic result
        return {**base, "provider": f"heuristic (LLM fallback: {type(e).__name__})"}
    patch = {}
    for item in (d.get("patch") or [])[:3]:
        path = str(item.get("path", "")).lstrip("/")
        if path and ".." not in path and isinstance(item.get("content"), str) and len(item["content"]) < 100000:
            patch[path] = item["content"]
    action = d.get("action") if d.get("action") in APP_ACTIONS else base["action"]
    return {**base, "category": str(d.get("category") or base["category"]), "title": str(d.get("title") or base["title"]),
            "root_cause": str(d.get("root_cause") or base["root_cause"]),
            "suggestion": str(d.get("suggestion") or base["suggestion"]),
            "confidence": max(0.0, min(1.0, float(d.get("confidence", base["confidence"])))),
            "action": action, "llm_patch": patch or None, "provider": analyzer.name}


def diagnose(db: Session, stage: str, log: str, repo_dir: Path | None = None, build_kind: str | None = None) -> dict:
    diag = heuristic(stage, log)
    if diag["action"] == "change_runtime" and build_kind == "dockerfile":
        # DevNexa does not own this Dockerfile's base image: say which version to put in FROM.
        diag.update(action="escalate", suggestion=f"Change the FROM image in the repo's Dockerfile to "
                                                  f"version {diag.get('runtime_version')}.")
    analyzer = get_analyzer()
    if isinstance(analyzer, LLMAnalyzer):
        diag = llm_diagnose(analyzer, stage, log, repo_dir, diag) or diag
    patch = diag.pop("llm_patch", None) or heuristic_patch(diag, repo_dir) \
        or dockerfile_patch(diag, repo_dir, build_kind)
    if patch:
        diag["patch"] = patch
        if diag["action"] == "escalate":
            diag["action"] = "open_fix_pr"
    elif diag["action"] == "open_fix_pr":
        diag["action"] = "escalate"
    diag["similar"] = recall(db, symptoms(diag))
    for s in diag["similar"]:
        if s["outcome"] == "fixed" and s["category"] == diag["category"]:
            diag["confidence"] = round(min(0.99, diag["confidence"] + 0.1), 2)
            break
    a = APP_ACTIONS[diag["action"]]
    diag.update(action_title=a.title, risk=a.risk, at=datetime.now(timezone.utc).isoformat())
    return diag


# ------------------------------------------------------------------ memory
MEMORY_KEY = "app_fix_memory"


def symptoms(diag: dict) -> str:
    return " ".join([diag.get("stage", ""), diag.get("category", ""), diag.get("title", ""),
                     " ".join(diag.get("evidence", [])[:6])])


def recall(db: Session, text: str, k: int = 3, min_sim: float = 0.3) -> list[dict]:
    row = db.get(KeyValue, MEMORY_KEY)
    if not row or not row.value.get("items"):
        return []
    q = get_embedder().embed(text)
    scored = [(cosine(q, it["embedding"]), it) for it in row.value["items"]]
    return [{k2: v for k2, v in it.items() if k2 != "embedding"} | {"similarity": round(s, 3)}
            for s, it in sorted(scored, key=lambda x: -x[0])[:k] if s >= min_sim]


def remember(db: Session, diag: dict, repo: str, action: str, outcome: str) -> None:
    """Store a verified outcome so the next similar failure is recognised (the learning step)."""
    row = db.get(KeyValue, MEMORY_KEY)
    items = list(row.value.get("items", [])) if row else []
    items.append({"repo": repo, "category": diag.get("category"), "title": diag.get("title"), "action": action,
                  "outcome": outcome, "at": datetime.now(timezone.utc).isoformat(),
                  "embedding": get_embedder().embed(symptoms(diag))})
    value = {"items": items[-200:]}
    if row:
        row.value = value
    else:
        db.add(KeyValue(key=MEMORY_KEY, value=value))
    db.flush()
