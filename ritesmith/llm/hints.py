"""Deployment-specific examples for the generation prompts.

The prompts name example devices and people so the model learns the shape of a
good answer ("Qual ar-condicionado? quarto, escritório, sala"). Real names belong
to the deployment, not to this repo. Sources, later ones winning per key:

1. neutral defaults (below);
2. the ``home`` provider's ``prompt_hints()`` (e.g. a private plugin);
3. a JSON file named by ``RITESMITH_PROMPT_HINTS``.

Resolved once per process: prompts must stay byte-identical across calls so the
provider's prompt cache keeps hitting.
"""

from __future__ import annotations

import json
import logging
import os
from functools import cache

log = logging.getLogger(__name__)

DEFAULTS: dict[str, str] = {
    # a refusal that lists the matching devices when the request is ambiguous
    "ambiguity_example": "Qual ar-condicionado? quarto, escritório, sala",
    # person targets from list_devices
    "person_examples": "alice, bob",
}


@cache
def hints() -> dict[str, str]:
    merged = dict(DEFAULTS)
    try:
        from ritesmith.runtime.providers import provider_for

        home = provider_for("home")
        if home is not None:
            merged.update(
                {k: v for k, v in home.prompt_hints().items() if isinstance(v, str) and v}
            )
    except Exception:
        log.exception("home provider prompt_hints() failed; using defaults")
    path = os.environ.get("RITESMITH_PROMPT_HINTS")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            merged.update({k: v for k, v in data.items() if isinstance(v, str) and v})
        except Exception:
            log.exception("RITESMITH_PROMPT_HINTS=%s could not be read; ignored", path)
    return merged


def fill(text: str) -> str:
    """Replace ``__HINT_<key>__`` markers (braces stay free for JSON examples)."""
    for key, value in hints().items():
        text = text.replace(f"__HINT_{key}__", value)
    return text
