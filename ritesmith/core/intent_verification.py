"""Does the built plan do what the user asked? Compared against the user's own words.

The readback (``ritesmith.workflows.readback``) says what the workflows will do. This
check asks a model to compare it with the user's literal messages
(``context.user_request``), not with the caller's paraphrase (``intent``). The
paraphrase is where the 2026-11-21 error started: a dated 6-month reminder was
written up with an arrival contract.

Modes (``RITESMITH_INTENT_VERIFICATION``):
- ``off`` (default): not run.
- ``shadow``: run and record the verdict on the plan and in the audit log;
  never changes the outcome.
- ``enforce``: a ``mismatch`` blocks the plan with the reason. ``unsure`` passes, and
  the caller is expected to ask the user.

Fails open: a model or parse error yields ``verdict="error"`` and changes nothing.
"""

from __future__ import annotations

import json
import logging
import time

from ritesmith.config import Settings

log = logging.getLogger(__name__)

VERSION = "intent-verify-v1"

_SYSTEM = """\
You check whether an automation that was ALREADY BUILT does what the USER asked.

You get:
- USER: the user's own recent chat messages, oldest first (Portuguese). This is the source
  of truth.
- PARAPHRASE: how an assistant summarised the request. It may be WRONG. Use it only to
  resolve references (e.g. a date it computed from "hoje + 6 meses"), never as the
  requirement itself.
- BUILT: a deterministic description of what the workflow will actually do (effects and
  their times or trigger conditions). This is ground truth about the artifact.
- NOW: the current local date and time.

Extract the requirement from USER, then compare it with BUILT field by field:
- trigger: "time" (a date/time or delay), "arrival", "departure", "condition" (reading
  crosses a limit, price moves…), "now", or "other";
- when: dates/times or delay (resolve relative dates against NOW);
- repeat: recurrence and how long it lasts ("" when none);
- action: what is sent or done, to which device or person, and on which channel.

verdict:
- "mismatch": any field contradicts the request. Examples: wrong trigger type, a
  different date or time, recurrence asked but missing (or the reverse), a different
  device, person or channel, or an extra side effect the user did not ask for.
- "unsure": the user's words are ambiguous, or BUILT lacks the information to decide.
- "match": every field the user specified is honoured. Reasonable defaults the user
  left open (a polling interval, a stop deadline) are fine.

BUILT cannot express some details: whether calls run in parallel, what a message
template or report will contain beyond its text, or which fields one reading returns
(a vacuum state may include its battery). Never call those a mismatch; use "unsure" only
when such a detail is the core of the request. A difference of a minute or two, or one
check more or less at the edge of a window, is not a mismatch.

Respond with JSON only:
{"requested": {"trigger": "...", "when": "...", "repeat": "...", "action": "..."},
 "built": {"trigger": "...", "when": "...", "repeat": "...", "action": "..."},
 "verdict": "match" | "mismatch" | "unsure",
 "reason": "<one short sentence in Portuguese, saying what differs>"}"""


def _user_text(user_request) -> str:
    if isinstance(user_request, list):
        return "\n".join(f"- {str(m)[:800]}" for m in user_request[-6:])
    return f"- {str(user_request)[:2400]}"


async def verify_intent(
    client,
    settings: Settings,
    *,
    user_request,
    intent: str,
    readback: str,
    now_local: str,
) -> dict:
    """Return {verdict, reason, requested, built, model, duration_s, tokens}."""
    model = settings.intent_verification_model or settings.llm_workflow_model or settings.llm_model
    effort = settings.intent_verification_effort
    user = (
        f"NOW: {now_local}\n\nUSER:\n{_user_text(user_request)}\n\n"
        f"PARAPHRASE: {intent[:1500]}\n\nBUILT:\n{readback[:4000]}\n\n"
        # json_object on the Responses API requires the word in the input itself.
        "Answer with the JSON object described in the instructions."
    )
    started = time.perf_counter()
    result: dict = {"verdict": "error", "reason": "", "model": model, "version": VERSION}
    try:
        kwargs: dict = {
            "model": model,
            "instructions": _SYSTEM,
            "input": [{"role": "user", "content": user}],
            "max_output_tokens": 1500,
            "text": {"format": {"type": "json_object"}},
            "store": False,
            "timeout": settings.intent_verification_timeout_seconds,
        }
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        response = await client.responses.create(**kwargs)
        data = json.loads(response.output_text or "{}")
        verdict = data.get("verdict")
        result.update(
            verdict=verdict if verdict in ("match", "mismatch", "unsure") else "error",
            reason=str(data.get("reason") or "")[:500],
            requested=data.get("requested"),
            built=data.get("built"),
        )
        usage = getattr(response, "usage", None)
        result["tokens"] = {
            "input": getattr(usage, "input_tokens", 0) or 0,
            "output": getattr(usage, "output_tokens", 0) or 0,
        }
    except Exception as exc:  # fail open: verification never breaks plan creation
        log.warning("intent verification failed: %s", exc)
        result["reason"] = f"verification error: {type(exc).__name__}"
    result["duration_s"] = round(time.perf_counter() - started, 3)
    return result
