"""Build failures the Deploy Agent now fixes by itself: npm peer conflicts (ERESOLVE), native modules, TS type
errors that block Vite, case-mismatched imports, wrong output folder; and LLM errors that say what went wrong."""

import json
from pathlib import Path

import httpx
import pytest

from app.config import get_settings
from app.services import analyzer, app_doctor, buildplan

from test_deploy import FakeExecutor, deploy_repo, fake, pushed  # noqa: F401  (fixture)

# The real log of vaibhavpetkar/vaibhav-portfolio deploy #8 (trimmed), as BuildKit prints it.
ERESOLVE_LOG = """#10 [build 4/6] RUN npm ci
#10 1.465 npm error code ERESOLVE
#10 1.465 npm error ERESOLVE could not resolve
#10 1.465 npm error
#10 1.465 npm error While resolving: react-day-picker@8.10.1
#10 1.465 npm error Found: react@19.2.4
#10 1.465 npm error node_modules/react
#10 1.465 npm error   react@"^19.2.0" from the root project
#10 1.465 npm error   peer react@">=16.8.0" from @floating-ui/react-dom@2.1.8
#10 1.465 npm error
#10 1.465 npm error Could not resolve dependency:
#10 1.465 npm error peer react@"^16.8.0 || ^17.0.0 || ^18.0.0" from react-day-picker@8.10.1
#10 1.465 npm error node_modules/react-day-picker
#10 1.465 npm error   react-day-picker@"^8.10.1" from the root project
#10 1.465 npm error
#10 1.465 npm error Conflicting peer dependency: react@18.3.1
#10 1.465 npm error node_modules/react
#10 1.465 npm error   peer react@"^16.8.0 || ^17.0.0 || ^18.0.0" from react-day-picker@8.10.1
#10 1.465 npm error Fix the upstream dependency conflict, or retry
#10 1.465 npm error this command with --force or --legacy-peer-deps
#10 1.471 npm error A complete log of this run can be found in: /root/.npm/_logs/x-debug-0.log
#10 ERROR: process "/bin/sh -c npm ci" did not complete successfully: exit code: 1
"""

VITE_PKG = json.dumps({"scripts": {"dev": "vite", "build": "tsc -b && vite build"},
                       "dependencies": {"react": "^19.2.0", "react-day-picker": "^8.10.1"},
                       "devDependencies": {"vite": "^6.0.0", "typescript": "^5.6.0"}})


@pytest.fixture
def no_registry(monkeypatch):
    monkeypatch.setattr(app_doctor, "compatible_release", lambda pkg, peer, ver, timeout=6.0: "9.11.1")


def test_peer_conflict_is_parsed_from_the_real_log():
    c = app_doctor.peer_conflict(ERESOLVE_LOG)
    assert c == {"package": "react-day-picker", "version": "8.10.1", "peer": "react",
                 "range": "^16.8.0 || ^17.0.0 || ^18.0.0", "found": "19.2.4"}


def test_semver_peer_ranges():
    rng = "^16.8.0 || ^17.0.0 || ^18.0.0"
    assert not app_doctor.range_allows(rng, "19.2.4") and app_doctor.range_allows(rng, "18.3.1")
    assert app_doctor.range_allows(">=16.8.0", "19.2.4")
    assert app_doctor.range_allows(">=16.8 <20", "19.0.0") and not app_doctor.range_allows(">=16.8 <19", "19.0.0")
    assert app_doctor.range_allows("^0.3.1", "0.3.9") and not app_doctor.range_allows("^0.3.1", "0.4.0")
    assert app_doctor.range_allows("18.x || 19.x", "19.1.0") and app_doctor.range_allows("*", "1.0.0")


def test_compatible_release_reads_the_npm_registry(monkeypatch):
    meta = {"versions": {"8.10.1": {"peerDependencies": {"react": "^16.8.0 || ^17.0.0 || ^18.0.0"}},
                         "9.4.0": {"peerDependencies": {"react": ">=16.8.0"}},
                         "9.11.1": {"peerDependencies": {"react": ">=16.8.0"}},
                         "10.0.0-rc.1": {"peerDependencies": {"react": ">=16.8.0"}}}}
    monkeypatch.setattr(app_doctor.httpx, "get", lambda url, **kw: httpx.Response(
        200, json=meta, request=httpx.Request("GET", url)))
    assert app_doctor.compatible_release("react-day-picker", "react", "19.2.4") == "9.11.1"


