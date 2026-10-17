"""Model pricing table and financial cost calculator for Tether."""

from typing import Dict, Tuple

# Pricing in USD per 1,000,000 tokens: (prompt_cost_per_m, completion_cost_per_m)
MODEL_PRICE_TABLE: Dict[str, Tuple[float, float]] = {
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "openai/gpt-4o": (2.50, 10.00),
    "openai/gpt-4o-mini": (0.15, 0.60),
    # Anthropic
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    "anthropic/claude-3-5-sonnet-20241022": (3.00, 15.00),
    "anthropic/claude-3-5-haiku-20241022": (0.80, 4.00),
    # Google Gemini
    "gemini/gemini-1.5-pro": (1.25, 5.00),
    "gemini/gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    # DeepSeek
    "deepseek/deepseek-chat": (0.14, 0.28),
    "deepseek/deepseek-reasoner": (0.55, 2.19),
}

DEFAULT_FALLBACK_PRICE_PER_M = (1.00, 3.00)


def calculate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Calculate the estimated financial cost in USD for a given model invocation.

    Args:
        model: Model identifier (e.g., 'gpt-4o-mini', 'claude-3-5-haiku-20241022')
        tokens_in: Prompt/input token count
        tokens_out: Completion/output token count

    Returns:
        Total cost in USD rounded to 6 decimal places.
    """
    normalized_model = model.lower().strip()

    # Look up direct match or substring match
    pricing = MODEL_PRICE_TABLE.get(normalized_model)
    if not pricing:
        for known_model, rates in MODEL_PRICE_TABLE.items():
            if known_model in normalized_model or normalized_model in known_model:
                pricing = rates
                break

    if not pricing:
        pricing = DEFAULT_FALLBACK_PRICE_PER_M

    prompt_rate, completion_rate = pricing
    prompt_cost = (tokens_in / 1_000_000) * prompt_rate
    completion_cost = (tokens_out / 1_000_000) * completion_rate

    return round(prompt_cost + completion_cost, 6)
