"""Work out how to build and run a cloned repo: use its Dockerfile, or generate one for the detected stack.

Supported without a Dockerfile: Node (npm/pnpm/yarn, SPAs, Next), Bun, Deno, Python (pip, Poetry, Pipenv, uv;
Django, FastAPI, Flask, Streamlit), Java and Kotlin (Maven, Gradle; Spring Boot, WAR on Tomcat), Go, Rust,
Ruby (Rails, Rack), PHP (Composer, Laravel), .NET (ASP.NET Core) and static sites. Versions come from the
project's own files (engines, .python-version, java.version, go.mod, TargetFramework, ...). A repo whose app
sits in a subfolder (frontend/, backend/, server/ ...) is found automatically when only one folder fits.
"""

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
    language: str = ""
    framework: str = ""
    tool: str = ""  # package manager / build tool
    version: str = ""  # runtime version the image uses
    subdir: str = ""  # set when the app was found in a subfolder of the requested folder

    @property
    def stack(self) -> str:
        """Human label, e.g. "Java 21 · Spring Boot · Maven"."""
        lang = f"{self.language} {self.version}".strip()
        return " · ".join(x for x in (lang, self.framework, self.tool) if x) or self.kind

    def to_dict(self) -> dict:
        return {"kind": self.kind, "stack": self.stack, "language": self.language, "framework": self.framework,
                "tool": self.tool, "version": self.version, "port": self.port, "subdir": self.subdir,
                "uses_repo_dockerfile": self.generated is None, "notes": self.notes, "dockerfile": self.generated}


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


# pnpm 10+ refuses to run dependencies' install scripts (sharp, esbuild, prisma, ...) unless they are
# approved, and newer versions fail the install. A server build has no one to approve them, so allow them.
PNPM_ALLOW_BUILDS = "--config.dangerously-allow-all-builds=true"


def _node_install(root: Path, use_npm_install: bool) -> tuple[str, str]:
    """Returns (copy line for manifests, install command). `use_npm_install` also relaxes pnpm/yarn lockfiles."""
    if (root / "pnpm-lock.yaml").exists():
        frozen = "--no-frozen-lockfile" if use_npm_install else "--frozen-lockfile"
        return ("COPY package.json pnpm-lock.yaml ./",
                f"corepack enable && pnpm install {frozen} {PNPM_ALLOW_BUILDS}")
    if (root / "yarn.lock").exists():
        frozen = "" if use_npm_install else " --frozen-lockfile"
        return "COPY package.json yarn.lock ./", f"corepack enable && yarn install{frozen}"
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
    node = str(options.get("runtime_version") or _node_major(pkg))
    copy, install = _node_install(root, options.get("npm_install", False))
    build_line = f"RUN {' '.join(_script(install, 'build'))}" if "build" in scripts else ""
    start = scripts.get("start", "")
    spa = ("vite" in deps or "react-scripts" in deps) and (not start or re.search(r"\b(vite|react-scripts)\b", start))
    if spa and "build" in scripts:
        out = "build" if "react-scripts" in deps else "dist"
        fw = "Create React App" if "react-scripts" in deps else "Vite"
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
""", [f"Single-page app: built with `{install}` and served by nginx from /{out}."],
                         "Node", fw, install.split()[-1] if "corepack" in install else "npm", node)
    port = _node_port(root, pkg) or 3000
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
""", [f"Node app: `{install}`, then `{' '.join(cmd)}` on port {port} (PORT env)."],
                     "Node", _node_framework(deps), _pm(install), node)


def _pm(install: str) -> str:
    return "pnpm" if "pnpm" in install else "yarn" if "yarn" in install else "npm"


def _node_framework(deps: dict) -> str:
    for name, label in (("next", "Next.js"), ("@nestjs/core", "NestJS"), ("nuxt", "Nuxt"), ("@remix-run/node", "Remix"),
                        ("@sveltejs/kit", "SvelteKit"), ("express", "Express"), ("fastify", "Fastify"), ("koa", "Koa"),
                        ("hono", "Hono")):
        if name in deps:
            return label
    return ""


