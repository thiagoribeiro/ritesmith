"""Offline worker for real Trama rendering, never a production integration path."""

import glob
import hashlib
import json
import os
import select
import subprocess
from copy import deepcopy
from pathlib import Path


def artifact_hashes(classpath=None):
    classpath = classpath or os.environ.get("TRAMA_ORACLE_CLASSPATH")
    if not classpath:
        raise RuntimeError("A frozen native oracle classpath is required")
    files = {}
    for entry in classpath.split(os.pathsep):
        for path in map(Path, glob.glob(entry)):
            candidates = path.iterdir() if path.is_dir() else [path]
            for file in candidates:
                if file.suffix in (".jar", ".class"):
                    digest = hashlib.sha256(file.read_bytes()).hexdigest()
                    if file.name in files and files[file.name] != digest:
                        raise RuntimeError("Conflicting native oracle artifacts: " + file.name)
                    files[file.name] = digest
    return files


class TramaTransportOracle:
    def __init__(self, classpath=None):
        classpath = classpath or os.environ.get("TRAMA_ORACLE_CLASSPATH")
        if not classpath:
            raise RuntimeError("An actual compiled Trama oracle classpath is required")
        self.worker = subprocess.Popen(
            [os.environ.get("TRAMA_ORACLE_JAVA", "java"), "-cp", classpath, "TramaTypedJsonOracle"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def exchange(self, request):
        self.worker.stdin.write(json.dumps(request) + "\n")
        self.worker.stdin.flush()
        if not select.select([self.worker.stdout], [], [], 5)[0]:
            self.worker.kill()
            raise RuntimeError("Native Trama oracle exceeded its response deadline")
        line = self.worker.stdout.readline()
        if not line:
            raise RuntimeError("Native Trama oracle terminated")
        response = json.loads(line)
        if "error" in response:
            raise ValueError(response["error"])
        return response

    def render(self, body, context):
        return self.exchange({"body": body, "context": context})["body"]

    def condition(self, expression, context):
        return self.exchange({"expression": expression, "context": context})["condition"]

    def action(self, action, context):
        from ritesmith.workflows.simulation import render

        request = deepcopy(action["request"])
        body = request.pop("body", None)
        rendered = render({**action, "request": request}, context, native_templates=True)
        if body is not None:
            rendered["request"]["body"] = self.render(body, context)
        return rendered

    def close(self):
        if self.worker.stdin:
            self.worker.stdin.close()
        try:
            self.worker.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.worker.kill()
            self.worker.wait()
        self.worker.stdout.close()
        self.worker.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
