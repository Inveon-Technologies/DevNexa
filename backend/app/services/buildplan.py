"""Work out how to build and run a cloned repo: use its Dockerfile, or generate one for common stacks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

GENERATED = ".devnexa.Dockerfile"


@dataclass
class BuildPlan:
    kind: str
    port: int
    dockerfile: str  # path relative to the build context
    generated: str | None = None  # Dockerfile content we wrote, if any
    notes: list[str] = field(default_factory=list)


class NotBuildable(Exception):
    pass


def _read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def exposed_port(dockerfile: str) -> int | None:
    m = re.search(r"^\s*EXPOSE\s+(\d+)", dockerfile, re.M | re.I)
    return int(m.group(1)) if m else None


def _node_major(pkg: dict) -> str:
    m = re.search(r"(\d{2})", str(pkg.get("engines", {}).get("node", "")))
    return m.group(1) if m and m.group(1) in ("18", "20", "22", "24") else "20"


def _node_install(root: Path, use_npm_install: bool) -> tuple[str, str]:
    """Returns (copy line for manifests, install command)."""
    if (root / "pnpm-lock.yaml").exists():
        return "COPY package.json pnpm-lock.yaml ./", "corepack enable && pnpm install --frozen-lockfile"
    if (root / "yarn.lock").exists():
        return "COPY package.json yarn.lock ./", "corepack enable && yarn install --frozen-lockfile"
    if (root / "package-lock.json").exists() and not use_npm_install:
        return "COPY package.json package-lock.json ./", "npm ci"
    return "COPY package*.json ./", "npm install"


def _script(install: str, script: str) -> list[str]:
    if "pnpm" in install:
        return ["pnpm", "run", script]
    if "yarn" in install:
        return ["yarn", script]
    return ["npm", "run", script]


def node_plan(root: Path, options: dict) -> BuildPlan:
    pkg = json.loads(_read(root / "package.json") or "{}")
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    scripts = pkg.get("scripts", {})
    node = _node_major(pkg)
    copy, install = _node_install(root, options.get("npm_install", False))
    build_line = f"RUN {' '.join(_script(install, 'build'))}" if "build" in scripts else ""
    start = scripts.get("start", "")
    spa = ("vite" in deps or "react-scripts" in deps) and (not start or re.search(r"\b(vite|react-scripts)\b", start))
    if spa and "build" in scripts:
        out = "build" if "react-scripts" in deps else "dist"
        return BuildPlan("node-static", 80, GENERATED, f"""FROM node:{node}-alpine AS build
WORKDIR /app
{copy}
RUN {install}
COPY . .
{build_line}

FROM nginx:1.27-alpine
COPY --from=build /app/{out} /usr/share/nginx/html
RUN printf 'server {{ listen 80; root /usr/share/nginx/html; location / {{ try_files $uri /index.html; }} }}' \\
    > /etc/nginx/conf.d/default.conf
EXPOSE 80
""", [f"Single-page app: built with `{install}` and served by nginx from /{out}."])
    port = 3000
    if not start and "main" not in pkg and not (root / "server.js").exists() and not (root / "index.js").exists():
        raise NotBuildable("package.json has no start script and no server.js/index.js entry point.")
    entry = pkg.get("main") or ("server.js" if (root / "server.js").exists() else "index.js")
    cmd = _script(install, "start") if start else ["node", entry]
    kind = "node-next" if "next" in deps else "node"
    return BuildPlan(kind, port, GENERATED, f"""FROM node:{node}-alpine
