#!/usr/bin/env python3
"""Restore a saved RiteSmith image/configuration without reverting application data."""

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument(
        "--check", action="store_true", help="Check rollback resources without switching"
    )
    args = parser.parse_args()
    snapshot = args.snapshot.resolve()
    manifest = json.loads((snapshot / "manifest.json").read_text())
    for name in (".env", "docker-compose.yml"):
        if not (snapshot / name).is_file():
            raise SystemExit(f"Missing rollback configuration: {name}")
    subprocess.run(
        ["docker", "image", "inspect", manifest["image_id"]],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    if args.check:
        print("Rollback image and configuration are available")
        return
    deploy = Path(manifest["deploy_dir"])
    for name in (".env", "docker-compose.yml"):
        shutil.copy2(snapshot / name, deploy / name)
    (deploy / ".env").chmod(0o600)
    subprocess.run(["docker", "tag", manifest["image_id"], "ritesmith:latest"], check=True)
    subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            manifest["compose_project"],
            "--file",
            str(deploy / "docker-compose.yml"),
            "up",
            "--detach",
            "--no-deps",
            "--force-recreate",
            "ritesmith",
        ],
        cwd=deploy,
        check=True,
    )
    for _ in range(30):
        try:
            with urlopen("http://127.0.0.1:8081/health", timeout=2) as response:
                if json.load(response)["status"] == "ok":
                    print(
                        "Rollback completed; RiteSmith is healthy. Application data was retained."
                    )
                    return
        except Exception:
            time.sleep(1)
    raise SystemExit("Rollback container started, but its health check failed")


if __name__ == "__main__":
    main()
