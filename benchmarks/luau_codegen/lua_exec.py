"""Runs a Lua chunk under lupa — the same engine as the production sandbox.

Invoked as a subprocess so an infinite loop can be killed by timeout.

    python lua_exec.py check <file>   # syntax only (load)
    python lua_exec.py run <file>     # load + execute
"""

import sys

from lupa import LuaError, LuaRuntime


def main() -> int:
    mode, path = sys.argv[1], sys.argv[2]
    with open(path, encoding="utf-8") as f:
        src = f.read()

    lua = LuaRuntime(unpack_returned_tuples=True)
    loaded = lua.globals().load(src, "=script")
    fn, err = loaded if isinstance(loaded, tuple) else (loaded, None)
    if fn is None:
        print(err, file=sys.stderr)
        return 2
    if mode == "check":
        return 0
    try:
        fn()
    except LuaError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