def _node_port(root: Path, pkg: dict) -> int | None:
    """A server that hardcodes its port (app.listen(5000)) instead of reading PORT."""
    entry = pkg.get("main") or ("server.js" if (root / "server.js").exists() else "index.js")
    text = _read(root / entry)
    if "process.env.PORT" in text:
        return None
    m = re.search(r"\.listen\(\s*(\d{2,5})\b", text)
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------ Bun / Deno
def bun_plan(root: Path, options: dict) -> BuildPlan:
    pkg = json.loads(_read(root / "package.json") or "{}")
    scripts = pkg.get("scripts", {})
    entry = pkg.get("module") or pkg.get("main") or next(
        (c for c in ("index.ts", "src/index.ts", "server.ts", "index.js") if (root / c).exists()), "index.ts")
    cmd = ["bun", "run", "start"] if "start" in scripts else ["bun", "run", entry]
    build = "\nRUN bun run build" if "build" in scripts else ""
    return BuildPlan("bun", 3000, GENERATED, f"""FROM oven/bun:1
WORKDIR /app
COPY . .
RUN bun install{'' if options.get('npm_install') else ' --frozen-lockfile'}{build}
ENV NODE_ENV=production PORT=3000 HOST=0.0.0.0
EXPOSE 3000
CMD {json.dumps(cmd)}
""", [f"Bun app: `bun install`, then `{' '.join(cmd)}` on port 3000."], "TypeScript" if entry.endswith(".ts") else "JavaScript",
                     _node_framework({**pkg.get("dependencies", {})}), "Bun", "")


def deno_plan(root: Path, options: dict) -> BuildPlan:
    cfg_file = root / "deno.json" if (root / "deno.json").exists() else root / "deno.jsonc"
    text = re.sub(r"^\s*//.*$", "", _read(cfg_file), flags=re.M)
    try:
        tasks = json.loads(text or "{}").get("tasks", {})
    except ValueError:
        tasks = {}
    entry = next((c for c in ("main.ts", "server.ts", "mod.ts", "src/main.ts", "main.js") if (root / c).exists()), "main.ts")
    cmd = ["deno", "task", "start"] if "start" in tasks else ["deno", "run", "-A", entry]
    return BuildPlan("deno", 8000, GENERATED, f"""FROM denoland/deno:2.1.4
WORKDIR /app
COPY . .
RUN deno install || deno cache {entry} || true
ENV PORT=8000 HOST=0.0.0.0
EXPOSE 8000
CMD {json.dumps(cmd)}
""", [f"Deno app: `{' '.join(cmd)}` on port 8000."], "TypeScript", "", "Deno", "2")


# ---------------------------------------------------------------------- Python
def _py_entry(root: Path, marker: str) -> tuple[str, str] | None:
    for cand in ("main.py", "app.py", "server.py", "api.py", "app/main.py", "app/__init__.py", "src/main.py",
                 "src/app.py", "wsgi.py", "run.py"):
        text = _read(root / cand)
        m = re.search(rf"^(\w+)\s*=\s*{marker}\(", text, re.M)
        if m:
            mod = cand[:-3].replace("/", ".").removesuffix(".__init__")
            return mod, m.group(1)
    return None


def python_version(root: Path) -> str:
    """3.x from .python-version, runtime.txt (python-3.11.4) or requires-python; 3.12 otherwise."""
    for f in (".python-version", "runtime.txt"):
        m = re.search(r"(3\.\d{1,2})", _read(root / f))
        if m:
            return m.group(1)
    m = re.search(r"requires-python\s*=\s*[\"']([^\"']+)", _read(root / "pyproject.toml"))
    if m:
        spec = m.group(1)
        upper = re.search(r"<\s*(3\.\d{1,2})", spec)
        exact = re.search(r"(?:==|~=)\s*(3\.\d{1,2})", spec)
        if exact:
            return exact.group(1)
        if upper:  # <3.12 -> 3.11
            major, minor = upper.group(1).split(".")
            return f"{major}.{int(minor) - 1}"
        lower = re.search(r">=?\s*(3\.\d{1,2})", spec)
        if lower and tuple(map(int, lower.group(1).split("."))) > (3, 12):
            return lower.group(1)
    return "3.12"