WORKDIR /app
{copy}
RUN {install}
COPY . .
{build_line}
ENV NODE_ENV=production PORT={port} HOST=0.0.0.0 HOSTNAME=0.0.0.0
EXPOSE {port}
CMD {json.dumps(cmd)}
""", [f"Node app: `{install}`, then `{' '.join(cmd)}` on port {port} (PORT env)."])


def _py_entry(root: Path, marker: str) -> tuple[str, str] | None:
    for cand in ("main.py", "app.py", "server.py", "api.py", "app/main.py", "src/main.py", "src/app.py", "wsgi.py"):
        text = _read(root / cand)
        m = re.search(rf"^(\w+)\s*=\s*{marker}\(", text, re.M)
        if m:
            return cand[:-3].replace("/", "."), m.group(1)
    return None


def python_plan(root: Path, options: dict) -> BuildPlan:
    reqs = _read(root / "requirements.txt").lower() + _read(root / "pyproject.toml").lower()
    install = ("pip install --no-cache-dir -r requirements.txt" if (root / "requirements.txt").exists()
               else "pip install --no-cache-dir .")
    port = 8000
    if (root / "manage.py").exists():
        kind, cmd, extra = "python-django", f'["python", "manage.py", "runserver", "0.0.0.0:{port}"]', ""
    elif "fastapi" in reqs and (e := _py_entry(root, "FastAPI")):
        kind, extra = "python-fastapi", " uvicorn"
        cmd = f'["uvicorn", "{e[0]}:{e[1]}", "--host", "0.0.0.0", "--port", "{port}"]'
    elif "flask" in reqs and (e := _py_entry(root, "Flask")):
        kind, extra = "python-flask", " gunicorn"
        cmd = f'["gunicorn", "-b", "0.0.0.0:{port}", "{e[0]}:{e[1]}"]'
    elif "streamlit" in reqs:
        port = 8501
        entry = next((c for c in ("app.py", "main.py", "streamlit_app.py") if (root / c).exists()), "app.py")
        kind, extra = "python-streamlit", ""
        cmd = f'["streamlit", "run", "{entry}", "--server.port", "{port}", "--server.address", "0.0.0.0"]'
    else:
        entry = next((c for c in ("main.py", "app.py", "server.py") if (root / c).exists()), None)
        if not entry:
            raise NotBuildable("Python project without a recognisable entry point (main.py, app.py, manage.py).")
        kind, extra, cmd = "python", "", f'["python", "{entry}"]'
    pip_extra = f"\nRUN pip install --no-cache-dir{extra}" if extra else ""
    return BuildPlan(kind, port, GENERATED, f"""FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PORT={port} HOST=0.0.0.0
COPY . .
RUN {install}{pip_extra}
EXPOSE {port}
CMD {cmd}
""", [f"Python app ({kind}): `{install}`, served on port {port}."])


def detect(root: Path, options: dict | None = None) -> BuildPlan:
    options = options or {}
    if (root / "Dockerfile").exists():
        df = _read(root / "Dockerfile")
        port = int(options.get("port") or exposed_port(df) or 80)
        return BuildPlan("dockerfile", port, "Dockerfile", None,
                         [f"Using the repo's Dockerfile; app port {port}"
                          + (" (from EXPOSE)." if exposed_port(df) else " (no EXPOSE, assumed).")])
    if (root / "package.json").exists():
        plan = node_plan(root, options)
    elif any((root / f).exists() for f in ("requirements.txt", "pyproject.toml", "manage.py")):
        plan = python_plan(root, options)
    elif (root / "go.mod").exists():
        plan = BuildPlan("go", 8080, GENERATED, """FROM golang:1.23-alpine AS build
WORKDIR /src
COPY . .
RUN go build -o /out/app .

FROM alpine:3.20
COPY --from=build /out/app /app
ENV PORT=8080
EXPOSE 8080
CMD ["/app"]
""", ["Go module built with `go build`, port 8080 (PORT env)."])
    elif (root / "index.html").exists():
        plan = BuildPlan("static", 80, GENERATED, "FROM nginx:1.27-alpine\nCOPY . /usr/share/nginx/html\nEXPOSE 80\n",
                         ["Static site served by nginx."])
    else:
        raise NotBuildable("Could not detect how to build this repo: no Dockerfile, package.json, "
                           "requirements.txt/pyproject.toml, go.mod or index.html at its root.")
    if options.get("port"):
        plan.port = int(options["port"])
    return plan
