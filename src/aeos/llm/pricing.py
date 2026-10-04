"""Approximate USD cost per call (estimates for budgeting, not billing)."""

# (input $/1M tokens, output $/1M tokens)
PRICES: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-3.1-pro-preview": (2.00, 12.00),  # estimates
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-3.5-flash": (0.50, 3.00),
}
DEFAULT_PRICE = (1.00, 8.00)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    inp, out = PRICES.get(model, DEFAULT_PRICE)
    return (input_tokens * inp + output_tokens * out) / 1_000_000