def _py_install(root: Path) -> tuple[str, str]:
    """(install command, tool label)."""
    pyproject = _read(root / "pyproject.toml")
    if (root / "uv.lock").exists():
        return ("pip install --no-cache-dir uv && uv export --frozen --no-dev --no-hashes -o /tmp/req.txt "
                "&& pip install --no-cache-dir -r /tmp/req.txt", "uv")
    if (root / "poetry.lock").exists() or "[tool.poetry]" in pyproject:
        return ("pip install --no-cache-dir poetry && poetry config virtualenvs.create false "
                "&& poetry install --no-interaction --no-root --only main", "Poetry")
    if (root / "Pipfile").exists():
        return "pip install --no-cache-dir pipenv && pipenv install --system --deploy --ignore-pipfile", "Pipenv"
    if (root / "requirements.txt").exists():
        return "pip install --no-cache-dir -r requirements.txt", "pip"
    return "pip install --no-cache-dir .", "pip"


def _procfile_web(root: Path) -> str | None:
    m = re.search(r"^web:\s*(.+)$", _read(root / "Procfile"), re.M)
    return m.group(1).strip() if m else None


def python_plan(root: Path, options: dict) -> BuildPlan:
    reqs = " ".join(_read(root / f).lower() for f in ("requirements.txt", "pyproject.toml", "Pipfile"))
    install, tool = _py_install(root)
    version = str(options.get("runtime_version") or python_version(root))
    port = 8000
    procfile = _procfile_web(root)
    collect = ""
    if (root / "manage.py").exists():
        kind, fw, extra = "python-django", "Django", " gunicorn"
        m = re.search(r"DJANGO_SETTINGS_MODULE['\"]?\s*,\s*['\"]([\w.]+)\.settings", _read(root / "manage.py"))
        if m and (root / m.group(1).replace(".", "/") / "wsgi.py").exists():
            cmd = json.dumps(["gunicorn", "-b", f"0.0.0.0:{port}", f"{m.group(1)}.wsgi:application"])
        else:
            cmd = json.dumps(["python", "manage.py", "runserver", f"0.0.0.0:{port}"])
        collect = "\nRUN python manage.py collectstatic --noinput || true"
    elif "fastapi" in reqs and (e := _py_entry(root, "FastAPI")):
        kind, fw, extra = "python-fastapi", "FastAPI", " uvicorn"
        cmd = json.dumps(["uvicorn", f"{e[0]}:{e[1]}", "--host", "0.0.0.0", "--port", str(port)])
    elif "flask" in reqs and (e := _py_entry(root, "Flask")):
        kind, fw, extra = "python-flask", "Flask", " gunicorn"
        cmd = json.dumps(["gunicorn", "-b", f"0.0.0.0:{port}", f"{e[0]}:{e[1]}"])
    elif "streamlit" in reqs:
        port = 8501
        entry = next((c for c in ("app.py", "main.py", "streamlit_app.py", "Home.py") if (root / c).exists()), "app.py")
        kind, fw, extra = "python-streamlit", "Streamlit", ""
        cmd = json.dumps(["streamlit", "run", entry, "--server.port", str(port), "--server.address", "0.0.0.0"])
    elif "gradio" in reqs and (entry := next((c for c in ("app.py", "main.py") if (root / c).exists()), None)):
        port = 7860
        kind, fw, extra = "python-gradio", "Gradio", ""
        cmd = json.dumps(["python", entry])
    elif procfile:
        kind, fw, extra = "python", "", ""
        cmd = json.dumps(["sh", "-c", procfile.replace("$PORT", str(port))])
    else:
        entry = next((c for c in ("main.py", "app.py", "server.py", "run.py") if (root / c).exists()), None)
        if not entry:
            raise NotBuildable("Python project without a recognisable entry point (main.py, app.py, manage.py).")
        kind, fw, extra, cmd = "python", "", "", json.dumps(["python", entry])
    pip_extra = f"\nRUN pip install --no-cache-dir{extra}" if extra and extra.strip() not in reqs else ""
    gradio = "GRADIO_SERVER_NAME=0.0.0.0 " if kind == "python-gradio" else ""
    return BuildPlan(kind, port, GENERATED, f"""FROM python:{version}-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT={port} HOST=0.0.0.0 {gradio}PIP_DISABLE_PIP_VERSION_CHECK=1
COPY . .
RUN {install}{pip_extra}{collect}
EXPOSE {port}
CMD {cmd}
""", [f"Python {version} app ({fw or 'script'}): `{install}`, served on port {port}."], "Python", fw, tool, version)


