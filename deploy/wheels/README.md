# Prebuilt wheels (optional override)

RiteSmith needs the **LunarDyson** wheel to run `luau_script` artifacts. The Docker
image downloads it from the LunarDyson GitHub release pinned by the
`LUNARDYSON_VERSION` build argument (see `Dockerfile`), for the image's architecture
(`x86_64` or `aarch64`).

To test an unreleased LunarDyson build instead, drop its wheel here (`*.whl` files in
this directory are git-ignored); a `lunardyson-*.whl` found here takes precedence over
the download:

```bash
# inside ../lunardyson, on a machine with the target architecture
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build --parallel 2
./scripts/build_wheel.sh            # → dist/lunardyson-<version>-py3-none-linux_<arch>.whl
```

Keep `RITESMITH_REQUIRE_LUAU=true` in production: an image without LunarDyson then
refuses to start instead of silently falling back to the Lua (lupa) sandbox.
`GET /health` reports the active script runtime (`"script_runtime": "lunardyson 0.1.2"`).