def test_install_flags_in_the_generated_dockerfile(tmp_path):
    (tmp_path / "package.json").write_text(VITE_PKG)
    (tmp_path / "package-lock.json").write_text("{}")
    plain = buildplan.detect(tmp_path).generated
    assert "RUN npm ci\n" in plain and "legacy" not in plain
    df = buildplan.detect(tmp_path, {"legacy_peer_deps": True, "build_tools": True, "skip_typecheck": True}).generated
    assert "RUN npm ci --legacy-peer-deps" in df and "apk add --no-cache python3 make g++" in df
    assert "RUN npx vite build" in df and "tsc" not in df
    (tmp_path / ".npmrc").write_text("legacy-peer-deps=true\n")
    assert "COPY package.json package-lock.json .npmrc ./" in buildplan.detect(tmp_path).generated
    (tmp_path / "vite.config.ts").write_text("export default { build: { outDir: 'build/web' } }")
    assert "COPY --from=build /app/build/web " in buildplan.detect(tmp_path).generated


def test_eresolve_redeploys_with_legacy_peer_deps_and_opens_an_npmrc_pr(client, fake, no_registry):
    files = {"package.json": VITE_PKG, "package-lock.json": "{}", "index.html": "<div id=root></div>"}
    ex = fake(files=files, build_ok=False, build_log=ERESOLVE_LOG)
    builds = []

    def build(cmd, on_line=None, timeout=None, env=None):
        if cmd[:2] == ["git", "clone"]:  # the fix branch carries the .npmrc DevNexa pushed
            fix = "devnexa/fix-" in " ".join(cmd)
            npmrc = next((p["files"] for p in pushed if ".npmrc" in p.get("files", {})), {})
            ex.files = {**files, **(npmrc if fix else {})}
        if cmd[:2] == ["docker", "build"]:
            df = Path(cmd[cmd.index("-f") + 1]).read_text()
            builds.append(df)
            ex.build_ok = "--legacy-peer-deps" in df or ".npmrc" in df
        return FakeExecutor.run(ex, cmd, on_line, timeout, env)
    ex.run = build
    d = client.get(f"/api/apps/deployments/{deploy_repo(client, repo='vaibhavpetkar/vaibhav-portfolio')['id']}").json()
    diag = d["diagnosis"]
    assert diag["category"] == "npm_peer_deps" and diag["action"] == "use_legacy_peer_deps"
    assert "react-day-picker@8.10.1" in diag["title"] and "19.2.4" in diag["title"]
    assert "react-day-picker@^9.11.1" in diag["suggestion"]
    assert diag["patch"] == {".npmrc": "legacy-peer-deps=true\n"}
    redeploy, pr = d["remediations"]
    assert redeploy["action"] == "use_legacy_peer_deps" and redeploy["status"] == "verified"
    assert pr["action"] == "open_fix_pr" and pr["branch"] == "devnexa/fix-vaibhav-portfolio-npm-peer-deps"
    assert pr["pr_url"].startswith("https://github.com/vaibhavpetkar/vaibhav-portfolio/pull/")
    assert any("RUN npm ci --legacy-peer-deps" in b for b in builds)  # the app itself is live again
    assert any("COPY package.json package-lock.json .npmrc ./" in b for b in builds)  # and the PR is verified


def test_eresolve_in_the_repos_own_dockerfile_gets_a_fix_pr(tmp_path, no_registry, client):
    (tmp_path / "package.json").write_text(VITE_PKG)
    (tmp_path / "Dockerfile").write_text("FROM node:20-alpine AS build\nCOPY package*.json ./\nRUN npm ci\n"
                                         "FROM nginx:alpine\n")
    from app.db import SessionLocal

    diag = app_doctor.diagnose(SessionLocal(), "build", ERESOLVE_LOG, tmp_path, "dockerfile")
    assert diag["action"] == "open_fix_pr"
    assert "FROM node:20-alpine AS build\nENV NPM_CONFIG_LEGACY_PEER_DEPS=true\n" in diag["patch"]["Dockerfile"]
    assert "FROM nginx:alpine\n" in diag["patch"]["Dockerfile"] and ".npmrc" in diag["patch"]