# ------------------------------------------------------------------ Java / JVM
def _java_version(text: str) -> str:
    for pat in (r"<java\.version>\s*(\d+)", r"<maven\.compiler\.(?:release|source|target)>\s*(?:1\.)?(\d+)",
                r"<release>\s*(\d+)\s*</release>", r"JavaLanguageVersion\.of\(\s*(\d+)", r"VERSION_(?:1_)?(\d+)",
                r"(?:source|target)Compatibility\s*=\s*['\"]?(?:1\.)?(\d+)", r"jvmTarget\s*=\s*['\"](?:1\.)?(\d+)"):
        m = re.search(pat, text)
        if m:
            v = int(m.group(1))
            for lts in (8, 11, 17, 21, 25):
                if v <= lts:
                    return str(lts)
    return "21"


def _spring_port(root: Path) -> int | None:
    for f in ("src/main/resources/application.properties", "src/main/resources/application.yml",
              "src/main/resources/application.yaml"):
        m = re.search(r"server\.port\s*[=:]\s*(\d{2,5})|^\s*port:\s*(\d{2,5})", _read(root / f), re.M)
        if m:
            return int(m.group(1) or m.group(2))
    return None


PICK_JAR = ("ls {dir}/*.jar | grep -v -e '-plain.jar' -e '-sources.jar' -e '-javadoc.jar' -e '/original-' "
            "| head -n 1 | xargs -I{{}} cp {{}} /out/app.jar")


