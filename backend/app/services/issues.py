"""Live issue capture for deployed apps.

Three sources feed one list of issues per app, grouped by fingerprint so a repeated error is one row with a count:

* browser: a small script (`/__devnexa/agent.js`) injected into the app's HTML reports JavaScript errors, failed
  API calls, failed images/scripts, console errors and warnings, blank pages and empty sections, each with the
  page path and the CSS selector of the section involved. In "report mode" a person clicks a section and says
  what is wrong with it.
* server: the Monitoring Agent turns error lines and HTTP 5xx access-log lines from the container into issues.
* scan: a crawl of the app's pages flags HTTP errors, broken assets and pages with no content.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AppDeployment, AppIssue
from .audit import audit

KINDS = {
    "js_error": ("error", "JavaScript error"),
    "api_error": ("error", "API call failed"),
    "resource": ("error", "File failed to load"),
    "console_error": ("error", "Console error"),
    "console_warning": ("warning", "Console warning"),
    "blank_page": ("error", "Blank page"),
    "blank_section": ("warning", "Empty section"),
    "user_report": ("error", "Reported by a person"),
    "server_error": ("error", "Server error"),
    "http_error": ("error", "Page returns an error"),
    "broken_asset": ("error", "Broken asset"),
}
SOURCES = {"js_error": "browser", "api_error": "browser", "resource": "browser", "console_error": "browser",
           "console_warning": "browser", "blank_page": "browser", "blank_section": "browser", "user_report": "user",
           "server_error": "server", "http_error": "scan", "broken_asset": "scan"}


def now() -> datetime:
    return datetime.now(timezone.utc)


_VOLATILE = re.compile(r"0x[0-9a-f]+|\b[0-9a-f]{8,}\b|\b\d+\b|\?[^\s'\"]*|[\w.+-]+@[\w-]+\.[\w.]+", re.I)


def normalize(text: str) -> str:
    return _VOLATILE.sub("#", (text or "").strip().lower())[:400]


def fingerprint(kind: str, message: str, page: str, selector: str | None) -> str:
    # Page is part of the key for page-shaped problems; a JS error with the same message on two pages is one bug.
    where = page if kind in ("blank_page", "blank_section", "http_error", "user_report") else ""
    raw = "|".join([kind, normalize(message), where or "", (selector or "") if kind != "js_error" else ""])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _clip(v, n: int) -> str | None:
    if v is None:
        return None
    return str(v)[:n]


def record(db: Session, app: str, kind: str, message: str, page: str = "/", selector: str | None = None,
           section: str | None = None, stack: str | None = None, sample: dict | None = None,
           deployment_id: int | None = None, source: str | None = None) -> tuple[AppIssue, bool]:
    """Add one occurrence. Returns (issue, is_new). A resolved issue that comes back is reopened."""
    kind = kind if kind in KINDS else "js_error"
    page = (urlparse(page).path or "/")[:300] if page else "/"
    fp = fingerprint(kind, message, page, selector)
    issue = db.scalars(select(AppIssue).where(AppIssue.app == app, AppIssue.fingerprint == fp)).first()
    if issue:
        issue.count += 1
        issue.last_seen = now()
        issue.sample = sample or issue.sample
        if deployment_id:
            issue.deployment_id = deployment_id
        if issue.status == "resolved":
            issue.status, issue.reopened = "open", issue.reopened + 1
            audit(db, "monitoring-agent", "app.issue.reopened", "app_issue", issue.id,
                  f"{app}: '{issue.message[:80]}' came back after the fix")
        return issue, False
    issue = AppIssue(app=app, fingerprint=fp, source=source or SOURCES.get(kind, "browser"), kind=kind,
                     severity=KINDS[kind][0], message=_clip(message, 2000) or KINDS[kind][1], page=page,
                     selector=_clip(selector, 300), section=_clip(section, 300), stack=_clip(stack, 8000),
                     sample=sample or {}, deployment_id=deployment_id)
    db.add(issue)
    db.flush()
    return issue, True


def summary(db: Session, app: str) -> dict:
    rows = db.scalars(select(AppIssue).where(AppIssue.app == app)).all()
    open_ = [r for r in rows if r.status in ("open", "fixing", "in_preview")]
    return {"open": len(open_), "errors": sum(1 for r in open_ if r.severity == "error"),
            "warnings": sum(1 for r in open_ if r.severity == "warning"),
            "pages": sorted({r.page for r in open_})[:20]}


# ------------------------------------------------------------- browser agent
_hits: dict[str, deque] = defaultdict(deque)


def rate_ok(key: str, limit: int = 60, window: float = 60.0) -> bool:
    q, t = _hits[key], time.monotonic()
    while q and t - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(t)
    return True


def ingest_report(db: Session, app: str, data: dict) -> AppIssue | None:
    kind = str(data.get("kind") or "js_error")
    if kind not in KINDS or SOURCES[kind] not in ("browser", "user"):
        return None
    live = db.scalars(select(AppDeployment).where(AppDeployment.app == app, AppDeployment.status == "running")
                      .order_by(AppDeployment.id.desc())).first()
    sample = {k: _clip(data.get(k), n) for k, n in (("url", 500), ("ua", 300), ("html", 3000), ("text", 1000),
                                                     ("last_click", 300), ("viewport", 40)) if data.get(k)}
    issue, _ = record(db, app, kind, str(data.get("message") or ""), str(data.get("page") or "/"),
                      _clip(data.get("selector"), 300), _clip(data.get("section"), 300), _clip(data.get("stack"), 8000),
                      sample, live.id if live else None)
    return issue


# Kept small and dependency-free: it runs on every page of the user's app.
AGENT_JS = r"""(function () {
  if (window.__devnexa) return; window.__devnexa = true;
  var me = document.currentScript, base = "", app = "";
  if (me && me.src) { base = me.src.replace(/\/__devnexa\/agent\.js.*$/, ""); app = me.getAttribute("data-app") || ""; }
  var seen = {}, sent = 0, lastClick = "";
  function cssPath(el) {
    if (!el || el.nodeType !== 1) return "";
    var parts = [];
    while (el && el.nodeType === 1 && parts.length < 5) {
      if (el.id) { parts.unshift("#" + el.id); break; }
      var p = el.tagName.toLowerCase(), c = (typeof el.className === "string" ? el.className : "").trim().split(/\s+/).filter(function (x) { return x && x.length < 30; }).slice(0, 2);
      if (c.length) p += "." + c.join(".");
      var sib = el.parentNode ? Array.prototype.filter.call(el.parentNode.children, function (x) { return x.tagName === el.tagName; }) : [];
      if (sib.length > 1) p += ":nth-of-type(" + (sib.indexOf(el) + 1) + ")";
      parts.unshift(p);
      if (el.tagName === "BODY") break;
      el = el.parentElement;
    }
    return parts.join(" > ");
  }
  function sectionOf(el) {
    var s = el && el.closest && el.closest("[data-section],section,main,header,footer,nav,article,aside,form,[id]");
    return s ? cssPath(s) : "";
  }
  function fmt(a) { try { return typeof a === "string" ? a : (a && a.message) || JSON.stringify(a); } catch (e) { return String(a); } }
  function send(kind, d) {
    var key = kind + "|" + (d.message || "") + "|" + location.pathname + "|" + (d.selector || "");
    if (seen[key] || sent > 40) return; seen[key] = 1; sent++;
    d.kind = kind; d.app = app; d.page = location.pathname; d.url = location.href; d.ua = navigator.userAgent;
    d.viewport = innerWidth + "x" + innerHeight; if (lastClick) d.last_click = lastClick;
    var body = JSON.stringify(d), url = base + "/__devnexa/report";
    try { if (navigator.sendBeacon && navigator.sendBeacon(url, new Blob([body], { type: "text/plain" }))) return; } catch (e) {}
    try { origFetch && origFetch.call(window, url, { method: "POST", body: body, keepalive: true, headers: { "Content-Type": "text/plain" } }).catch(function () {}); } catch (e) {}
  }
  document.addEventListener("click", function (e) { lastClick = cssPath(e.target); }, true);
  window.addEventListener("error", function (e) {
    var t = e.target;
    if (t && t !== window && t.nodeType === 1) {
      var src = t.src || t.href || "";
      if (src.indexOf("/__devnexa/") < 0) send("resource", { message: "Failed to load " + t.tagName.toLowerCase() + " " + src, selector: cssPath(t), section: sectionOf(t) });
      return;
    }
    send("js_error", { message: String(e.message || "Script error"), stack: (e.error && e.error.stack) || (e.filename + ":" + e.lineno + ":" + e.colno) });
  }, true);
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason; send("js_error", { message: "Unhandled promise rejection: " + fmt(r), stack: r && r.stack });
  });
  ["error", "warn"].forEach(function (lvl) {
    var orig = console[lvl];
    console[lvl] = function () {
      try { send(lvl === "error" ? "console_error" : "console_warning", { message: Array.prototype.map.call(arguments, fmt).join(" ").slice(0, 1000) }); } catch (e) {}
      return orig.apply(console, arguments);
    };
  });
  var origFetch = window.fetch;
  if (origFetch) window.fetch = function (input, init) {
    var method = (init && init.method) || (input && input.method) || "GET", u = (input && input.url) || String(input);
    return origFetch.apply(this, arguments).then(function (r) {
      if (r.status >= 500 && u.indexOf("/__devnexa/") < 0) send("api_error", { message: method + " " + u.replace(location.origin, "") + " returned " + r.status });
      return r;
    }, function (err) {
      if (u.indexOf("/__devnexa/") < 0) send("api_error", { message: method + " " + u.replace(location.origin, "") + " failed: " + fmt(err) });
      throw err;
    });
  };
  var XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__dn = (m || "GET") + " " + u; return XO.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    var x = this;
    x.addEventListener("loadend", function () {
      if (x.status >= 500 || x.status === 0) send("api_error", { message: x.__dn + (x.status ? " returned " + x.status : " failed (network)") });
    });
    return XS.apply(this, arguments);
  };
  function visible(el) { return el.offsetWidth > 0 || el.offsetHeight > 0; }
  function hasContent(el) { return (el.innerText || "").trim().length > 0 || el.querySelector("img,svg,canvas,video,iframe,input,button,select,textarea,picture"); }
  function checkBlank() {
    var b = document.body;
    if (!b) return;
    if (!hasContent(b)) { send("blank_page", { message: "Page is blank after loading", selector: "body" }); return; }
    var n = 0;
    Array.prototype.forEach.call(document.querySelectorAll("main,section,article,[data-section]"), function (el) {
      if (n < 3 && visible(el) && el.offsetHeight > 40 && !hasContent(el)) { n++; send("blank_section", { message: "Section has no content", selector: cssPath(el), section: cssPath(el) }); }
    });
  }
  function later() { setTimeout(checkBlank, 4000); }
  if (document.readyState === "complete") later(); else window.addEventListener("load", later);
  ["pushState", "replaceState"].forEach(function (k) { var o = history[k]; history[k] = function () { var r = o.apply(this, arguments); later(); return r; }; });
  window.addEventListener("popstate", later);

  // Report mode: open the site with ?devnexa-report=1 (or press Alt+Shift+R), click the broken section, describe it.
  var picking = false, box = null, bar = null;
  function report() {
    if (picking) return; picking = true;
    try { sessionStorage.setItem("devnexa-report", "1"); } catch (e) {}
    box = document.createElement("div");
    box.style.cssText = "position:fixed;pointer-events:none;z-index:2147483646;border:2px solid #f43f5e;background:rgba(244,63,94,.08);border-radius:4px;transition:all .05s";
    bar = document.createElement("div");
    bar.style.cssText = "position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:2147483647;background:#0f172a;color:#fff;font:14px system-ui;padding:10px 14px;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.35)";
    bar.innerHTML = "DevNexa: click the section that has a problem &nbsp;<button style='margin-left:8px;background:#334155;color:#fff;border:0;border-radius:6px;padding:4px 8px;cursor:pointer'>Done</button>";
    bar.querySelector("button").onclick = function (e) { e.stopPropagation(); stop(); };
    document.body.appendChild(box); document.body.appendChild(bar);
    document.addEventListener("mousemove", hover, true); document.addEventListener("click", pick, true);
  }
  function stop() {
    picking = false; try { sessionStorage.removeItem("devnexa-report"); } catch (e) {}
    document.removeEventListener("mousemove", hover, true); document.removeEventListener("click", pick, true);
    box && box.remove(); bar && bar.remove();
  }
  function hover(e) { if (bar.contains(e.target)) return; var r = e.target.getBoundingClientRect(); box.style.left = r.left + "px"; box.style.top = r.top + "px"; box.style.width = r.width + "px"; box.style.height = r.height + "px"; }
  function pick(e) {
    if (bar.contains(e.target)) return;
    e.preventDefault(); e.stopPropagation();
    var el = e.target, what = window.prompt("What is wrong with this section?");
    if (what === null) return;
    seen = {}; send("user_report", { message: what || "Something is wrong here", selector: cssPath(el), section: sectionOf(el),
      html: (el.outerHTML || "").slice(0, 3000), text: (el.innerText || "").slice(0, 1000) });
    bar.firstChild.textContent = "Thanks, sent to DevNexa. Click another section or ";
  }
  document.addEventListener("keydown", function (e) { if (e.altKey && e.shiftKey && (e.key === "R" || e.key === "r")) report(); });
  var wants = /[?&]devnexa-report=1/.test(location.search); try { wants = wants || sessionStorage.getItem("devnexa-report") === "1"; } catch (e) {}
  if (wants) { if (document.body) report(); else document.addEventListener("DOMContentLoaded", report); }
})();
"""


def script_tag(app: str) -> str:
    from ..config import get_settings

    s = get_settings()
    base = "" if s.apps_domain else f"http://{s.apps_public_host}:8000"
    return f'<script src="{base}/__devnexa/agent.js" data-app="{app}" defer></script>'


HTML_CANDIDATES = ("index.html", "public/index.html", "src/index.html", "static/index.html", "templates/index.html",
                   "templates/base.html", "templates/layout.html", "app/templates/base.html")


def inject_agent(root: Path, app: str) -> list[str]:
    """Add the issue-capture script to the app's HTML entry files in the build checkout (never committed)."""
    tag = script_tag(app)
    done = []
    for rel in HTML_CANDIDATES:
        p = root / rel
        if not p.is_file():
            continue
        text = p.read_text(errors="replace")
        if "/__devnexa/agent.js" in text:
            continue
        if re.search(r"</head>", text, re.I):
            text = re.sub(r"</head>", tag + "\n</head>", text, count=1, flags=re.I)
        elif re.search(r"<body[^>]*>", text, re.I):
            text = re.sub(r"(<body[^>]*>)", r"\1" + tag, text, count=1, flags=re.I)
        else:
            continue
        p.write_text(text)
        done.append(rel)
    return done


