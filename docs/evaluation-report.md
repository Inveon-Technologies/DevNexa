# DevNexa evaluation report

This report gives the results of the controlled fault-injection evaluation planned in section 8.5 of the
synopsis. It compares three variants on the same sequence of faults:

* **DevNexa**: prioritised tests, LLM+RAG diagnosis, graded autonomy and the Feedback Engine.
* **DevNexa without RAG**: the same platform with similar-incident retrieval switched off (an ablation).
* **Conventional CI/CD**: full test suite, no Feedback Engine, and manual monitoring, diagnosis and recovery.

The results are from a research prototype on a simulated environment. They are not production claims.

## Method

* **Faults.** There are seven controlled scenarios: invalid configuration, a failing downstream dependency, an
  application exception after deployment, a slow query from a missing index, memory pressure from an unbounded
  cache, database connection pool exhaustion, and an expired TLS client certificate. Each scenario occurs 3 times,
  so there are 21 fault occurrences per run, interleaved with clean commits. Some faults arrive through a
  commit and pipeline; others appear at runtime.
* **Runs.** Every variant replays the same randomised sequence, on a fresh database and simulated environment, for
  5 seeds (2026, 7, 42, 1234, 99). Tables show the mean ± standard deviation over the seeds.
* **What is measured, and what is assumed.**
  * The two DevNexa variants are measured by running the real agent workflows (the LangGraph CI/CD and incident
    graphs, the policy layer and the Feedback Engine) on a simulated clock.
  * Diagnosis used the offline heuristic analyzer (`DEVNEXA_LLM_PROVIDER=heuristic`), not a hosted LLM.
  * 60% of the distinctive log lines are dropped at random (`observability_gap`), so diagnosis works from
    incomplete evidence.
  * Human approval takes 2 to 10 minutes.
  * The conventional baseline's human work cannot be executed, so it is modelled with these assumptions:
    * detection takes 5 to 20 minutes;
    * diagnosis takes 15 to 45 minutes, and the first diagnosis is correct 70% of the time;
    * a fix takes 5 to 15 minutes;
    * manual verification takes 5 to 10 minutes;
    * each incident needs 4 manual steps.
  * All of these values are in `DEFAULT_CONFIG` in `backend/app/evaluation/runner.py`.
* **Reproduce.** Run `cd backend && python scripts/run_evaluation.py` (about 10 seconds). It prints the tables
  below. You can also start single runs from the Evaluation page or with `POST /api/experiments`.

## Results

| Metric | DevNexa (LLM+RAG + feedback) | DevNexa without RAG | Conventional CI/CD |
| --- | --- | --- | --- |
| Fault occurrences | 21 ± 0 | 21 ± 0 | 21 ± 0 |
| Production incidents | 15 ± 0 | 15 ± 0 | 21 ± 0 |
| Faults stopped in CI | 6.00 ± 0.00 | 6.00 ± 0.00 | 0.00 ± 0.00 |
| Repeated incidents | 8.00 ± 0.00 | 8.00 ± 0.00 | 14 ± 0 |
| Failure detection time (s) | 75 ± 0 | 75 ± 0 | 791 ± 30 |
| Root-cause accuracy | 0.95 ± 0.07 | 0.96 ± 0.04 | 0.70 ± 0.09 |
| MTTR (s) | 481 ± 69 | 477 ± 54 | 3439 ± 178 |
| Human steps per incident | 0.80 ± 0.10 | 0.81 ± 0.06 | 4.61 ± 0.19 |
| Incidents resolved automatically | 3.60 ± 0.55 | 3.40 ± 0.55 | 0.00 ± 0.00 |
| Tests run per pipeline | 19 ± 0 | 19 ± 0 | 30 ± 0 |
| Test time per pipeline (s) | 25 ± 1 | 25 ± 1 | 49 ± 0 |
| Test time saved vs full suite (%) | 55 ± 1 | 55 ± 1 | 0.00 ± 0.00 |
| Faults caught by tests | 6.00 ± 0.00 | 6.00 ± 0.00 | 0.00 ± 0.00 |
| Faults caught per test-hour | 29 ± 1 | 29 ± 1 | 0.00 ± 0.00 |

