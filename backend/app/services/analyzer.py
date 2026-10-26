"""LLM + RAG diagnosis.

``HeuristicAnalyzer`` is an offline, deterministic evidence scorer used by default and as a fallback.
``LLMAnalyzer`` sends the structured evidence plus the retrieved verified incidents to an LLM
(Anthropic Messages API or any OpenAI-compatible endpoint, including local models) and asks for a
JSON diagnosis. Both return the same ``DiagnosisResult`` so the rest of the workflow is provider-agnostic.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

import httpx

from ..config import get_settings
from ..simulation.catalog import ACTIONS, CI_DEFECTS, FAULTS, module_for_file
from .embeddings import softmax

# Known cause taxonomy (what the platform has runbooks for).
CAUSES = {
    fid: {"title": f.title, "description": f.root_cause, "component": f.component, "module": f.module,
          "runbook": f.fix_actions[0]}
    for fid, f in FAULTS.items()
}
CAUSES["unknown"] = {"title": "Unknown cause", "description": "Evidence is insufficient to identify a cause.",
                     "component": "unknown", "module": "unknown", "runbook": "escalate"}

# (regex, weight) per cause. Shared symptoms deliberately overlap so diagnosis is not trivial.
LOG_RULES: dict[str, list[tuple[str, float]]] = {
    "invalid_config": [(r"invalid literal|validationerror|failed to parse", 2.2),
                       (r"exited with code|restarting", 0.7), (r"http 500", 0.5)],
    "dependency_failure": [(r"connection refused|connecterror|503 service|circuit .* probe", 2.2),
                           (r"upstream|retries", 0.7)],
    "app_exception": [(r"keyerror|typeerror|attributeerror|traceback|unhandled exception", 2.2),
                      (r"http 500", 0.5)],
    "slow_query": [(r"slow query|seq scan|duration \d+ ms statement", 2.2),
                   (r"database response time|took \d", 0.6)],
    "memory_leak": [(r"oomkilled|memoryerror|evictions=0|maxsize=none", 2.2),
                    (r"exited with code|restarted unexpectedly", 0.7)],
    "db_pool_exhaustion": [(r"queuepool|pool checked_out|pool saturation|waiting for db connection", 2.2),
                           (r"database response time|took \d", 0.6)],
    "cert_expired": [(r"certificate|x509|ssl|tls handshake", 2.2),
                     (r"upstream|retries", 0.7)],
}
METRIC_RULES: dict[str, dict[str, float]] = {
    "http_error_rate": {"invalid_config": 0.6, "app_exception": 0.6, "dependency_failure": 0.4, "cert_expired": 0.4},
    "http_p95_latency_ms": {"slow_query": 0.7, "db_pool_exhaustion": 0.7, "dependency_failure": 0.3,
                            "memory_leak": 0.3},
    "upstream_error_rate": {"dependency_failure": 1.0, "cert_expired": 1.0},
    "memory_mb": {"memory_leak": 1.5},
    "container_restarts": {"invalid_config": 0.8, "memory_leak": 0.8},
    "db_connections_in_use": {"db_pool_exhaustion": 0.6, "slow_query": 0.4},
    "cpu_usage": {"slow_query": 0.4},
}
SPAN_RULES: list[tuple[str, str, dict[str, float]]] = [
    ("error", r"settings", {"invalid_config": 1.5}),
    ("error", r"serializer|to_dict", {"app_exception": 1.5}),
    ("error", r"payments", {"dependency_failure": 1.2}),
    ("error", r"auth", {"cert_expired": 1.2}),
    ("slow", r"payments", {"dependency_failure": 0.8}),
    ("slow", r"db\.query", {"slow_query": 0.8, "db_pool_exhaustion": 0.5}),
    ("slow", r"db\.connect", {"db_pool_exhaustion": 1.2}),
    ("slow", r"cache", {"memory_leak": 0.8}),
]
RAG_WEIGHT = 4.0


@dataclass
class DiagnosisResult:
    root_cause_id: str
    root_cause: str
    affected_component: str
    confidence: float
    uncertainty: str
    reasoning: str
    proposed_action: str
    supporting_evidence: list[dict] = field(default_factory=list)
    alternatives: list[dict] = field(default_factory=list)
    provider: str = "heuristic"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DiagnosisContext:
    alert_name: str
    service: str
    summary: str
    evidence: list[dict]
    similar: list[dict]
    excluded_causes: list[str] = field(default_factory=list)
    use_rag: bool = True


class HeuristicAnalyzer:
    name = "heuristic"

    def diagnose(self, ctx: DiagnosisContext) -> DiagnosisResult:
        scores: dict[str, float] = defaultdict(float)
        support: dict[str, list[dict]] = defaultdict(list)

        def add(cause: str, w: float, kind: str, detail: str):
            if cause in ctx.excluded_causes:
                return
            scores[cause] += w
            if len(support[cause]) < 8:
                support[cause].append({"kind": kind, "detail": detail, "weight": round(w, 2)})

        for ev in ctx.evidence:
            c = ev["content"]
            if ev["kind"] == "log":
                for ln in c.get("lines", []):
                    if ln["level"] not in ("ERROR", "WARN"):
                        continue
                    msg = ln["message"].lower()
                    for cause, rules in LOG_RULES.items():
                        for pat, w in rules:
                            if re.search(pat, msg):
                                add(cause, w, "log", ln["message"])
                                break
            elif ev["kind"] == "metric":
                for a in c.get("anomalies", []):
                    for cause, w in METRIC_RULES.get(a["metric"], {}).items():
                        add(cause, w, "metric", f"{a['label']}: {a['baseline']} -> {a['current']}")
            elif ev["kind"] == "trace":
                for t in c.get("traces", []):
                    for sp in t["spans"]:
                        tags = ["error"] if sp.get("error") else []
                        if sp.get("depth") and sp["duration_ms"] > 300:
                            tags.append("slow")
                        for tag in tags:
                            for kind, pat, causes in SPAN_RULES:
                                if kind == tag and re.search(pat, sp["name"].lower()):
                                    for cause, w in causes.items():
                                        add(cause, w, "trace", f"{tag} span {sp['name']} in {t['root']}"
                                                                f" ({sp['duration_ms']} ms)")
            elif ev["kind"] == "deployment":
                deps = c.get("deployments", [])
                if deps and deps[0].get("minutes_before_alert", 1e9) <= 30:
                    d = deps[0]
                    add("app_exception", 1.0, "deployment",
                        f"{d['version']} deployed {d['minutes_before_alert']} min before the alert")
                    add("invalid_config", 0.4, "deployment", f"recent deployment {d['version']}")
            elif ev["kind"] == "commit":
                for cm in c.get("commits", [])[:2]:
                    for f in cm.get("files", []):
                        mod = module_for_file(f)
                        for cause, info in CAUSES.items():
                            if mod and info["module"] == mod:
                                add(cause, 0.6, "commit", f"{cm['sha']} '{cm['message']}' changed {f}")

        if ctx.use_rag and ctx.similar:
            # Best match per cause, sharpened relative to the overall best match, so several loosely similar
            # cases of one cause cannot outvote a close match of another.
            best: dict[str, dict] = {}
            for case in ctx.similar:
                c = case["root_cause_id"]
                if c in CAUSES and (c not in best or case["similarity"] > best[c]["similarity"]):
                    best[c] = case
            top_sim = max(c["similarity"] for c in best.values()) if best else 1.0
            for cause, case in best.items():
                sim = case["similarity"]
                add(cause, RAG_WEIGHT * sim ** 2 * (sim / top_sim) ** 6, "similar_incident",
                    f"Verified incident #{case.get('incident_id') or case['knowledge_id']} "
                    f"(similarity {sim:.2f}): {case['title']}")

        scores = {k: v for k, v in scores.items() if k not in ctx.excluded_causes}
        if not scores or max(scores.values()) < 1.0:
            return self._unknown(ctx)

        probs = softmax(scores, temperature=1.3)
        ranked = sorted(probs.items(), key=lambda kv: -kv[1])
        top, conf = ranked[0]
        info = CAUSES[top]
        action = self._action_for(top, ctx)

        unc = []
        if len(ranked) > 1 and ranked[1][1] > 0.15:
            unc.append(f"{CAUSES[ranked[1][0]]['title']} is also plausible (p={ranked[1][1]:.2f}).")
        if not any(s["kind"] == "log" and s["weight"] >= 2 for s in support[top]):
            unc.append("No log line directly names the cause; diagnosis relies on correlated symptoms.")
        if ctx.use_rag and not ctx.similar:
            unc.append("No similar verified incident was found in memory.")
        if ctx.excluded_causes:
            unc.append("Previously proposed cause(s) failed verification: "
                       + ", ".join(CAUSES.get(c, {"title": c})["title"] for c in ctx.excluded_causes) + ".")

        kinds = Counter(s["kind"] for s in support[top])
        reasoning = (
            f"The strongest explanation for {ctx.alert_name} on {ctx.service} is "
            f"'{info['title']}'. It is supported by "
            + ", ".join(f"{n} {k.replace('_', ' ')} signal{'s' if n > 1 else ''}" for k, n in kinds.items())
            + f". Recommended response: {ACTIONS[action].title.lower()}."
        )
        return DiagnosisResult(
            root_cause_id=top, root_cause=info["description"], affected_component=info["component"],
            confidence=round(conf, 3), uncertainty=" ".join(unc) or "Low: evidence is consistent.",
            reasoning=reasoning, proposed_action=action,
            supporting_evidence=sorted(support[top], key=lambda s: -s["weight"]),
            alternatives=[{"cause_id": c, "title": CAUSES[c]["title"], "probability": round(p, 3)}
                          for c, p in ranked[1:4]],
            provider=self.name,
        )

    @staticmethod
    def _action_for(cause: str, ctx: DiagnosisContext) -> str:
        if ctx.use_rag:
            past = Counter(c["action_type"] for c in ctx.similar
                           if c["root_cause_id"] == cause and c.get("outcome") == "resolved")
            if past:
                return past.most_common(1)[0][0]
        return CAUSES[cause]["runbook"]

    @staticmethod
    def _unknown(ctx: DiagnosisContext) -> DiagnosisResult:
        return DiagnosisResult(
            root_cause_id="unknown", root_cause=CAUSES["unknown"]["description"], affected_component="unknown",
            confidence=0.0, uncertainty="High: no rule or similar incident explains the evidence.",
            reasoning="Evidence did not match any known failure pattern; escalating to a human.",
            proposed_action="escalate", provider="heuristic",
        )


SYSTEM_PROMPT = """You are the Incident Agent of DevNexa, an agentic DevOps platform.
Diagnose production incidents ONLY from the evidence and verified past incidents you are given.
Never invent log lines or metrics. Express uncertainty honestly.
Respond with a single JSON object and nothing else, with keys:
root_cause_id (one of the allowed ids), root_cause (one or two sentences), affected_component,
confidence (0..1), uncertainty (what could make this wrong), reasoning (3-5 sentences citing evidence),
proposed_action (one of the allowed actions), supporting_evidence (list of {kind, detail}),
alternatives (list of {cause_id, probability})."""


class LLMAnalyzer:
    def __init__(self, provider: str):
        self.s = get_settings()
        self.provider = provider
        self.name = f"{provider}:{self.s.llm_model}"
        self.fallback = HeuristicAnalyzer()

    def _prompt(self, ctx: DiagnosisContext) -> str:
        ev = []
        for e in ctx.evidence:
            c = dict(e["content"])
            if e["kind"] == "metric":
                c = {"anomalies": c.get("anomalies", [])}
            if e["kind"] == "log":
                c = {"lines": [f"{ln['level']} {ln['message']}" for ln in c.get("lines", [])][-40:]}
            ev.append({"kind": e["kind"], "source": e["source"], "content": c})
        return json.dumps({
            "alert": ctx.alert_name, "service": ctx.service, "summary": ctx.summary,
            "allowed_root_cause_ids": {k: v["title"] for k, v in CAUSES.items() if k not in ctx.excluded_causes},
            "allowed_actions": {k: f"{a.title} (risk {a.risk})" for k, a in ACTIONS.items()},
            "causes_that_failed_verification": ctx.excluded_causes,
            "similar_verified_incidents": ctx.similar if ctx.use_rag else [],
            "evidence": ev,
        }, default=str)[:60000]

    def _complete(self, system: str, user: str) -> str:
        s = self.s
        with httpx.Client(timeout=s.llm_timeout) as client:
            if self.provider == "anthropic":
                r = client.post(
                    (s.llm_base_url or "https://api.anthropic.com") + "/v1/messages",
                    headers={"x-api-key": s.llm_api_key, "anthropic-version": "2023-06-01"},
                    json={"model": s.llm_model, "max_tokens": 1500, "system": system,
                          "messages": [{"role": "user", "content": user}]},
                )
                r.raise_for_status()
                return "".join(b.get("text", "") for b in r.json()["content"])
            r = client.post(
                (s.llm_base_url or "https://api.openai.com") + "/v1/chat/completions",
                headers={"Authorization": f"Bearer {s.llm_api_key}"} if s.llm_api_key else {},
                json={"model": s.llm_model, "temperature": 0,
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    @staticmethod
    def _parse(text: str) -> dict:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0) if m else text)

    def diagnose(self, ctx: DiagnosisContext) -> DiagnosisResult:
        try:
            d = self._parse(self._complete(SYSTEM_PROMPT, self._prompt(ctx)))
            cause = d.get("root_cause_id") if d.get("root_cause_id") in CAUSES else "unknown"
            action = d.get("proposed_action") if d.get("proposed_action") in ACTIONS else CAUSES[cause]["runbook"]
            return DiagnosisResult(
                root_cause_id=cause, root_cause=str(d.get("root_cause", CAUSES[cause]["description"])),
                affected_component=str(d.get("affected_component", CAUSES[cause]["component"])),
                confidence=max(0.0, min(1.0, float(d.get("confidence", 0.5)))),
                uncertainty=str(d.get("uncertainty", "")), reasoning=str(d.get("reasoning", "")),
                proposed_action=action,
                supporting_evidence=[{"kind": str(x.get("kind", "evidence")), "detail": str(x.get("detail", "")),
                                      "weight": None} for x in d.get("supporting_evidence", [])[:10]
                                     if isinstance(x, dict)],
                alternatives=[{"cause_id": a.get("cause_id"), "title": CAUSES.get(a.get("cause_id"), {}).get(
                    "title", a.get("cause_id")), "probability": a.get("probability")}
                    for a in d.get("alternatives", [])[:3] if isinstance(a, dict)],
                provider=self.name,
            )
        except Exception as e:  # network/parse problems fall back to the offline analyzer
            res = self.fallback.diagnose(ctx)
            res.provider = f"heuristic (fallback: {type(e).__name__})"
            return res

    def explain_ci_failure(self, stage: str, log: str, files: list[str]) -> dict:
        try:
            text = self._complete(
                "You are the DevNexa Build/Test Agent. Explain CI failures to developers concisely. "
                "Reply as JSON with keys summary and suggestion.",
                json.dumps({"stage": stage, "changed_files": files, "log": log[-6000:]}),
            )
            d = self._parse(text)
            return {"summary": d["summary"], "suggestion": d["suggestion"], "provider": self.name}
        except Exception:
            return explain_ci_failure_heuristic(stage, log, files)


def explain_ci_failure_heuristic(stage: str, log: str, files: list[str]) -> dict:
    for d in CI_DEFECTS.values():
        if d.log.splitlines()[0][:40] in log:
            return {"summary": d.explanation, "suggestion": d.suggestion, "provider": "heuristic"}
    patterns = [
        (r"SyntaxError", "The code does not compile (syntax error).", "Fix the syntax error at the reported line."),
        (r"ModuleNotFoundError: No module named '([\w.]+)'", "A module could not be imported: {0}.",
         "Add {0} to requirements or fix the import path."),
        (r"conflicting dependencies|ResolutionImpossible", "Dependency resolution failed.",
         "Align the conflicting version pins."),
        (r"FAILED ([\w/.:]+)", "Test {0} failed.", "Run the failing test locally and inspect the assertion."),
    ]
    for pat, summary, sugg in patterns:
        m = re.search(pat, log)
        if m:
            g = m.groups() or ("",)
            return {"summary": summary.format(*g), "suggestion": sugg.format(*g), "provider": "heuristic"}
    return {"summary": f"The {stage} stage failed. See the log excerpt for details.",
            "suggestion": "Inspect the last lines of the log.", "provider": "heuristic"}


def get_analyzer():
    p = get_settings().llm_provider
    if p in ("anthropic", "openai"):
        return LLMAnalyzer(p)
    return HeuristicAnalyzer()


def explain_ci_failure(stage: str, log: str, files: list[str]) -> dict:
    a = get_analyzer()
    if isinstance(a, LLMAnalyzer):
        return a.explain_ci_failure(stage, log, files)
    return explain_ci_failure_heuristic(stage, log, files)