@pytest.mark.parametrize("log,category,action", [
    ("gyp ERR! stack Error: not found: make\ngyp ERR! node-gyp -v v10", "native_build", "add_build_tools"),
    ("#12 ERROR: failed to calculate checksum: \"/app/dist\": not found", "build_output_missing", "change_output_dir"),
    ("npm error 404 Not Found - GET https://registry.npmjs.org/reacct - Not found\n"
     "npm error 404  'reacct@^1.0.0' is not in this registry.", "npm_not_found", "escalate"),
    ("Bind for 0.0.0.0:3000 failed: port is already allocated", "port_in_use", "retry_deploy"),
    ("ERROR: failed to resolve source metadata for docker.io/library/node:99", "base_image", "escalate"),
    ("npm error code ETARGET\nnpm error notarget No matching version found for vite@^99.0.0.", "deps_resolution",
     "open_fix_pr"),
])
def test_more_build_failures_are_recognised(log, category, action):
    d = app_doctor.heuristic("build", log)
    assert (d["category"], d["action"]) == (category, action)


def test_type_errors_that_block_vite_are_bundled_without_the_check(tmp_path, client):
    (tmp_path / "package.json").write_text(VITE_PKG)
    from app.db import SessionLocal

    log = "src/App.tsx(3,7): error TS2322: Type 'string' is not assignable to type 'number'."
    diag = app_doctor.diagnose(SessionLocal(), "build", log, tmp_path, "node-static")
    assert diag["category"] == "syntax_error" and diag["action"] == "skip_typecheck" and diag["risk"] == "low"


def test_case_mismatched_import_is_fixed(tmp_path, client):
    (tmp_path / "src/components").mkdir(parents=True)
    (tmp_path / "src/components/Header.tsx").write_text("export default () => null\n")
    (tmp_path / "src/App.tsx").write_text('import Header from "./components/header"\n')
    from app.db import SessionLocal

    log = '[vite]: Rollup failed to resolve import "./components/header" from "/app/src/App.tsx".'
    diag = app_doctor.diagnose(SessionLocal(), "build", log, tmp_path, "node-static")
    assert diag["category"] == "import_path" and diag["action"] == "open_fix_pr"
    assert diag["patch"] == {"src/App.tsx": 'import Header from "./components/Header"\n'}


def test_llm_errors_say_what_the_provider_said(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "anthropic")
    monkeypatch.setattr(s, "llm_api_key", "sk-bad")
    monkeypatch.setattr(analyzer.time, "sleep", lambda x: None)

    def post(self, url, headers=None, json=None):
        return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error",
                                                                     "message": "invalid x-api-key"}},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.Client, "post", post)
    res = analyzer.llm_check()
    assert not res["ok"] and "HTTP 401 invalid x-api-key" in res["detail"] and "DEVNEXA_LLM_API_KEY" in res["detail"]
    base = app_doctor.heuristic("build", "SyntaxError: bad")
    d = app_doctor.llm_diagnose(analyzer.get_analyzer(), "build", "SyntaxError: bad", None, base)
    assert d["provider"].startswith("heuristic (LLM unavailable: HTTP 401")


def test_llm_retries_when_overloaded(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "anthropic")
    monkeypatch.setattr(s, "llm_api_key", "sk-ok")
    monkeypatch.setattr(analyzer.time, "sleep", lambda x: None)
    codes = iter([529, 200])

    def post(self, url, headers=None, json=None):
        c = next(codes)
        body = {"content": [{"type": "text", "text": "OK"}]} if c == 200 else {"error": {"message": "Overloaded"}}
        return httpx.Response(c, json=body, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.Client, "post", post)
    assert analyzer.llm_check() == {"ok": True, "provider": "anthropic", "model": s.llm_model, "detail": "Reply: OK"}


def test_known_mechanical_fixes_skip_the_llm(monkeypatch, tmp_path, client, no_registry):
    monkeypatch.setattr(get_settings(), "llm_provider", "anthropic")
    monkeypatch.setattr(app_doctor, "llm_diagnose", lambda *a, **k: pytest.fail("LLM should not be called"))
    (tmp_path / "package.json").write_text(VITE_PKG)
    from app.db import SessionLocal

    assert app_doctor.diagnose(SessionLocal(), "build", ERESOLVE_LOG, tmp_path, "node-static")["action"] == \
        "use_legacy_peer_deps"