def java_plan(root: Path, options: dict) -> BuildPlan:
    maven = (root / "pom.xml").exists()
    build_file = "pom.xml" if maven else ("build.gradle.kts" if (root / "build.gradle.kts").exists() else "build.gradle")
    text = _read(root / build_file)
    v = str(options.get("runtime_version") or _java_version(text))
    spring = "spring-boot" in text or "org.springframework.boot" in text
    kotlin = "kotlin" in text.lower() or (root / "src/main/kotlin").is_dir()
    lang = "Kotlin" if kotlin else "Java"
    war = maven and re.search(r"<packaging>\s*war\s*</packaging>", text) and not spring
    port = (_spring_port(root) if spring else None) or 8080
    if maven:
        tool = "Maven"
        if (root / "mvnw").exists():
            build = "chmod +x mvnw && ./mvnw -B -q -DskipTests package"
        else:
            build = "mvn -B -q -DskipTests package"
        image, out_dir = f"maven:3.9-eclipse-temurin-{v}", "target"
    else:
        tool = "Gradle"
        task = "bootJar" if spring else "build"
        runner = "chmod +x gradlew && ./gradlew" if (root / "gradlew").exists() else "gradle"
        build = f"{runner} --no-daemon -q {task} -x test"
        image, out_dir = f"gradle:8-jdk{v}", "build/libs"
    fw = "Spring Boot" if spring else ("Servlet (WAR)" if war else "")
    if war:
        dockerfile = f"""FROM {image} AS build
WORKDIR /src
COPY . .
RUN {build} && mkdir -p /out && cp $(ls target/*.war | head -n 1) /out/ROOT.war

FROM tomcat:10.1-jre{v}
RUN rm -rf /usr/local/tomcat/webapps/*
COPY --from=build /out/ROOT.war /usr/local/tomcat/webapps/ROOT.war
EXPOSE 8080
"""
        return BuildPlan("java-war", 8080, GENERATED, dockerfile, [f"{lang} WAR built with {tool}, served by Tomcat 10 on 8080."],
                         lang, fw, tool, v)
    dockerfile = f"""FROM {image} AS build
WORKDIR /src
COPY . .
RUN {build} && mkdir -p /out && {PICK_JAR.format(dir=out_dir)} && test -f /out/app.jar

FROM eclipse-temurin:{v}-jre
WORKDIR /app
COPY --from=build /out/app.jar /app/app.jar
ENV PORT={port} SERVER_PORT={port} JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=75"
EXPOSE {port}
CMD ["java", "-jar", "/app/app.jar"]
"""
    return BuildPlan("java-spring" if spring else "java", port, GENERATED, dockerfile,
                     [f"{lang} {v} app built with {tool} (`{build}`), run with `java -jar` on port {port}"
                      + (" (SERVER_PORT env)." if spring else " (PORT env).")], lang, fw, tool, v)


# ------------------------------------------------------------------------- Go
def go_plan(root: Path, options: dict) -> BuildPlan:
    mod = _read(root / "go.mod")
    m = re.search(r"^go\s+(1\.\d+)", mod, re.M)
    v = m.group(1) if m and int(m.group(1).split(".")[1]) >= 21 else "1.23"
    v = str(options.get("runtime_version") or v)
    pkg = "."
    if not list(root.glob("*.go")):
        mains = sorted(p.parent for p in root.glob("cmd/*/main.go"))
        if mains:
            pkg = "./" + str(mains[0].relative_to(root))
    fw = next((label for name, label in (("gin-gonic/gin", "Gin"), ("labstack/echo", "Echo"), ("gofiber/fiber", "Fiber"),
                                          ("go-chi/chi", "chi")) if name in mod), "")
    return BuildPlan("go", 8080, GENERATED, f"""FROM golang:{v}-alpine AS build
WORKDIR /src
COPY go.* ./
RUN go mod download || true
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /out/app {pkg}

FROM alpine:3.20
RUN apk add --no-cache ca-certificates tzdata
COPY --from=build /out/app /app
ENV PORT=8080 GIN_MODE=release
EXPOSE 8080
CMD ["/app"]
""", [f"Go {v} module built with `go build {pkg}`, port 8080 (PORT env)."], "Go", fw, "go modules", v)


# ----------------------------------------------------------------------- Rust
def rust_plan(root: Path, options: dict) -> BuildPlan:
    cargo = _read(root / "Cargo.toml")
    m = re.search(r"\[\[bin\]\][^\[]*?name\s*=\s*\"([^\"]+)\"", cargo, re.S) or \
        re.search(r"\[package\][^\[]*?name\s*=\s*\"([^\"]+)\"", cargo, re.S)
    if not m:
        raise NotBuildable("Cargo.toml has no [package] name, so the binary to run is unknown.")
    name = m.group(1)
    fw = next((label for dep, label in (("actix-web", "Actix Web"), ("axum", "Axum"), ("rocket", "Rocket"),
                                        ("warp", "warp")) if re.search(rf"^{dep}\s*=", cargo, re.M)), "")
    return BuildPlan("rust", 8080, GENERATED, f"""FROM rust:1-slim AS build
WORKDIR /src
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*
COPY . .
RUN cargo build --release --bin {name} && cp target/release/{name} /out-app

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libssl3 && rm -rf /var/lib/apt/lists/*
COPY --from=build /out-app /usr/local/bin/app
ENV PORT=8080 HOST=0.0.0.0 ROCKET_ADDRESS=0.0.0.0 ROCKET_PORT=8080
EXPOSE 8080
CMD ["app"]
""", [f"Rust binary `{name}` built with `cargo build --release`, port 8080 (PORT env)."], "Rust", fw, "Cargo", "")


