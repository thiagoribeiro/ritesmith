"""What a workflow will actually do, described from its graph — never from the intent.

Confirmations used to echo the request ("Automação ativa: <intent>"), so a misrouted
plan was confirmed as if it did what was asked. On 2026-11-21 "remind me on 15/05/2027
and every 6 months" became a next-arrival reminder, and the user was told otherwise.
The readback walks the definition instead. It reports each effect (a message, a device
action, a report) and when it happens: at a wall-clock time when only sleeps lead to it,
or under which observed conditions when a polling loop does.

Deterministic, no model call. It is the text a user sees and the text an intent
verifier compares with the user's own words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

_MAX_PATHS = 64
_MAX_DEPTH = 400
_OUT = re.compile(r"^nodes\.([A-Za-z0-9_-]+)\.response\.body\.output\.?(.*)$")
_TEMPLATE = re.compile(r"\{\{.*?\}\}")
# home.execute capabilities that change something; everything else (ac.read,
# sensor.battery, vacuum.state, presence.read…) is a reading.
_ACTION_VERBS = {
    "on",
    "off",
    "toggle",
    "press",
    "start",
    "stop",
    "pause",
    "resume",
    "home",
    "dock",
    "locate",
    "open",
    "close",
    "arm",
    "disarm",
    "play",
    "send",
    "clean",
    "trigger",
    "away",
}
_UNROLLED = re.compile(r"__rs_\d+$")  # semantic repeat copies: fetch__rs_3
_OPS = {"<": "<", "<=": "≤", ">": ">", ">=": "≥", "==": "=", "===": "=", "!=": "≠", "!==": "≠"}


@dataclass
class Effect:
    node_id: str
    what: str
    at_seconds: float | None = None  # from start, when only sleeps lead here
    conditions: list[str] = field(default_factory=list)
    poll_seconds: float | None = None
    delay_after_seconds: float = 0  # sleeps between the matched condition and the effect
    starts_after_seconds: float = 0  # sleeps before the polling loop begins
    ends: bool = False  # nothing waits or loops after it: the workflow finishes


@dataclass
class Readback:
    effects: list[Effect]
    deadline: datetime | None
    text: str

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "effects": [e.__dict__ for e in self.effects],
        }


def _body(node: dict) -> dict:
    body = (node.get("action") or {}).get("request", {}).get("body")
    return body if isinstance(body, dict) else {}


def _call(node: dict) -> tuple[str, dict]:
    body = _body(node)
    return str(body.get("capability_name") or ""), body.get("input") or {}


def _is_read(capability: str, inner: str) -> bool:
    if capability == "home.execute":
        verb = inner.rsplit(".", 1)[-1].lower()
        return not (verb in _ACTION_VERBS or verb.startswith(("set", "turn_", "run")))
    return not (
        capability.endswith((".send", "send_markdown")) or capability in ("report.write_html",)
    )


def _quote(text: str, limit: int = 140) -> str:
    text = _TEMPLATE.sub("‹valor›", str(text)).strip().replace("\n", " ")
    return "“" + (text[: limit - 1] + "…" if len(text) > limit else text) + "”"


def _effect_text(node: dict) -> str | None:
    if node.get("kind") != "task":
        return None
    capability, args = _call(node)
    if not capability:
        return None  # internal callbacks (plan completion)
    if capability.startswith("telegram."):
        return f"envia no Telegram {_quote(args.get('text', ''))}"
    if capability == "report.write_html":
        return "gera um relatório HTML"
    if capability == "home.execute":
        inner = str(args.get("capability") or "")
        if _is_read(capability, inner):
            return None
        extra = args.get("input") or {}
        detail = f" ({', '.join(f'{k}={v}' for k, v in extra.items())})" if extra else ""
        return f"executa {inner} em {args.get('target')}{detail}"
    if not _is_read(capability, ""):
        return f"chama {capability}"
    return None


class _Describer:
    def __init__(self, definition: dict, tz: ZoneInfo, start: datetime):
        self.nodes = {n["id"]: n for n in definition.get("nodes", []) if "id" in n}
        self.entry = definition.get("entrypoint") or next(iter(self.nodes), None)
        self.tz, self.start = tz, start
        self.cyclic = self._cyclic_nodes()
        self.capability_uses: dict[str, int] = {}
        for node in self.nodes.values():
            if node.get("kind") == "task" and not _UNROLLED.search(node["id"]):
                cap = _call(node)[0]
                self.capability_uses[cap] = self.capability_uses.get(cap, 0) + 1
        self.deadlines: list[float] = []
        for node in self.nodes.values():
            for case in node.get("cases", []) if node.get("kind") == "switch" else []:
                when = case.get("when")
                if isinstance(when, dict) and len(when) == 1:
                    ((op, args),) = when.items()
                    if (
                        op in (">", ">=")
                        and isinstance(args, list)
                        and len(args) == 2
                        and isinstance(args[1], (int, float))
                        and isinstance(args[0], dict)
                        and (self._source(str(args[0].get("var", ""))) or ("",))[0].startswith(
                            "runtime.timestamp"
                        )
                    ):
                        self.deadlines.append(float(args[1]))

    # ── graph ──────────────────────────────────────────────────────────────────
    def _edges(self, node: dict) -> list[tuple[str, str | None]]:
        kind = node.get("kind")
        if kind == "switch":
            out = [(c.get("target"), c) for c in node.get("cases", [])]
            out.append((node.get("default"), None))
            return [(t, c) for t, c in out if t and t != "end"]
        if kind == "split":  # branches run, then the parent resumes at the join
            out = [(b, None) for b in node.get("branches", [])]
            return out + ([(node["join"], None)] if node.get("join") else [])
        nxt = node.get("next")
        return [(nxt, None)] if nxt and nxt != "end" else []

    def _cyclic_nodes(self) -> set[str]:
        index, low, stack, on, out, counter = {}, {}, [], set(), set(), [0]

        def strong(v):  # Tarjan, iterative enough for workflow sizes
            index[v] = low[v] = counter[0]
            counter[0] += 1
            stack.append(v)
            on.add(v)
            for w, _ in self._edges(self.nodes[v]):
                if w not in self.nodes:
                    continue
                if w not in index:
                    strong(w)
                    low[v] = min(low[v], low[w])
                elif w in on:
                    low[v] = min(low[v], index[w])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                self_loop = any(t == v for t, _ in self._edges(self.nodes[v]))
                if len(comp) > 1 or self_loop:
                    out.update(comp)

        for v in self.nodes:
            if v not in index:
                strong(v)
        return out

    def _finishes(self, nid: str) -> bool:
        """From this node on, the workflow ends without waiting or looping again."""
        seen, stack = set(), [t for t, _ in self._edges(self.nodes[nid])]
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in self.nodes:
                continue
            seen.add(cur)
            node = self.nodes[cur]
            if node.get("kind") == "sleep" or cur in self.cyclic:
                return False
            stack.extend(t for t, _ in self._edges(node))
        return True

    def _poll_seconds(self, node_id: str) -> float | None:
        sleeps = [
            float(self.nodes[n].get("durationSeconds") or 0)
            for n in self.cyclic
            if self.nodes[n].get("kind") == "sleep"
        ]
        return min(sleeps) if sleeps else None

    # ── conditions ─────────────────────────────────────────────────────────────
    def _source(self, path: str) -> tuple[str, str] | None:
        m = _OUT.match(path)
        if not m:
            return None
        node = self.nodes.get(m.group(1))
        if not node:
            return None
        capability, args = _call(node)
        if capability == "home.execute":
            label = f"{args.get('capability')}:{args.get('target')}"
        else:
            label = capability or m.group(1)
            if self.capability_uses.get(capability, 0) > 1:
                label = f"{label} ({_UNROLLED.sub('', m.group(1))})"  # base vs fetch
        return label, m.group(2)

    def _value(self, value) -> str:
        if isinstance(value, dict) and "var" in value:
            src = self._source(str(value["var"]))
            if src:
                label, fld = src
                name = label.split(":", 1)[-1] if ":" in label else label
                return f"{name} {fld.removeprefix('result.')}".strip()
            return str(value["var"])
        if isinstance(value, dict) and len(value) == 1:
            ((op, args),) = value.items()
            if op in ("*", "+", "-", "/") and isinstance(args, list) and len(args) == 2:
                sym = {"*": "×", "+": "+", "-": "−", "/": "÷"}[op]
                return f"{self._value(args[0])} {sym} {self._value(args[1])}"
        if isinstance(value, bool):
            return "true" if value else "false"
        return (
            repr(value)
            if isinstance(value, str)
            else f"{value:g}"
            if isinstance(value, float)
            else str(value)
        )

    def _presence(self, expr) -> str | None:
        """and/or of presence reads → '@all|a,b:fora' style atom, else None."""
        if not isinstance(expr, dict) or len(expr) != 1:
            return None
        ((op, args),) = expr.items()
        if op in ("and", "or") and isinstance(args, list):
            atoms = [self._presence(a) for a in args]
            atoms = [a for a in atoms if a != ""]
            if not atoms or any(a is None for a in atoms):
                return None
            parsed = [a[1:].split(":") for a in atoms]  # [quant|people, state]
            states = {st for _, st in parsed}
            people = sorted({p for who, _ in parsed for p in who.split("|", 1)[1].split(",")})
            if len(states) != 1:
                return None
            quant = "all" if op == "and" else "any"
            if len(people) == 1:
                quant = "one"
            return f"@{quant}|{','.join(people)}:{states.pop()}"
        if op in ("==", "===", "!=", "!==") and isinstance(args, list) and len(args) == 2:
            left, right = args
            if isinstance(left, dict) and "var" in left:
                src = self._source(str(left["var"]))
                if src and src[1] == "success":
                    return ""  # plumbing inside a presence conjunction
                if src and src[0].startswith("presence.read:") and src[1] == "result.present":
                    who = src[0].split(":", 1)[1]
                    home = bool(right) if op in ("==", "===") else not bool(right)
                    return f"@one|{who}:{'casa' if home else 'fora'}"
        return None

    def _render(self, expr) -> str | None:
        """Meaningful condition text; falls back to plumbing (a read worked / returned a
        value) when that is all the case checks, so success and failure branches differ."""
        return self._render_meaning(expr) or self._render_plumbing(expr)

    def _render_plumbing(self, expr) -> str | None:
        if not isinstance(expr, dict) or len(expr) != 1:
            return None
        ((op, args),) = expr.items()
        if op in ("and", "or") and isinstance(args, list):
            parts = list(dict.fromkeys(p for p in (self._render_plumbing(a) for a in args) if p))
            return (" e " if op == "and" else " ou ").join(parts) or None
        if op in ("==", "===", "!=", "!==") and isinstance(args, list) and len(args) == 2:
            left, right = args
            src = self._source(str(left.get("var", ""))) if isinstance(left, dict) else None
            if src is None:
                return None
            name = src[0].split(":", 1)[-1] if ":" in src[0] else src[0]
            positive = op in ("==", "===")
            if src[1] == "success" and isinstance(right, bool):
                return f"a leitura de {name} {'funcionou' if positive == right else 'falhou'}"
            if right is None:
                return f"{name} {'sem' if positive else 'com'} resultado"
        return None

    def _render_meaning(self, expr) -> str | None:
        presence = self._presence(expr)
        if presence:
            return presence
        if not isinstance(expr, dict) or len(expr) != 1:
            return None
        ((op, args),) = expr.items()
        if op in ("and", "or"):
            parts = [p for p in (self._render_meaning(a) for a in args) if p]
            if not parts:
                return None
            return (" e " if op == "and" else " ou ").join(parts)
        if op == "!":
            inner = self._render_meaning(args[0] if isinstance(args, list) else args)
            return f"não ({inner})" if inner else None
        if op in _OPS and isinstance(args, list) and len(args) == 2:
            left, right = args
            if isinstance(left, dict) and "var" in left:
                src = self._source(str(left["var"]))
                if src is None:
                    return None
                label, fld = src
                if fld == "success":  # read succeeded — plumbing, not meaning
                    return None
                if right is None:  # "reading present" — plumbing
                    return None
                if (
                    label.startswith("runtime.timestamp")
                    and op in (">", ">=")
                    and isinstance(right, (int, float))
                ):
                    return f"prazo de {self._fmt(datetime.fromtimestamp(right, UTC))} esgotado"
                return f"{self._value(left)} {_OPS[op]} {self._value(right)}"
        return None

    def _fmt(self, at: datetime) -> str:
        at = at + timedelta(seconds=30)  # nearest minute: 23:29:58 reads as 23:30
        return at.astimezone(self.tz).strftime("%d/%m/%Y %H:%M")

    # ── walk ───────────────────────────────────────────────────────────────────
    def effects(self) -> list[Effect]:
        found: dict[tuple, Effect] = {}
        paths = [0]

        def walk(nid, elapsed, conds, on_path, polled, depth, after=0.0, start=0.0):
            if paths[0] >= _MAX_PATHS or depth > _MAX_DEPTH or nid not in self.nodes:
                return
            if nid in on_path:
                return  # back-edge: the loop is described by its conditions and poll period
            node = self.nodes[nid]
            if not polled and nid in self.cyclic:
                start = elapsed  # sleeps before the loop: when watching begins
            polled = polled or nid in self.cyclic
            if node.get("kind") == "sleep":
                seconds = float(node.get("durationSeconds") or 0)
                elapsed += seconds
                if polled and nid not in self.cyclic:
                    after += seconds
            what = _effect_text(node)
            if what:
                # Unrolled repeats re-check the same thing; presence keeps its order
                # (casa → fora → casa is an arrival, not a departure).
                seen_plain: set[str] = set()
                kept = []
                for c in conds:
                    if c.startswith("@"):
                        if not kept or kept[-1] != c:
                            kept.append(c)
                    elif c not in seen_plain:
                        seen_plain.add(c)
                        kept.append(c)
                conds = kept
                key = (nid, tuple(conds))
                if key not in found:
                    found[key] = Effect(
                        node_id=nid,
                        what=what,
                        at_seconds=None if polled else elapsed,
                        conditions=list(conds),
                        poll_seconds=self._poll_seconds(nid) if polled else None,
                        delay_after_seconds=after,
                        starts_after_seconds=start if polled else 0,
                        ends=self._finishes(nid),
                    )
            edges = self._edges(node)
            if not edges:
                paths[0] += 1
                return
            for target, case in edges:
                extra = []
                if node.get("kind") == "switch" and case is not None:
                    rendered = self._render(case.get("when"))
                    extra = [rendered] if rendered else []
                elif (
                    node.get("kind") == "switch"
                    and not (nid in self.cyclic and target in self.cyclic)
                    # Only an alternative effect (e.g. "não consegui") is a branch worth
                    # naming; "nothing new, go on to the next round" is not.
                    and _effect_text(self.nodes.get(target) or {}) is not None
                ):
                    others = [self._render(c.get("when")) for c in node.get("cases", [])]
                    others = [o for o in others if o and not o.startswith("@")]
                    if others:
                        extra = [f"caso contrário de ({'; '.join(others)})"]
                # A default edge that loops back is "keep waiting"; it adds no condition.
                walk(
                    target, elapsed, conds + extra, on_path | {nid}, polled, depth + 1, after, start
                )

        if self.entry:
            walk(self.entry, 0.0, [], frozenset(), False, 0)
        return list(found.values())


def _presence_phrase(conds: list[str]) -> list[str]:
    """Fold presence observations into arrivals/departures: fora→casa = chegar."""
    out: list[str] = []
    seq: dict[str, list[tuple[str, str]]] = {}
    for c in conds:
        if c.startswith("@"):
            head, state = c[1:].rsplit(":", 1)
            quant, people = head.split("|", 1)
            states = seq.setdefault(people, [])
            if not states or states[-1][1] != state:
                states.append((quant, state))
        else:
            out.append(c)
    for people, states in seq.items():
        names = people.replace(",", ", ")
        group = "," in people
        last = [st for _, st in states[-2:]]
        if last == ["fora", "casa"]:
            phrase = (
                f"quando alguém de ({names}) chegar em casa"
                if group
                else f"quando {names} chegar em casa"
            )
        elif last == ["casa", "fora"]:
            phrase = (
                f"quando todos ({names}) tiverem saído de casa"
                if group
                else f"quando {names} sair de casa"
            )
        else:
            where = "em casa" if last[-1] == "casa" else "fora de casa"
            phrase = (
                f"quando ({names}) {'estiverem' if group else 'estiver'} {where}"
                if group
                else f"quando {names} estiver {where}"
            )
        out.insert(0, phrase)
    return out


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _literal(value):
    if isinstance(value, dict) and set(value) == {"__trama_literal_json__"}:
        return value["__trama_literal_json__"]
    return value


def _continuations(definition: dict) -> list[str]:
    """Long monitors run in blocks; the last node of a block starts the next one."""
    out = []
    for node in definition.get("nodes", []):
        if node.get("kind") != "task":
            continue
        request = (node.get("action") or {}).get("request") or {}
        body = request.get("body") if isinstance(request.get("body"), dict) else {}
        context = _literal(body.get("context")) or {}
        cont = _literal(context.get("workflow_continuation")) if isinstance(context, dict) else None
        if not str(request.get("url", "")).rstrip("/").endswith("/plans") or not isinstance(
            cont, dict
        ):
            continue
        repeats = [
            st
            for st in ((cont.get("plan") or {}).get("steps") or [])
            if isinstance(st, dict) and st.get("kind") == "repeat"
        ]
        r0 = repeats[0] if repeats else {}
        if r0.get("duration_seconds") or r0.get("deadline_unix") or r0.get("continuous"):
            until = (
                f"até completar ≈ {_human(float(r0['duration_seconds']))} no total"
                if r0.get("duration_seconds")
                else "sem prazo definido"
                if r0.get("continuous")
                else "até "
                + datetime.fromtimestamp(float(r0["deadline_unix"]), UTC).strftime(
                    "%d/%m/%Y %H:%M UTC"
                )
            )
            out.append(
                "Ao fim deste bloco, o mesmo monitor (checagens e avisos) continua sozinho "
                f"{until}."
            )
        elif repeats and repeats[0].get("count") and repeats[0].get("interval_seconds"):
            r = repeats[0]
            total = float(r["count"]) * float(r["interval_seconds"])
            out.append(
                "Ao fim deste bloco, o mesmo monitor (checagens e avisos) continua sozinho até "
                f"completar {int(r['count'])} rodadas a cada {_human(float(r['interval_seconds']))}"
                f" (≈ {_human(total)} no total)."
            )
        else:
            out.append(
                "Ao fim deste bloco, o mesmo monitor (checagens e avisos) continua sozinho "
                "num novo bloco."
            )
    return list(dict.fromkeys(out))


def _sources(definition: dict) -> list[str]:
    """What the workflow reads: devices, prices, searches, metrics (deduplicated)."""
    out: list[str] = []
    for node in definition.get("nodes", []):
        if node.get("kind") != "task" or _effect_text(node) is not None:
            continue
        capability, args = _call(node)
        if not capability or capability.startswith(("runtime.", "stat.", "memory.")):
            continue
        if capability == "home.execute":
            item = f"{args.get('capability')} de {args.get('target')}"
        else:
            shown = [
                str(v)[:60]
                for k, v in args.items()
                if isinstance(v, (str, int, float))
                and k not in ("text", "prompt", "instructions")
                and "{{" not in str(v)
            ][:3]
            item = f"{capability} ({', '.join(shown)})" if shown else capability
        if item not in out:
            out.append(item)
    return out


def describe(
    definition: dict,
    *,
    start: datetime | None = None,
    timezone: str = "America/Sao_Paulo",
) -> Readback:
    tz = ZoneInfo(timezone)
    start = start or datetime.now(UTC)
    d = _Describer(definition, tz, start)
    effects = d.effects()
    deadline = datetime.fromtimestamp(max(d.deadlines), UTC) if d.deadlines else None

    continuations = _continuations(definition)
    block = " neste bloco" if continuations else ""
    timed = [e for e in effects if e.at_seconds is not None and not e.conditions]
    lines: list[str] = []
    # Repeated identical timed effects (a dated series) collapse to dates.
    by_what: dict[str, list[Effect]] = {}
    for e in timed:
        by_what.setdefault(e.what, []).append(e)
    for what, group in by_what.items():
        times = sorted(start + timedelta(seconds=e.at_seconds) for e in group)
        if len(times) == 1 and group[0].at_seconds < 5:
            lines.append(f"• Agora: {what}.")
        elif len(times) <= 4:
            lines.append(f"• {_cap(what)} em {', '.join(d._fmt(t) for t in times)}.")
        else:
            first = ", ".join(d._fmt(t) for t in times[:3])
            lines.append(f"• {_cap(what)} {len(times)} vezes: {first}, … até {d._fmt(times[-1])}.")
    grouped: dict[tuple, list[Effect]] = {}
    for e in effects:
        if e in timed:
            continue
        grouped.setdefault(tuple(_presence_phrase(e.conditions)), []).append(e)
    for conds, group in grouped.items():
        whats = list(dict.fromkeys(e.what for e in group))
        when = "; ".join(conds) if conds else "durante a execução"
        timing = ""
        poll = next((e.poll_seconds for e in group if e.poll_seconds), None)
        times = sorted({e.at_seconds for e in group if e.at_seconds is not None})
        joined = " e depois ".join(whats)
        if len(times) > 1:
            step = times[1] - times[0]
            if all(e.ends for e in group):
                lines.append(
                    f"• {_cap(when)}: {joined} — uma vez, e encerra "
                    f"(verifica a cada {_human(step)})."
                )
            else:
                lines.append(
                    f"• {_cap(when)}: {joined} — até {len(times)} vezes{block}, a cada "
                    f"{_human(step)} (última checagem depois de {_human(times[-1])})."
                )
            continue
        delay = next((e.at_seconds for e in group if e.at_seconds), None)
        wait = next((e.delay_after_seconds for e in group if e.delay_after_seconds), 0)
        if wait:
            when += f", espera {_human(wait)}"
        begins = next((e.starts_after_seconds for e in group if e.starts_after_seconds), 0)
        if poll:
            timing = f" (verifica a cada {_human(poll)}"
            if begins >= 60:
                timing += f", a partir de {d._fmt(start + timedelta(seconds=begins))}"
            timing += ")"
        elif delay:
            timing = f" (depois de {_human(delay)})"
        once = " — uma vez, e encerra" if poll and all(e.ends for e in group) else ""
        lines.append(f"• {_cap(when)}: {joined}{once}{timing}.")
    if not lines:
        lines.append("• Não envia mensagens nem age em dispositivos.")
    lines += continuations
    sources = _sources(definition)
    if sources:
        shown = sources[:10] + ([f"… +{len(sources) - 10}"] if len(sources) > 10 else [])
        lines.append("Consulta: " + "; ".join(shown) + ".")
    if deadline:
        lines.append(f"Encerra até {d._fmt(deadline)} se nada acontecer.")
    return Readback(effects=effects, deadline=deadline, text="\n".join(lines))


def _human(seconds: float) -> str:
    s = round(seconds)
    for unit, size in (("dia", 86400), ("h", 3600), ("min", 60)):
        if s >= size and s % size == 0:
            n = s // size
            return f"{n} {unit}{'s' if unit == 'dia' and n > 1 else ''}"
    if s >= 3600:
        return f"{s / 3600:.1f} h"
    if s >= 60:
        return f"{s // 60} min"
    return f"{s} s"


def execution_outcome(
    definition: dict, steps: list[dict], *, timezone: str = "America/Sao_Paulo"
) -> str:
    """What a finished execution actually did, from Trama's step log.

    A completion message used to repeat the request ("Automação concluída: desligar o ar…")
    even when the monitor ended at its deadline without acting.
    """
    tz = ZoneInfo(timezone)
    nodes = {n["id"]: n for n in definition.get("nodes", []) if "id" in n}
    done, failed, reads = [], [], 0
    for st in steps:
        if st.get("phase") not in (None, "UP"):
            continue  # compensations are reported by the failure path
        node = nodes.get(st.get("stepName"))
        if node is None or node.get("kind") != "task":
            continue
        what = _effect_text(node)
        if what is None:
            if _call(node)[0] and not _call(node)[0].startswith(("runtime.", "stat.")):
                reads += 1
            continue
        when = st.get("createdAt") or st.get("stepStartedAt")
        try:
            at = datetime.fromisoformat(str(when)).astimezone(tz)
            stamp = (at + timedelta(seconds=30)).strftime("%d/%m %H:%M")
        except ValueError:
            stamp = "?"
        (done if st.get("success") else failed).append(f"{stamp} {what}")
    lines = []
    if done:
        lines.append("Fez: " + "; ".join(done) + ".")
    if failed:
        lines.append("Falhou: " + "; ".join(failed) + ".")
    if not done and not failed:
        lines.append(
            "Não agiu: a condição não aconteceu"
            + (f" em {reads} verificaç{'ão' if reads == 1 else 'ões'}" if reads else "")
            + "."
        )
    return "\n".join(lines)
