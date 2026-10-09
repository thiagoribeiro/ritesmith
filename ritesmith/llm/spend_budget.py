"""Opt-in, durable call reservations shared by an entire bounded evaluation.

Only standard text requests are supported. UTF-8 byte length plus framing
headroom bounds input tokens conservatively; output reserves the full token cap,
including reasoning. Unknown usage retains its reservation and stops all calls.
"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path

from ritesmith.core.exceptions import LLMError
from ritesmith.observability.costs import PRICE_VERSION, PRICES

current_spend_budget: ContextVar[SpendBudget | None] = ContextVar("spend_budget", default=None)


class SpendBudget:
    def __init__(self, path, *, limit_usd="1", max_requests=12):
        self.path = Path(path)
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            # A process restart must never resubmit an unfinished paid call.
            if any(call["status"] == "reserved" for call in self.state["calls"]):
                self.state["blocked"] = "unfinished_call_usage_unknown"
        else:
            self.state = {
                "limit_usd": str(limit_usd),
                "max_requests": max_requests,
                "requests": [],
                "calls": [],
                "blocked": None,
                "price_version": PRICE_VERSION,
            }
        self.flush()

    def flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2))
        temporary.replace(self.path)

    def stop(self, reason):
        self.state["blocked"] = reason
        self.flush()

    def check(self):
        if self.state["blocked"]:
            raise LLMError(
                "Evaluation stopped: " + self.state["blocked"],
                details={
                    "category": "evaluation_budget",
                    "retryable": False,
                    "stats": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "usage_known": True,
                        "dispatched": False,
                    },
                },
            )

    def request(self, cell):
        self.check()
        if (
            cell in self.state["requests"]
            or len(self.state["requests"]) >= self.state["max_requests"]
        ):
            self.stop("request_limit_or_duplicate")
            self.check()
        self.state["requests"].append(cell)
        self.flush()

    def reserve(self, kwargs):
        self.check()
        model = kwargs["model"]
        if model not in PRICES or model not in ("gpt-5-mini", "gpt-5.4-mini", "gpt-4.1-nano"):
            self.stop("unaccounted_model_price")
            self.check()
        if kwargs.get("service_tier", "default") != "default" or "tools" in kwargs:
            self.stop("unaccounted_request_pricing")
            self.check()
        text = json.dumps(
            {key: value for key, value in kwargs.items() if key != "timeout"}, ensure_ascii=False
        )
        input_bound = len(text.encode()) + 4096
        output_bound = kwargs.get("max_completion_tokens", kwargs.get("max_tokens"))
        if not isinstance(output_bound, int) or output_bound <= 0:
            self.stop("unbounded_output")
            self.check()
        price = [Decimal(str(value)) for value in PRICES[model]]
        maximum = (input_bound * price[0] + output_bound * price[2]) / Decimal(1000000)
        occupied = sum(
            (Decimal(call.get("actual_usd", call["reserved_usd"])) for call in self.state["calls"]),
            Decimal(0),
        )
        if occupied + maximum > Decimal(self.state["limit_usd"]):
            self.stop("spending_limit")
            self.check()
        call = {
            "model": model,
            "status": "reserved",
            "reserved_usd": str(maximum),
            "input_token_bound": input_bound,
            "output_token_bound": output_bound,
            "request_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "cell": self.state["requests"][-1] if self.state["requests"] else None,
        }
        self.state["calls"].append(call)
        self.flush()
        return call

    def settle(self, call, response):
        if callable(getattr(response, "model_dump", None)):
            call["provider_response"] = response.model_dump(mode="json")
        if getattr(response, "service_tier", "default") != "default":
            self.unknown(call, "unaccounted_response_service_tier")
            return
        usage = response.usage
        if usage is None:
            self.unknown(call, "missing_usage")
            return
        prompt, completion = usage.prompt_tokens, usage.completion_tokens
        cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        if (
            prompt < 0
            or completion < 0
            or not 0 <= cached <= prompt
            or prompt > call["input_token_bound"]
            or completion > call["output_token_bound"]
        ):
            self.unknown(call, "usage_exceeds_reservation")
            return
        price = [Decimal(str(value)) for value in PRICES[call["model"]]]
        amount = (
            (prompt - cached) * price[0] + cached * price[1] + completion * price[2]
        ) / Decimal(1000000)
        call.update(
            status="settled",
            actual_usd=str(amount),
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_tokens=cached,
            resolved_model=response.model,
            response_content=response.choices[0].message.content,
            finish_reason=response.choices[0].finish_reason,
            refusal=getattr(response.choices[0].message, "refusal", None),
            reasoning_tokens=getattr(
                getattr(usage, "completion_tokens_details", None), "reasoning_tokens", 0
            )
            or 0,
        )
        self.flush()

    def unknown(self, call, reason):
        call["status"] = "unknown"
        self.stop(reason)

    async def dispatch(self, client, kwargs, method="unknown"):
        # Prevent an account's default processing tier from changing reserved prices.
        kwargs = {"service_tier": "default", **kwargs}
        call = self.reserve(kwargs)
        call["method"] = method
        self.flush()
        try:
            response = await client.chat.completions.create(**kwargs)
        except BaseException:
            self.unknown(call, "failed_or_cancelled_call_usage_unknown")
            raise
        self.settle(call, response)
        return response
