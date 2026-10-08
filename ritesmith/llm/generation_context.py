"""Local prompt routing and lossless contract selection; no model calls."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

PROMPT_VERSION = "generation-v3"


def normalized(text):
    return unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()


def proposal_kind(goal, input_schema=None, output_schema=None):
    text = normalized(goal)
    code = bool(
        input_schema
        or output_schema
        or re.search(r"\b(lua|luau|script|function|funcao|codigo)\b", text)
    )
    workflow = bool(
        re.search(
            r"\b(workflow|trama|monitor|watch|every|reminder|parallel|callback|schedule)\b|lembre|monitore|a cada|em paralelo|diariamente|semanal",
            text,
        )
    )
    return (
        "mixed" if code and workflow else "script" if code else "workflow" if workflow else "mixed"
    )


@dataclass
class PreparedCatalog:
    inventory: list[dict]
    contracts: list[dict]
    full_contracts: list[dict]
    version: str


def prepare_catalog(goal, capabilities, required=()):
    unique = {}
    for cap in capabilities:
        name = cap.get("capability_name") or cap.get("capability_id")
        if name:
            unique.setdefault(name, cap)
    inventory = [
        {"name": name, "description": (cap.get("description") or "").split(". ")[0]}
        for name, cap in sorted(unique.items())
    ]
    selected = set(required or ())
    words = set(re.findall(r"[a-z]{4,}", normalized(goal))) - {
        "with",
        "from",
        "that",
        "para",
        "como",
        "every",
        "workflow",
        "script",
    }
    confident = False
    for name, cap in unique.items():
        literal = bool(
            re.search(r"(?<![\w.])" + re.escape(name.lower()) + r"(?![\w.])", goal.lower())
        )
        overlap = words & set(
            re.findall(r"[a-z]{4,}", normalized(name + " " + (cap.get("description") or "")))
        )
        if literal or len(overlap) >= 2:
            selected.add(name)
            confident = True
    # State helpers are compiler dependencies of recurrence, not guessed business actions.
    if re.search(
        r"monitor|every|a cada|repeat|repet|sample|amostra|state|estado", normalized(goal)
    ):
        selected.update(name for name in unique if name.startswith("stat."))
    full = [cap for _, cap in sorted(unique.items())]
    contracts = [cap for name, cap in unique.items() if name in selected] if confident else full
    digest = hashlib.sha256(json.dumps(full, sort_keys=True, default=str).encode()).hexdigest()
    return PreparedCatalog(inventory, contracts, full, digest)
