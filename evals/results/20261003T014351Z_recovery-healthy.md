# Eval results: recovery-healthy

- run: `20261003T014351Z`  llm: `fake`  scenario: `wrong_fix`  break: `none`  memory: `off`
- tasks: 5  trials/task: 1  runs: 5
- versions: code `61faa5ca5e9d`, prompts `9866d65c165c`, policy `7dbaaea94b09`, dataset `778a64b55f2c`, graders `2b09da2e678a`

## Overall

| metric | value (mean ± stderr) |
|---|---|
| Task success (outcome + root cause + policy) | 1.000 ± 0.000 (n=5) |
| Root cause correct | 1.000 ± 0.000 (n=5) |
| Tool-selection recall | 0.480 ± 0.018 (n=5) |
| Plan v1 sound and not revised | 0.500 ± 0.000 (n=5) |
| Recovered after a failed attempt | 1.000 ± 0.000 (n=5) |
| Needed a human (escalated) | 0.000 ± 0.000 (n=5) |
| LLM/heuristic rubric score | 0.880 ± 0.000 (n=5) |
| Agent steps per task | 19.000 ± 0.000 (n=5) |
| LLM calls per task | 25.000 ± 0.000 (n=5) |
| Cost per task (USD) | 0.030 ± 0.000 (n=5) |
| Latency per task (s) | 24.768 ± 0.613 (n=5) |

## By template

| template | success | root cause | recovery | steps | cost | latency |
|---|---|---|---|---|---|---|
| cache_disabled | 1.00 | 1.00 | 1.00 | 19.0 | $0.030 | 23s |
| missing_index | 1.00 | 1.00 | 1.00 | 19.0 | $0.030 | 25s |
| n_plus_one | 1.00 | 1.00 | 1.00 | 19.0 | $0.030 | 26s |
| pricing_per_item_query | 1.00 | 1.00 | 1.00 | 19.0 | $0.030 | 26s |
| serialization_quadratic | 1.00 | 1.00 | 1.00 | 19.0 | $0.030 | 24s |
