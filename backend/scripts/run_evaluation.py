"""Re-run the fault-injection evaluation over several seeds and print the tables in docs/evaluation-report.md.

    cd backend && python scripts/run_evaluation.py [--seeds 2026 7 42 1234 99] [--repetitions 3]
"""

import argparse
import os
import statistics as st
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DEVNEXA_DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/eval.db")
os.environ.setdefault("DEVNEXA_SIM_STEP_DELAY", "0")

from app.evaluation.runner import run_experiment  # noqa: E402

VARIANTS = ["devnexa", "no_rag", "baseline"]
METRICS = [
    ("fault_occurrences", "Fault occurrences"), ("incidents", "Production incidents"),
    ("prevented_in_ci", "Faults stopped in CI"), ("repeated_incidents", "Repeated incidents"),
    ("failure_detection_s", "Failure detection time (s)"), ("rca_accuracy", "Root-cause accuracy"),
    ("mttr_s", "MTTR (s)"), ("human_steps_per_incident", "Human steps per incident"),
    ("auto_resolved", "Incidents resolved automatically"), ("avg_tests_per_run", "Tests run per pipeline"),
    ("avg_test_time_s", "Test time per pipeline (s)"), ("test_time_saved_pct", "Test time saved vs full suite (%)"),
    ("faults_caught_in_ci", "Faults caught by tests"), ("faults_per_test_hour", "Faults caught per test-hour"),
]


def mean_sd(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return "n/a"
    m, s = st.fmean(xs), (st.stdev(xs) if len(xs) > 1 else 0.0)
    return f"{m:.2f} ± {s:.2f}" if abs(m) < 10 else f"{m:.0f} ± {s:.0f}"


def avg(xs, digits=2):
    xs = [x for x in xs if x is not None]
    return f"{st.fmean(xs):.{digits}f}" if xs else "n/a"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--seeds", type=int, nargs="+", default=[2026, 7, 42, 1234, 99])
    p.add_argument("--repetitions", type=int, default=3)
    a = p.parse_args()
    runs = {s: run_experiment({"seed": s, "repetitions": a.repetitions}) for s in a.seeds}
    first = runs[a.seeds[0]]

    print("| Metric | " + " | ".join(first["variants"][v]["label"] for v in VARIANTS) + " |")
    print("| --- | --- | --- | --- |")
    for key, label in METRICS:
        print(f"| {label} | " + " | ".join(mean_sd([r["variants"][v]["metrics"][key] for r in runs.values()])
                                         for v in VARIANTS) + " |")

    print("\n| Seed | DevNexa | DevNexa without RAG | Conventional CI/CD |")
    print("| --- | --- | --- | --- |")
    for s, r in runs.items():
        cells = [f"{m['rca_accuracy']:.2f} / {m['mttr_s']:.0f} s" for m in (r["variants"][v]["metrics"] for v in VARIANTS)]
        print(f"| {s} | " + " | ".join(cells) + " |")

    print("\n| Scenario | RCA DevNexa | RCA no RAG | RCA baseline | MTTR DevNexa (s) | MTTR baseline (s) "
          "| Incidents DevNexa | Incidents baseline |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    rows: dict[str, dict] = {}
    for r in runs.values():
        for row in r["scenarios"]:
            agg = rows.setdefault(row["title"], {})
            for k, v in row.items():
                agg.setdefault(k, []).append(v)
    for title, agg in sorted(rows.items()):
        print(f"| {title} | {avg(agg['devnexa_rca'])} | {avg(agg['no_rag_rca'])} | {avg(agg['baseline_rca'])} "
              f"| {avg(agg['devnexa_mttr_s'], 0)} | {avg(agg['baseline_mttr_s'], 0)} "
              f"| {sum(x or 0 for x in agg['devnexa_incidents'])} | {sum(x or 0 for x in agg['baseline_incidents'])} |")


if __name__ == "__main__":
    main()