# ----------------------------------------------------------------------- Ruby
def ruby_plan(root: Path, options: dict) -> BuildPlan:
    gems = _read(root / "Gemfile")
    m = re.search(r"(\d\.\d)", _read(root / ".ruby-version")) or re.search(r"^ruby\s+['\"](\d\.\d)", gems, re.M)
    v = m.group(1) if m else "3.3"
    rails = re.search(r"gem\s+['\"]rails['\"]", gems) is not None
    if rails:
        fw, kind, port = "Rails", "ruby-rails", 3000
        cmd = '["sh", "-c", "bin/rails db:prepare 2>/dev/null || true; exec bin/rails server -b 0.0.0.0 -p 3000"]'
        extra = "\nRUN SECRET_KEY_BASE_DUMMY=1 bin/rails assets:precompile 2>/dev/null || true"
        env = "RAILS_ENV=production RAILS_SERVE_STATIC_FILES=1 RAILS_LOG_TO_STDOUT=1 SECRET_KEY_BASE_DUMMY=1"
    elif (root / "config.ru").exists():
        fw = "Sinatra" if "sinatra" in gems else "Rack"
        kind, port, extra, env = "ruby-rack", 9292, "", "RACK_ENV=production"
        cmd = '["bundle", "exec", "rackup", "-o", "0.0.0.0", "-p", "9292"]'
    else:
        entry = next((c for c in ("app.rb", "main.rb", "server.rb") if (root / c).exists()), None)
        if not entry:
            raise NotBuildable("Ruby project without Rails, config.ru or app.rb/main.rb.")
        fw = "Sinatra" if "sinatra" in gems else ""
        kind, port, extra, env = "ruby", 4567, "", "APP_ENV=production RACK_ENV=production"
        cmd = json.dumps(["bundle", "exec", "ruby", entry, "-o", "0.0.0.0"])
    return BuildPlan(kind, port, GENERATED, f"""FROM ruby:{v}-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev libyaml-dev git \\
    && rm -rf /var/lib/apt/lists/*
ENV {env} PORT={port} BUNDLE_WITHOUT=development:test
COPY Gemfile* ./
RUN bundle install
COPY . .{extra}
EXPOSE {port}
CMD {cmd}
""", [f"Ruby {v} app ({fw or 'script'}): `bundle install`, served on port {port}."], "Ruby", fw, "Bundler", v)