# ----------------------------------------------------------- server logs
ACCESS_5XX = re.compile(r'"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (\S+)[^"]*"\s+(5\d\d)\b')
ERROR_LINE = re.compile(r"\b(error|exception|fatal|panic|unhandled)\b|\b\w+(Error|Exception):", re.I)
_SKIP = re.compile(r"Traceback \(most recent call last\)|^\s+(File |at )|^\s*$|DeprecationWarning", re.I)


def from_logs(db: Session, app: str, dep_id: int, text: str) -> list[AppIssue]:
    """Turn new container log lines into server issues (one per distinct error, with the lines before it)."""
    lines = text.splitlines()
    out = []
    for i, ln in enumerate(lines):
        m = ACCESS_5XX.search(ln)
        if m:
            issue, _ = record(db, app, "server_error", f"{m.group(1)} {urlparse(m.group(2)).path} returned {m.group(3)}",
                              page=m.group(2), stack="\n".join(lines[max(0, i - 15): i + 1]),
                              deployment_id=dep_id, source="server")
            out.append(issue)
            continue
        if ERROR_LINE.search(ln) and not _SKIP.search(ln):
            msg = re.sub(r"^\S*\d{4}-\d\d-\d\dT[\d:.]+Z?\s*", "", ln).strip()
            issue, _ = record(db, app, "server_error", msg[:500], stack="\n".join(lines[max(0, i - 20): i + 1]),
                              deployment_id=dep_id, source="server")
            out.append(issue)
    return out


