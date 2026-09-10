"""llm.* host functions — an LLM review/compose step for workflows.

llm.evaluate(task, data) asks the fast model to judge whether monitored data
changed in a way worth notifying about, and to compose the notification text.
Used by content-monitor workflows in place of a dumb count comparison + template.

Runs in a worker thread (via /trama/execute's asyncio.to_thread), so it drives
the async OpenAI call with its own asyncio.run — no running loop here.
"""
from __future__ import annotations

import asyncio
import json
import logging

from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider

logger = logging.getLogger(__name__)

_SYSTEM = (
    "Você revisa dados monitorados (notícias, preços, status) e decide se houve "
    "mudança RELEVANTE desde a última checagem. Se sim, escreve um aviso curto e "
    "claro em pt-BR (2 a 3 linhas; pode citar 1 ou 2 links). Se a mudança for "
    "irrelevante ou inexistente, não escreve nada. "
    'Responda SOMENTE um objeto JSON: {"decision": "notify" | "skip", "message": "<texto ou string vazia>"}.'
)

_MAX_DATA_CHARS = 6000


def _evaluate(task: str, data=None, previous=None, current=None) -> dict:
    """task: what to judge/compose. data|previous|current: the material to compare."""
    from ritesmith.config import get_settings
    from ritesmith.llm.openai_provider import OpenAIProvider

    settings = get_settings()
    payload: dict = {}
    if data is not None:
        payload["data"] = data
    if previous is not None:
        payload["previous"] = previous
    if current is not None:
        payload["current"] = current

    user = f"TAREFA: {task}\n\nMATERIAL:\n{json.dumps(payload, ensure_ascii=False)[:_MAX_DATA_CHARS]}"

    try:
        prov = OpenAIProvider(settings)
        raw, _stats = asyncio.run(
            prov._chat_with_retry(
                settings.llm_model_fast, _SYSTEM, user,
                max_tokens=500, temperature=0.2, method="llm.evaluate",
            )
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("llm.evaluate failed: %s", e)
        return {"decision": "skip", "message": "", "error": str(e)}

    try:
        out = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"decision": "skip", "message": ""}

    decision = out.get("decision")
    if decision not in ("notify", "skip"):
        decision = "notify" if (out.get("message") or "").strip() else "skip"
    return {"decision": decision, "message": (out.get("message") or "").strip()}


_INPUT_SCHEMA = {
    "type": "object",
    "required": ["task"],
    "properties": {
        "task": {"type": "string", "description": "O que julgar/compor, em pt-BR."},
        "data": {"description": "Material único a avaliar (qualquer forma)."},
        "previous": {"description": "Estado anterior, para comparar com `current`."},
        "current": {"description": "Estado atual."},
    },
}
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["notify", "skip"]},
        "message": {"type": "string"},
    },
}


class LlmProvider(ToolProvider):
    namespace = "llm"
    profile = "readonly_network"
    risk_level = "low"
    side_effects = "external_read"

    def is_available(self) -> bool:
        import os

        return bool(os.getenv("OPENAI_API_KEY"))

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        return {
            "llm.evaluate": HostFunctionDef(
                "llm.evaluate", "readonly_network", _evaluate,
                description=(
                    "Pergunta ao modelo rápido se dados monitorados mudaram de forma "
                    "relevante e compõe o aviso. Retorna {decision: notify|skip, message}. "
                    "Use em monitores de conteúdo no lugar de comparar contagem."
                ),
                input_schema=_INPUT_SCHEMA,
                output_schema=_OUTPUT_SCHEMA,
            ),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        return []
