"""Luau task types → Python TypedDict stubs for the `python` target (Monty + ty).

The task files declare their types once, in Luau. This translates them so the
Python target sees the same contract: records become TypedDicts, `{ T }` lists,
`{ [K]: V }` dicts, `T?` an optional key, `true`/`false` literal tags, and
unions stay unions (ty narrows them on the `ok` tag like luau-analyze does).
"""

import keyword
import re

from benchmarks.luau_codegen.preamble import BASE_TYPES, _tools_by_namespace

_PRIMS = {"string": "str", "number": "float", "boolean": "bool", "any": "Any", "nil": "None"}
_TOKEN = re.compile(r"\s*(->|[{}\[\]():,|?]|\w+)")


def _tokens(text: str) -> list[str]:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise ValueError(f"cannot tokenize Luau type near {text[pos:pos + 20]!r}")
        out.append(m.group(1))
        pos = m.end()
    return out


class _Translator:
    def __init__(self):
        self.classes: list[str] = []  # emitted TypedDict definitions, in dependency order

    def _class_name(self, hint: str) -> str:
        return "".join(p[:1].upper() + p[1:] for p in re.split(r"[_\W]+", hint) if p)

    def translate(self, text: str, hint: str) -> str:
        self.toks, self.i = _tokens(text), 0
        py = self._union(hint)
        if self.i != len(self.toks):
            raise ValueError(f"trailing tokens in {text!r}: {self.toks[self.i:]}")
        return py

    def _peek(self) -> str | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _eat(self, tok: str | None = None) -> str:
        t = self._peek()
        if t is None or (tok is not None and t != tok):
            raise ValueError(f"expected {tok!r}, got {t!r}")
        self.i += 1
        return t

    def _union(self, hint: str) -> str:
        arms = [self._optional(hint, 0)]
        n = 1
        while self._peek() == "|":
            self._eat("|")
            arms.append(self._optional(hint, n))
            n += 1
        if len(arms) > 1:  # name anonymous record arms Hint1, Hint2…
            return " | ".join(arms)
        return arms[0]

    def _optional(self, hint: str, n: int) -> str:
        t = self._term(f"{hint}{n + 1}" if n or self._union_ahead() else hint)
        if self._peek() == "?":
            self._eat("?")
            return f"{t} | None"
        return t

    def _union_ahead(self) -> bool:
        depth = 0
        for tok in self.toks[self.i:]:
            if tok in "{([":
                depth += 1
            elif tok in "})]":
                if depth == 0:
                    return False
                depth -= 1
            elif tok == "|" and depth == 0:
                return True
            elif tok == "," and depth == 0:
                return False
        return False

    def _term(self, hint: str) -> str:
        t = self._peek()
        if t == "{":
            return self._table(hint)
        if t == "(":
            self._eat("(")
            inner = self._union(hint)
            self._eat(")")
            return inner
        self._eat()
        if t in ("true", "false"):
            return f"Literal[{t.capitalize()}]"
        return _PRIMS.get(t, t)

    def _table(self, hint: str) -> str:
        self._eat("{")
        if self._peek() == "[":  # { [K]: V }
            self._eat("[")
            key = self._union(hint + "Key")
            self._eat("]")
            self._eat(":")
            val = self._union(hint + "Value")
            self._eat("}")
            return f"dict[{key}, {val}]"
        if self._peek() == "}":
            self._eat("}")
            return "dict[str, Any]"
        # record if `name :` follows, else array { T }
        if len(self.toks) > self.i + 1 and self.toks[self.i + 1] == ":":
            fields = []
            while self._peek() != "}":
                name = self._eat()
                self._eat(":")
                start = self.i
                ftype = self._union(hint + self._class_name(name))
                optional = self.toks[self.i - 1] == "?" and self.i - 1 >= start
                fields.append((name, ftype, optional))
                if self._peek() == ",":
                    self._eat(",")
            self._eat("}")
            return self._emit(hint, fields)
        elem = self._union(hint + "Item")
        self._eat("}")
        return f"list[{elem}]"

    def _emit(self, hint: str, fields) -> str:
        name = self._class_name(hint)
        if any(keyword.iskeyword(f) for f, _, _ in fields):  # e.g. `from`: functional syntax
            items = ", ".join(
                f'"{f}": ' + (f"NotRequired[{t.removesuffix(' | None')}]" if opt else t)
                for f, t, opt in fields
            )
            self.classes.append(f'{name} = TypedDict("{name}", {{{items}}})')
            return name
        lines = [f"class {name}(TypedDict):"]
        for fname, ftype, optional in fields:
            if optional:
                ftype = ftype.removesuffix(" | None")
                lines.append(f"    {fname}: NotRequired[{ftype}]")
            else:
                lines.append(f"    {fname}: {ftype}")
        self.classes.append("\n".join(lines))
        return name


