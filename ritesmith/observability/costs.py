"""Historical text-token price snapshot used by the preserved baseline (USD / million).

Sources: https://developers.openai.com/api/docs/models/{model}
Estimates exclude tax, regional/priority premiums and account-specific discounts.
"""

PRICE_VERSION = "baseline-2026-10-08"

PRICES = {
    "gpt-5-mini": (0.25, 0.025, 2.0),
    "gpt-5": (1.25, 0.125, 10.0),
    "gpt-5.2": (1.75, 0.175, 14.0),
    "gpt-5.4-mini": (0.75, 0.075, 4.5),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
}


def estimated_cost(calls):
    total = 0.0
    for call in calls:
        if call.get("cost_known") is False or call.get("usage_known") is False:
            return None
        if call.get("cancelled"):
            return None  # billed usage was not received from the provider
        if call.get("error") and not {"prompt_tokens", "completion_tokens"} <= call.keys():
            return None  # failed transport/provider response has unknown billed usage
        prices = PRICES.get(call["model"])
        if prices is None:
            return None
        prompt = call.get("prompt_tokens", 0)
        cached = call.get("cached_tokens", 0)
        total += (
            (prompt - cached) * prices[0]
            + cached * prices[1]
            + call.get("completion_tokens", 0) * prices[2]
        ) / 1_000_000
    return total
