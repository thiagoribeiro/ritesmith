"""Record source and runtime identity without reading environment secrets."""

import hashlib
import json
import platform
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def software_manifest():
    root = Path(__file__).resolve().parents[2]
    files = {}
    for directory in ("ritesmith", "benchmarks/generation_latency", "benchmarks/luau_codegen"):
        for path in sorted((root / directory).rglob("*.py")):
            files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    source_sha = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    try:
        runtime_version = version("lunardyson")
    except PackageNotFoundError:
        runtime_version = "unavailable"
    return {
        "source_sha256": source_sha,
        "files": files,
        "recorded_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "lunardyson": runtime_version,
    }


if __name__ == "__main__":
    print(json.dumps(software_manifest(), indent=2))