def _declarations(text: str) -> list[tuple[str, str]]:
    """`type X = …` blocks (continuation lines start with `|`)."""
    decls: list[tuple[str, str]] = []
    for line in text.splitlines():
        m = re.match(r"\s*(?:export\s+)?type\s+(\w+)\s*=\s*(.*)$", line)
        if m:
            decls.append((m.group(1), m.group(2)))
        elif line.strip() and decls:
            name, body = decls[-1]
            decls[-1] = (name, f"{body} {line.strip()}")
    return decls


_HEADER = "from typing import Any, Literal, NotRequired, TypedDict"


def _types_block(task: dict) -> tuple[str, dict[str, tuple[str, str]]]:
    """(python declarations, {tool name: (args type, result type)})."""
    tr = _Translator()
    aliases = []
    luau = "\n".join(p for p in (BASE_TYPES, task.get("types", "").strip()) if p)
    for name, body in _declarations(luau):
        py = tr.translate(body, name)
        if py != name:  # a record emits `class Name`; anything else becomes an alias
            tr.classes.append(f"{name} = {py}")
    sigs: dict[str, tuple[str, str]] = {}
    for tool in task.get("tools", []):
        m = re.match(r"\s*\(\s*args\s*:\s*(.*)\)\s*->\s*(.*)$", tool["signature"], re.DOTALL)
        if not m:
            raise ValueError(f"unsupported tool signature: {tool['signature']}")
        base = tr._class_name(tool["name"].replace(".", "_"))
        args = tr.translate(m.group(1), base + "Args")
        result = tr.translate(m.group(2), base + "Result")
        if " | " in result or result.startswith(("list[", "dict[")):
            tr.classes.append(f"{base}Result = {result}")
            result = f"{base}Result"
        sigs[tool["name"]] = (args, result)
    aliases = tr.classes
    return "\n\n".join([_HEADER, *aliases]), sigs


def task_stubs(task: dict) -> str:
    """Declarations visible to the script while type checking: Input, Output, Context…"""
    return _types_block(task)[0]


def prompt_types(task: dict) -> str:
    """The same declarations for the prompt, without the typing import (not in scope)."""
    return task_stubs(task).removeprefix(_HEADER).strip()


def tool_signatures(task: dict) -> dict[str, str]:
    """{tool name: 'tools.ns.fn(args: XArgs) -> XResult'} for the prompt."""
    _, sigs = _types_block(task)
    return {name: f"tools.{name}(args: {a}) -> {r}" for name, (a, r) in sigs.items()}


def tool_module_stubs(task: dict) -> dict[str, str]:
    """`.pyi` text per tools namespace: the shared declarations plus the functions."""
    block, sigs = _types_block(task)
    out = {}
    for ns, tools in _tools_by_namespace(task).items():
        defs = [f"def {t['fn']}(args: {sigs[t['name']][0]}) -> {sigs[t['name']][1]}: ..." for t in tools]
        out[ns] = block + "\n\n" + "\n".join(defs) + "\n"
    return out
