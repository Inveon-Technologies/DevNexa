"""Real build, test and deploy of the sample app for live-mode pipelines.

In live mode (with the Docker socket available) the Build Agent runs ``docker build`` on the sample app source,
the Test Agent runs the sample app's pytest suite inside the freshly built image, and the Deployment Agent swaps
the running ``devnexa-sample-app`` container to the new image, rolling back to the previous image if it does not
come up. Source comes from the pushed commit when the run was triggered by GitHub, otherwise from the checkout
mounted at ``DEVNEXA_SAMPLE_APP_SRC``. Fault-lab runs (injected defects) stay simulated on purpose.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable

import httpx

from ..config import get_settings
from .deploy import executor

IMAGE = "devnexa/sample-app"
Line = Callable[[str], None]


def enabled(ctx, defect: str | None) -> bool:
    s = get_settings()
    return (ctx.env.mode == "live" and not defect and executor.available()
            and bool(s.sample_app_src or s.github_repo))


def checkout(run_id: int, repo: str, sha: str, token: str, out: Line) -> tuple[Path, Path | None]:
    """Returns (source dir, temp dir to clean up). Clones the pushed commit when possible."""
    s = get_settings()
    if re.fullmatch(r"[0-9a-f]{40}", sha or "") and "/" in (repo or ""):
        work = Path(s.apps_workdir) / f"ci-{run_id}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        auth = f"x-access-token:{token}@" if token else ""
        steps = [["git", "-C", str(work), "init", "-q"],
                 ["git", "-C", str(work), "fetch", "-q", "--depth", "1", f"https://{auth}github.com/{repo}.git", sha],
                 ["git", "-C", str(work), "checkout", "-q", "FETCH_HEAD"]]
        out(f"$ git fetch {repo} {sha[:7]}")
        for cmd in steps:
            code, text = executor.run(cmd, timeout=300)
            if code != 0:
                out(text.replace(token, "***") if token else text)
                out("Could not fetch the pushed commit; building the server's checkout instead.")
                shutil.rmtree(work, ignore_errors=True)
                break
        else:
            sub = work / s.sample_app_subdir if s.sample_app_subdir else work
            if (sub / "Dockerfile").is_file():
                return sub, work
            out(f"No Dockerfile in {s.sample_app_subdir or 'repo root'} of {repo}; building the server's checkout.")
            shutil.rmtree(work, ignore_errors=True)
    src = Path(s.sample_app_src)
    if not (src / "Dockerfile").is_file():
        raise FileNotFoundError(f"sample app source not found at {src}")
    return src, None


def build(src: Path, sha: str, out: Line) -> tuple[bool, str, str]:
    image = f"{IMAGE}:{(sha or 'local')[:7]}"
    out(f"$ docker build -t {image} .")
    code, text = executor.run(["docker", "build", "--progress=plain", "-t", image, str(src)], out,
                              timeout=get_settings().apps_build_timeout, env={"DOCKER_BUILDKIT": "1"})
    return code == 0, image, text


_COUNT = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")


def parse_pytest(text: str) -> dict:
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    tail = "\n".join(text.splitlines()[-5:])
    for n, kind in _COUNT.findall(tail):
        counts["error" if kind.startswith("error") else kind] += int(n)
    dur = re.search(r"in ([\d.]+)s", tail)
    counts["duration_s"] = float(dur.group(1)) if dur else 0.0
    counts["failed_tests"] = re.findall(r"^FAILED (\S+)", text, re.M)
    return counts


def test(src: Path, image: str, out: Line) -> tuple[bool, str, dict]:
    """Runs the repo's tests inside the built image, so they see exactly what will be deployed."""
    name = f"devnexa-ci-{uuid.uuid4().hex[:8]}"
    cmd = "pip install -q pytest >/dev/null 2>&1; python -m pytest -q -p no:cacheprovider"
    out(f"$ docker run {image} pytest -q")
    code, text = executor.run(["docker", "create", "--name", name, image, "sh", "-c", cmd])
    if code != 0:
        return False, text, parse_pytest("")
    try:
        for part in ("tests", "pytest.ini", "conftest.py"):
            if (src / part).exists():
                executor.run(["docker", "cp", str(src / part), f"{name}:/srv/{part}"])
        code, text = executor.run(["docker", "start", "-a", name], out, timeout=600)
    finally:
        executor.run(["docker", "rm", "-f", name])
    summary = parse_pytest(text)
    return code == 0 and summary["failed"] == 0 and summary["error"] == 0, text, summary


def _run_args(info: dict, image: str) -> list[str]:
    """Rebuilds `docker run` arguments from `docker inspect`, so the new container keeps the compose
    network, aliases, ports, volumes, env and labels of the one it replaces."""
    cfg, host = info["Config"], info["HostConfig"]
    args = ["docker", "run", "-d", "--name", info["Name"].lstrip("/")]
    nets = info["NetworkSettings"]["Networks"]
    first = next(iter(nets), None)
    if first:
        args += ["--network", first]
        for alias in (nets[first].get("Aliases") or []):
            if alias != info["Id"][:12]:
                args += ["--network-alias", alias]
    for e in cfg.get("Env") or []:
        if not e.startswith("PATH="):
            args += ["-e", e]
    for m in info.get("Mounts") or []:
        src = m.get("Name") if m.get("Type") == "volume" else m.get("Source")
        args += ["-v", f"{src}:{m['Destination']}" + ("" if m.get("RW", True) else ":ro")]
    for cport, binds in (host.get("PortBindings") or {}).items():
        for b in binds or []:
            ip = f"{b['HostIp']}:" if b.get("HostIp") else ""
            args += ["-p", f"{ip}{b['HostPort']}:{cport.split('/')[0]}"]
    for k, v in (cfg.get("Labels") or {}).items():
        args += ["--label", f"{k}={v}"]
    policy = (host.get("RestartPolicy") or {}).get("Name")
    if policy and policy != "no":
        args += ["--restart", policy]
    return args + [image]


def current_image(container: str) -> str | None:
    code, text = executor.run(["docker", "inspect", "-f", "{{.Config.Image}}", container])
    return text.strip() if code == 0 else None


def swap(container: str, image: str, out: Line) -> tuple[bool, str]:
    code, text = executor.run(["docker", "inspect", container])
    if code != 0:
        return False, f"container {container} not found: {text}"
    info = json.loads(text)[0]
    args = _run_args(info, image)
    out(f"$ docker rm -f {container} && docker run ... {image}")
    executor.run(["docker", "rm", "-f", container])
    code, text = executor.run(args)
    if code != 0:
        out(text)
        out(f"Starting {image} failed; restoring {info['Config']['Image']}")
        executor.run(_run_args(info, info["Config"]["Image"]))
        return False, text
    nets = list(info["NetworkSettings"]["Networks"])[1:]
    for net in nets:
        executor.run(["docker", "network", "connect", net, container])
    return True, text


def wait_ready(timeout: float | None = None) -> bool:
    s = get_settings()
    deadline = time.monotonic() + (timeout or s.apps_health_timeout)
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{s.sample_app_url}/health", timeout=3.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(2)
    return False
