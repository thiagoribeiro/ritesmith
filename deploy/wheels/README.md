# Prebuilt wheels

The Docker image installs every `*.whl` found here (they are git-ignored).
RiteSmith needs the **LunarDyson** wheel to run `luau_script` artifacts. Without
it, generation falls back to Lua and the app logs a warning at startup.

The wheel bundles the native `liblunardyson.so`, so it must be built for the image's
platform. For the Orange Pi / R2D2 that is `linux_aarch64`. Build it from the
sibling repo:

```bash
# on the target host (or any machine with the same arch), inside ../lunardyson
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build --parallel 2
./scripts/build_wheel.sh            # → dist/lunardyson-<version>-py3-none-linux_<arch>.whl
```

Then copy the wheel into this directory before running `deploy.sh`.