# -------------------------------------------------------------- page scan
class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.assets, self.text, self.scripts = [], [], [], 0
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "script":
            self.scripts += 1
            self._skip += 1
            if a.get("src"):
                self.assets.append(a["src"])
        if tag == "style":
            self._skip += 1
        if tag == "link" and a.get("href") and "stylesheet" in (a.get("rel") or ""):
            self.assets.append(a["href"])
        if tag == "img" and a.get("src") and not a["src"].startswith("data:"):
            self.assets.append(a["src"])

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.text.append(data.strip())


def scan(db: Session, app: str, base_url: str, max_pages: int = 15, fetch=None) -> dict:
    """Crawl the app's own pages and record HTTP errors, broken assets and pages with no content."""
    fetch = fetch or (lambda u: httpx.get(u, timeout=10.0, follow_redirects=True))
    host = urlparse(base_url).netloc
    queue, seen, checked_assets = [base_url.rstrip("/") + "/"], set(), set()
    found, pages = [], 0
    live = db.scalars(select(AppDeployment).where(AppDeployment.app == app, AppDeployment.status == "running")
                      .order_by(AppDeployment.id.desc())).first()
    dep_id = live.id if live else None
    while queue and pages < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        pages += 1
        path = urlparse(url).path or "/"
        try:
            r = fetch(url)
        except httpx.HTTPError as e:
            found.append(record(db, app, "http_error", f"{path} is unreachable: {type(e).__name__}", path,
                                deployment_id=dep_id, source="scan")[0])
            continue
        if r.status_code >= 400:
            found.append(record(db, app, "http_error", f"{path} returned HTTP {r.status_code}", path,
                                deployment_id=dep_id, source="scan")[0])
            continue
        if "html" not in r.headers.get("content-type", ""):
            continue
        p = _Links()
        try:
            p.feed(r.text)
        except Exception:  # noqa: BLE001 - malformed HTML is not an app issue in itself
            continue
        if not p.text and not p.scripts:
            found.append(record(db, app, "blank_page", f"{path} has no content", path, "body",
                                deployment_id=dep_id, source="scan")[0])
        for a in p.assets[:30]:
            au = urljoin(url, a)
            if urlparse(au).netloc != host or au in checked_assets:
                continue
            checked_assets.add(au)
            try:
                ar = fetch(au)
                bad = ar.status_code >= 400
            except httpx.HTTPError:
                bad = True
            if bad:
                found.append(record(db, app, "broken_asset", f"{urlparse(au).path} referenced by {path} is missing",
                                    path, deployment_id=dep_id, source="scan")[0])
        for href in p.links:
            u = urljoin(url, href).split("#")[0]
            if urlparse(u).netloc == host and u not in seen and not re.search(r"\.(png|jpe?g|gif|svg|pdf|zip|css|js)$", u, re.I):
                queue.append(u)
    return {"pages": pages, "issues": len({i.id for i in found})}
