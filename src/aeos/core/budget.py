"""Live budget tracking shared by all agents of a task (steps, tokens, dollars, wall clock)."""

import time

from aeos.core.state import Budget
from aeos.llm.base import Usage
from aeos.llm.pricing import estimate_cost


class BudgetTracker:
    def __init__(self, budget: Budget | None = None) -> None:
        self.budget = (budget or Budget()).model_copy(deep=True)
        if self.budget.started_at is None:
            self.budget.started_at = time.time()

    def add_llm(self, usage: Usage, model: str) -> None:
        b = self.budget
        b.llm_calls += 1
        b.tokens_used += usage.input_tokens + usage.output_tokens
        b.cost_usd += estimate_cost(model, usage.input_tokens, usage.output_tokens)

    def add_step(self) -> None:
        self.budget.steps_used += 1

    def add_tool_call(self) -> None:
        self.budget.tool_calls += 1

    def exhausted(self) -> str | None:
        return self.budget.exhausted()
