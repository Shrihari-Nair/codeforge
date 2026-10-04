# Eval results: live-gemini-full

- run: `20261003T024031Z`  llm: `gemini`  scenario: `happy`  break: `none`  memory: `off`
- tasks: 30  trials/task: 1  runs: 30
- versions: code `61faa5ca5e9d`, prompts `9866d65c165c`, policy `7dbaaea94b09`, dataset `778a64b55f2c`, graders `2b09da2e678a`

## Overall

| metric | value (mean ± stderr) |
|---|---|
| Task success (outcome + root cause + policy) | 0.500 ± 0.091 (n=30) |
| Root cause correct | 0.600 ± 0.083 (n=30) |
| Tool-selection recall | 0.808 ± 0.054 (n=30) |
| Plan v1 sound and not revised | 0.767 ± 0.061 (n=30) |
| Recovered after a failed attempt | 0.222 ± 0.139 (n=9) |
| Needed a human (escalated) | 0.133 ± 0.062 (n=30) |
| LLM/heuristic rubric score | 0.000 ± 0.000 (n=30) |
| Agent steps per task | 32.067 ± 3.682 (n=30) |
| LLM calls per task | 35.733 ± 4.029 (n=30) |
| Cost per task (USD) | 0.093 ± 0.012 (n=30) |
| Latency per task (s) | 321.711 ± 85.917 (n=30) |

## By template

| template | success | root cause | recovery | steps | cost | latency |
|---|---|---|---|---|---|---|
| audit_every_request | 0.80 | 0.90 | n/a | 19.4 | $0.047 | 92s |
| cache_disabled | 0.20 | 0.40 | 0.00 | 30.0 | $0.097 | 507s |
| missing_index | 0.40 | 0.60 | 0.25 | 54.6 | $0.160 | 230s |
| n_plus_one | 0.40 | 0.40 | 0.00 | 30.4 | $0.080 | 133s |
| pricing_per_item_query | 0.60 | 0.70 | 1.00 | 29.0 | $0.082 | 481s |
| serialization_quadratic | 0.60 | 0.60 | 0.00 | 29.0 | $0.090 | 487s |

## Failures

- `latency-audit_every_request-04` (trial 0, completed): root_cause_match: right file (checkout/audit.py) but wrong commit f3f14195ae; llm_rubric: judge unavailable: gemini error 404: This model models/gemini-2.5-pro is no longer available to new users. Ple
- `latency-cache_disabled-01` (trial 0, completed): root_cause_match: right file (checkout/cache.py) but wrong commit 8967402cff; planning: sound plan; replanned; llm_rubric: judge unavailable: gemini error 404: This model models/gemini-2.5-pro is no l
- `latency-cache_disabled-02` (trial 0, escalated): analytics malformed: model produced no tool call 3 times (finish_reason='blocked'); needs human attention
- `latency-cache_disabled-03` (trial 0, completed): root_cause_match: right file (checkout/cache.py) but wrong commit 6c09b86b6d; llm_rubric: judge unavailable: gemini error 404: This model models/gemini-2.5-pro is no longer available to new users. Ple
- `latency-cache_disabled-05` (trial 0, crashed): TimeoutError: 
- `latency-missing_index-01` (trial 0, escalated): escalated to a human after 3 failed attempts (perf_regression: p95 +706% vs baseline (36.42 ms vs 4.52 ms))
- `latency-missing_index-04` (trial 0, completed): root_cause_match: blamed checkout/service.py @ f3f14195ae; truth is checkout/db.py; llm_rubric: judge unavailable: gemini error 404: This model models/gemini-2.5-pro is no longer available to new user
- `latency-missing_index-05` (trial 0, escalated): escalated to a human after 3 failed attempts (requirement_not_met: judge: patch does not address the diagnosed root cause: The patch does not restore the missing database index.)
- `latency-n_plus_one-01` (trial 0, completed): root_cause_match: blamed checkout-service @ 0e285675b4; truth is checkout/catalog.py; llm_rubric: judge unavailable: gemini error 404: This model models/gemini-2.5-pro is no longer available to new us
- `latency-n_plus_one-03` (trial 0, completed): root_cause_match: blamed checkout/service.py @ f3f14195ae; truth is checkout/catalog.py; planning: sound plan; replanned; llm_rubric: judge unavailable: gemini error 404: This model models/gemini-2.5-
- `latency-n_plus_one-05` (trial 0, completed): root_cause_match: blamed checkout/cache.py @ 0e285675b4; truth is checkout/catalog.py; llm_rubric: judge unavailable: gemini error 404: This model models/gemini-2.5-pro is no longer available to new u
- `latency-pricing_per_item_query-02` (trial 0, crashed): TimeoutError: 
- `latency-pricing_per_item_query-03` (trial 0, completed): root_cause_match: right file (checkout/pricing.py) but wrong commit da8a6a1556; llm_rubric: judge unavailable: gemini error 404: This model models/gemini-2.5-pro is no longer available to new users. P
- `latency-serialization_quadratic-04` (trial 0, crashed): TimeoutError: 
- `latency-serialization_quadratic-05` (trial 0, escalated): escalated to a human after 3 failed attempts (verifier_error: benchmark_failed: 
python: can't open file '/workspace/benchmark.py': [Errno 2] No such file or directory
)