Root-cause accuracy and MTTR for each seed:

| Seed | DevNexa | DevNexa without RAG | Conventional CI/CD |
| --- | --- | --- | --- |
| 2026 | 1.00 / 461 s | 0.93 / 525 s | 0.67 / 3352 s |
| 7 | 0.87 / 580 s | 0.93 / 529 s | 0.81 / 3246 s |
| 42 | 1.00 / 446 s | 1.00 / 402 s | 0.76 / 3389 s |
| 1234 | 0.87 / 516 s | 0.93 / 479 s | 0.57 / 3715 s |
| 99 | 1.00 / 400 s | 1.00 / 450 s | 0.67 / 3495 s |

Each scenario, over all 5 seeds:

| Scenario | RCA DevNexa | RCA no RAG | RCA baseline | MTTR DevNexa (s) | MTTR baseline (s) | Incidents DevNexa | Incidents baseline |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Application exception after deployment | 1.00 | 1.00 | 0.87 | 578 | 2984 | 5 | 15 |
| Database connection pool exhaustion | 1.00 | 0.93 | 0.73 | 534 | 3554 | 15 | 15 |
| Expired TLS client certificate | 0.93 | 0.93 | 0.47 | 617 | 3699 | 15 | 15 |
| Failing downstream dependency | 0.87 | 0.93 | 0.60 | 234 | 3665 | 15 | 15 |
| Invalid configuration value | 0.93 | 1.00 | 0.60 | 532 | 3571 | 15 | 15 |
| Memory pressure from unbounded cache | 1.00 | 1.00 | 0.73 | 138 | 3398 | 5 | 15 |
| Slow query from missing index | 1.00 | 1.00 | 0.87 | 737 | 3205 | 5 | 15 |

## Findings

1. **The Feedback Engine prevented repeat incidents.** Three scenarios arrive through a commit: the application
   exception, the memory leak and the slow query. After the first verified incident of each, the regression test
   added by the Feedback Engine failed the next faulty build. Six of the 21 fault occurrences were stopped in CI,
   and repeated incidents fell from 14 to 8. The other 8 repeats are runtime faults, such as an expired
   certificate or a failing dependency, which no test can catch.
2. **Prioritised testing roughly halved test time.** 19 of 30 tests ran (55% less test time). The baseline's
   full suite caught none of the faulty builds, because the regression tests that catch them only exist once the
   Feedback Engine has learned from a verified incident.
3. **RAG did not measurably improve root-cause accuracy here.** DevNexa scored 0.95 ± 0.07 with RAG and
   0.96 ± 0.04 without it. The seeds disagree: RAG helped on seeds 2026 and 99 and hurt on 7 and 1234. With only 7
   distinct causes and evidence rules tuned to them, the offline analyzer already separates most cases without
   retrieval. A fair test of RAG needs a real LLM (`DEVNEXA_LLM_PROVIDER=anthropic` or `openai`), more varied
   faults and a larger incident history.
4. **Comparisons with the baseline depend on its assumptions.** The detection time, MTTR and human-step gaps
   (75 s against 791 s, 481 s against 3439 s, and 0.8 against 4.6 steps) come mostly from the assumed human
   timings. Read them as "an automated loop compared with these assumed manual timings", not as a measured
   speed-up. To measure them, time real engineers handling the same injected faults and put those numbers in
   `config.baseline`.

## Threats to validity

* The environment is simulated. Fault effects, metrics and remediation outcomes come from a model of the
  orders-service, not from production traffic.
* The heuristic analyzer was designed alongside the fault catalogue, so its accuracy is optimistic for unseen
  faults.
* There are seven scenarios with three repetitions each, which is a small sample. The standard deviations above
  reflect only the randomness between seeds.
* The live stack (Prometheus, Loki, Jaeger and real Docker deployments) is not part of these runs. It can be
  evaluated by injecting the same faults through the dashboard's Fault Lab in live mode and timing each incident.
