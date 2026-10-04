# Eval results: recovery-BROKEN

- run: `20261003T014428Z`  llm: `fake`  scenario: `wrong_fix`  break: `retry`  memory: `off`
- tasks: 5  trials/task: 1  runs: 5
- versions: code `61faa5ca5e9d`, prompts `9866d65c165c`, policy `7dbaaea94b09`, dataset `778a64b55f2c`, graders `2b09da2e678a`

## Overall

| metric | value (mean ± stderr) |
|---|---|
| Task success (outcome + root cause + policy) | 0.000 ± 0.000 (n=5) |
| Root cause correct | 0.000 ± 0.000 (n=5) |
| Tool-selection recall | 0.240 ± 0.009 (n=5) |
| Plan v1 sound and not revised | 1.000 ± 0.000 (n=5) |
| Recovered after a failed attempt | 0.000 ± 0.000 (n=5) |
| Needed a human (escalated) | 1.000 ± 0.000 (n=5) |
| LLM/heuristic rubric score | 0.520 ± 0.000 (n=5) |
| Agent steps per task | 10.000 ± 0.000 (n=5) |
| LLM calls per task | 13.000 ± 0.000 (n=5) |
| Cost per task (USD) | 0.016 ± 0.000 (n=5) |
| Latency per task (s) | 16.866 ± 0.659 (n=5) |

## By template

| template | success | root cause | recovery | steps | cost | latency |
|---|---|---|---|---|---|---|
| cache_disabled | 0.00 | 0.00 | 0.00 | 10.0 | $0.016 | 15s |
| missing_index | 0.00 | 0.00 | 0.00 | 10.0 | $0.016 | 18s |
| n_plus_one | 0.00 | 0.00 | 0.00 | 10.0 | $0.016 | 18s |
| pricing_per_item_query | 0.00 | 0.00 | 0.00 | 10.0 | $0.016 | 18s |
| serialization_quadratic | 0.00 | 0.00 | 0.00 | 10.0 | $0.016 | 16s |

## vs previous run

| metric | previous | current | delta | |
|---|---|---|---|---|
| Task success (outcome + root cause + policy) | 1.000 | 0.000 | -1.000 | REGRESSED |
| Root cause correct | 1.000 | 0.000 | -1.000 | REGRESSED |
| Tool-selection recall | 0.480 | 0.240 | -0.240 | REGRESSED |
| Plan v1 sound and not revised | 0.500 | 1.000 | +0.500 |  |
| Recovered after a failed attempt | 1.000 | 0.000 | -1.000 | REGRESSED |
| Needed a human (escalated) | 0.000 | 1.000 | +1.000 | REGRESSED |
| LLM/heuristic rubric score | 0.880 | 0.520 | -0.360 | REGRESSED |
| Agent steps per task | 19.000 | 10.000 | -9.000 |  |
| LLM calls per task | 25.000 | 13.000 | -12.000 |  |
| Cost per task (USD) | 0.030 | 0.016 | -0.014 |  |
| Latency per task (s) | 24.768 | 16.866 | -7.902 |  |

## Failures

- `latency-cache_disabled-01` (trial 0, escalated): escalated to a human after 1 failed attempts (perf_regression: p95 +86% vs baseline (8.42 ms vs 4.52 ms))
- `latency-missing_index-01` (trial 0, escalated): escalated to a human after 1 failed attempts (perf_regression: p95 +706% vs baseline (36.42 ms vs 4.52 ms))
- `latency-n_plus_one-01` (trial 0, escalated): escalated to a human after 1 failed attempts (perf_regression: p95 +518% vs baseline (27.92 ms vs 4.52 ms))
- `latency-pricing_per_item_query-01` (trial 0, escalated): escalated to a human after 1 failed attempts (perf_regression: p95 +544% vs baseline (29.12 ms vs 4.52 ms))
- `latency-serialization_quadratic-01` (trial 0, escalated): escalated to a human after 1 failed attempts (perf_regression: p95 +256% vs baseline (16.1 ms vs 4.52 ms))