# ------------------------------------------------------------------------ PHP
def php_plan(root: Path, options: dict) -> BuildPlan:
    composer = (root / "composer.json").exists()
    laravel = (root / "artisan").exists()
    docroot = "public" if laravel or (root / "public/index.php").exists() else ""
    m = re.search(r"\"php\"\s*:\s*\"[^\d]*(8\.\d)", _read(root / "composer.json"))
    v = m.group(1) if m else "8.3"
    lines = [f"FROM php:{v}-apache",
             "RUN apt-get update && apt-get install -y --no-install-recommends git unzip libzip-dev libpq-dev "
             "&& docker-php-ext-install pdo pdo_mysql pdo_pgsql zip && a2enmod rewrite && rm -rf /var/lib/apt/lists/*"]
    if docroot:
        lines.append(f"ENV APACHE_DOCUMENT_ROOT=/var/www/html/{docroot}")
        lines.append("RUN sed -ri -e 's!/var/www/html!${APACHE_DOCUMENT_ROOT}!g' /etc/apache2/sites-available/*.conf "
                     "&& sed -ri -e 's!AllowOverride None!AllowOverride All!g' /etc/apache2/apache2.conf")
    lines.append("WORKDIR /var/www/html")
    if composer:
        lines.append("COPY --from=composer:2 /usr/bin/composer /usr/bin/composer")
    lines.append("COPY . .")
    if composer:
        lines.append("RUN composer install --no-dev --optimize-autoloader --no-interaction --no-progress")
    if laravel:
        lines.append("RUN cp -n .env.example .env 2>/dev/null; php artisan key:generate --force || true; "
                     "chown -R www-data:www-data storage bootstrap/cache")
    lines.append("EXPOSE 80")
    return BuildPlan("php-laravel" if laravel else "php", 80, GENERATED, "\n".join(lines) + "\n",
                     [f"PHP {v} on Apache" + (" with Composer" if composer else "")
                      + (f", document root /{docroot}" if docroot else "") + ", port 80."],
                     "PHP", "Laravel" if laravel else "", "Composer" if composer else "", v)


# ----------------------------------------------------------------------- .NET
def dotnet_plan(root: Path, options: dict) -> BuildPlan:
    projs = sorted(root.glob("*.csproj")) + sorted(root.glob("*.fsproj")) or sorted(root.glob("*/*.csproj"))
    web = [p for p in projs if "Microsoft.NET.Sdk.Web" in _read(p)]
    proj = (web or projs)[0]
    text = _read(proj)
    m = re.search(r"<TargetFramework>net(\d+\.\d+)", text)
    v = m.group(1) if m else "8.0"
    an = re.search(r"<AssemblyName>([^<]+)", text)
    dll = (an.group(1) if an else proj.stem) + ".dll"
    rel = str(proj.relative_to(root))
    return BuildPlan("dotnet", 8080, GENERATED, f"""FROM mcr.microsoft.com/dotnet/sdk:{v} AS build
WORKDIR /src
COPY . .
RUN dotnet publish "{rel}" -c Release -o /out

FROM mcr.microsoft.com/dotnet/aspnet:{v}
WORKDIR /app
COPY --from=build /out .
ENV ASPNETCORE_URLS=http://+:8080 PORT=8080
EXPOSE 8080
ENTRYPOINT ["dotnet", "{dll}"]
""", [f".NET {v} project {rel} published with `dotnet publish`, port 8080."], "F#" if proj.suffix == ".fsproj" else "C#",
                     "ASP.NET Core" if web else "", "dotnet", v)


def static_plan(root: Path, options: dict) -> BuildPlan:
    return BuildPlan("static", 80, GENERATED, "FROM nginx:1.27-alpine\nCOPY . /usr/share/nginx/html\nEXPOSE 80\n",
                     ["Static site served by nginx."], "HTML", "", "nginx", "")


