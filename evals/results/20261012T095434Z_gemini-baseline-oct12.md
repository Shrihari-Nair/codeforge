# Eval results: gemini-baseline-oct12

- run: `20261012T095434Z`  llm: `gemini`  scenario: `happy`  break: `none`  memory: `off`
- tasks: 30  trials/task: 1  runs: 30
- versions: code `02fe54dfda03`, prompts `e8919857ce1f`, policy `7dbaaea94b09`, dataset `778a64b55f2c`, graders `44f262cacf55`

## Overall

| metric | value (mean ± stderr) |
|---|---|
| Task success (outcome + root cause + policy) | 0.867 ± 0.062 (n=30) |
| Verified fix delivered (outcome + policy, root-cause commit not required) | 0.967 ± 0.033 (n=30) |
| Root cause correct | 0.883 ± 0.056 (n=30) |
| Tool-selection recall | 0.887 ± 0.025 (n=30) |
| Plan v1 sound and not revised | 0.967 ± 0.023 (n=30) |
| Recovered after a failed attempt | 0.500 ± 0.354 (n=2) |
| Needed a human (escalated) | 0.033 ± 0.033 (n=30) |
| LLM/heuristic rubric score | 0.963 ± 0.020 (n=30) |
| Agent steps per task | 22.367 ± 1.775 (n=30) |
| LLM calls per task | 25.600 ± 1.901 (n=30) |
| Cost per task (USD) | 0.065 ± 0.006 (n=30) |
| Latency per task (s) | 115.779 ± 8.783 (n=30) |

## By template

| template | success | root cause | recovery | steps | cost | latency |
|---|---|---|---|---|---|---|
| audit_every_request | 1.00 | 1.00 | n/a | 17.6 | $0.049 | 93s |
| cache_disabled | 0.80 | 0.80 | 0.00 | 22.0 | $0.061 | 113s |
| missing_index | 0.60 | 0.70 | n/a | 19.4 | $0.052 | 96s |
| n_plus_one | 0.80 | 0.80 | n/a | 20.2 | $0.057 | 104s |
| pricing_per_item_query | 1.00 | 1.00 | 1.00 | 28.6 | $0.089 | 152s |
| serialization_quadratic | 1.00 | 1.00 | n/a | 26.4 | $0.084 | 138s |

## vs previous run

| metric | previous | current | delta | |
|---|---|---|---|---|
| Task success (outcome + root cause + policy) | 0.500 | 0.867 | +0.367 |  |
| Root cause correct | 0.600 | 0.883 | +0.283 |  |
| Tool-selection recall | 0.808 | 0.887 | +0.078 |  |
| Plan v1 sound and not revised | 0.767 | 0.967 | +0.200 |  |
| Recovered after a failed attempt | 0.222 | 0.500 | +0.278 |  |
| Needed a human (escalated) | 0.133 | 0.033 | -0.100 |  |
| LLM/heuristic rubric score | 0.000 | 0.963 | +0.963 |  |
| Agent steps per task | 32.067 | 22.367 | -9.700 |  |
| LLM calls per task | 35.733 | 25.600 | -10.133 |  |
| Cost per task (USD) | 0.093 | 0.065 | -0.027 |  |
| Latency per task (s) | 321.711 | 115.779 | -205.932 |  |

## Failures

- `latency-cache_disabled-05` (trial 0, escalated): escalated to a human after 3 failed attempts (requirement_not_met: judge: patch does not address the diagnosed root cause: The root cause was not identified, making it difficult to definitively link t
- `latency-missing_index-02` (trial 0, completed): root_cause_match: blamed cart_items_table @ 0982c3015c; truth is checkout/db.py
- `latency-missing_index-04` (trial 0, completed): root_cause_match: right file (checkout/db.py) but wrong commit c1377978e6
- `latency-n_plus_one-04` (trial 0, completed): root_cause_match: blamed checkout/service.py @ f3f14195ae; truth is checkout/catalog.py; llm_rubric: llm: {'identifies_root_cause': 1, 'evidence_quality': 4, 'fix_targets_cause': 2, 'verification_repo
