"""Fail if a tracked file names the maintainer's own deployment (people, places, hosts).

The denylist is stored as SHA-256 hashes of lowercase words (scripts/leak_gate.sha256),
so this public repo does not spell out what it must not contain. Also rejects private
LAN addresses other than the documentation/test ones.

    python scripts/leak_gate.py            # exit 1 and list hits
    python scripts/leak_gate.py --hash w   # print the hash line to add for a word
"""

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HASHES = ROOT / "scripts" / "leak_gate.sha256"
WORD = re.compile(r"[a-z0-9][a-z0-9_]*")
LAN = re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")
LAN_OK = {"192.168.0.1", "192.168.1.1", "192.168.1.10", "192.168.0.10"}


def digest(word: str) -> str:
    return hashlib.sha256(word.lower().encode()).hexdigest()


def main() -> int:
    if sys.argv[1:2] == ["--hash"]:
        for w in sys.argv[2:]:
            print(digest(w))
        return 0
    banned = {
        ln.split()[0]
        for ln in HASHES.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    }
    files = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    hits = []
    for name in files:
        path = ROOT / name
        if path.suffix in {".png", ".jpg", ".ico", ".whl", ".so"} or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            for w in WORD.findall(low):
                # split snake_case too: device aliases like fan_<name>
                for part in {w, *w.split("_")}:
                    if part and digest(part) in banned:
                        hits.append(f"{name}:{n}: private word (hash {digest(part)[:12]})")
            hits += [
                f"{name}:{n}: private LAN address {ip}"
                for ip in LAN.findall(line)
                if ip not in LAN_OK
            ]
    for h in sorted(set(hits)):
        print(h)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