# Order matters: the first match wins (a Node frontend with a Python helper script is a Node app).
DETECTORS: list[tuple[str, callable, callable]] = [
    ("bun", lambda r: (r / "package.json").exists() and ((r / "bun.lockb").exists() or (r / "bun.lock").exists())
     and not any((r / f).exists() for f in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock")), bun_plan),
    ("node", lambda r: (r / "package.json").exists(), node_plan),
    ("deno", lambda r: (r / "deno.json").exists() or (r / "deno.jsonc").exists(), deno_plan),
    ("java", lambda r: any((r / f).exists() for f in ("pom.xml", "build.gradle", "build.gradle.kts")), java_plan),
    ("python", lambda r: any((r / f).exists() for f in ("requirements.txt", "pyproject.toml", "manage.py", "Pipfile")),
     python_plan),
    ("go", lambda r: (r / "go.mod").exists(), go_plan),
    ("rust", lambda r: (r / "Cargo.toml").exists(), rust_plan),
    ("dotnet", lambda r: bool(list(r.glob("*.csproj")) or list(r.glob("*.fsproj")) or list(r.glob("*/*.csproj"))),
     dotnet_plan),
    ("ruby", lambda r: (r / "Gemfile").exists(), ruby_plan),
    ("php", lambda r: (r / "composer.json").exists() or (r / "index.php").exists(), php_plan),
    ("static", lambda r: (r / "index.html").exists(), static_plan),
]
MARKERS = ("Dockerfile", "package.json", "deno.json", "pom.xml", "build.gradle", "build.gradle.kts", "requirements.txt",
           "pyproject.toml", "manage.py", "Pipfile", "go.mod", "Cargo.toml", "Gemfile", "composer.json", "index.php",
           "index.html")
SKIP_SUBDIRS = {"docs", "doc", ".github", "test", "tests", "examples", "example", "scripts", "node_modules", "vendor",
                "infra", "deploy", "k8s", "helm", ".git", "assets", "public", "static"}
# When a repo holds several apps, the one people usually mean by "deploy the repo".
SUBDIR_PREFERENCE = ("app", "web", "server", "backend", "api", "frontend", "client", "site", "src")


def languages(root: Path) -> list[str]:
    """Every stack whose marker files are at this folder (a Node frontend + Python API shows both)."""
    return [name for name, match, _ in DETECTORS if match(root)]


def candidates(root: Path) -> list[str]:
    """Subfolders (one level down) that look deployable on their own."""
    out = []
    for p in sorted(root.iterdir()) if root.is_dir() else []:
        if p.is_dir() and p.name not in SKIP_SUBDIRS and not p.name.startswith("."):
            if any((p / m).exists() for m in MARKERS) or list(p.glob("*.csproj")):
                out.append(p.name)
    return out


def _detect_here(root: Path, options: dict) -> BuildPlan | None:
    if (root / "Dockerfile").exists() and not options.get("ignore_dockerfile"):
        df = _read(root / "Dockerfile")
        port = int(options.get("port") or exposed_port(df) or 80)
        found = languages(root)
        return BuildPlan("dockerfile", port, "Dockerfile", None,
                         [f"Using the repo's Dockerfile; app port {port}"
                          + (" (from EXPOSE)." if exposed_port(df) else " (no EXPOSE, assumed).")],
                         found[0].title() if found else "", "", "Dockerfile", "")
    for _name, match, make in DETECTORS:
        if match(root):
            return make(root, options)
    return None


def detect(root: Path, options: dict | None = None) -> BuildPlan:
    options = options or {}
    plan = _detect_here(root, options)
    if plan is None:
        subs = candidates(root)
        pick = subs[0] if len(subs) == 1 else next((s for s in SUBDIR_PREFERENCE if s in subs), None)
        if pick and (plan := _detect_here(root / pick, options)):
            plan.subdir = pick
            plan.notes.insert(0, f"No app at the repo root; found one in {pick}/"
                                 + (f" (other folders: {', '.join(s for s in subs if s != pick)}; set the folder "
                                    "to deploy one of those instead)." if len(subs) > 1 else "."))
        elif subs:
            raise NotBuildable(f"No app at the repo root, and several folders could be one: {', '.join(subs)}. "
                               "Set the folder to deploy.")
        else:
            raise NotBuildable("Could not detect how to build this repo: no Dockerfile, package.json, pom.xml, "
                               "build.gradle, requirements.txt/pyproject.toml, go.mod, Cargo.toml, Gemfile, "
                               "composer.json, *.csproj or index.html at its root.")
    others = [x for x in languages(root / plan.subdir if plan.subdir else root)
              if x not in (plan.language.lower(), plan.kind.split("-")[0], "static")]
    if others:
        plan.notes.append(f"Also found {', '.join(others)} files here; deployed as {plan.stack}. "
                          "Add a Dockerfile if the app should start differently.")
    if options.get("port"):
        plan.port = int(options["port"])
    return plan
