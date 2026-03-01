#!/usr/bin/env python3
"""Generate google protobuf Python code from nanit.proto."""

import subprocess
import sys
from pathlib import Path

PROTO_DIR = Path(__file__).parent.parent / "proto"
OUT_DIR = Path(__file__).parent.parent / "aionanit" / "proto"


def main() -> None:
    """Run protoc with --python_out to generate nanit_pb2.py."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{PROTO_DIR}",
        f"--python_out={OUT_DIR}",
        str(PROTO_DIR / "nanit.proto"),
    ]

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"STDERR: {result.stderr}", file=sys.stderr)
        sys.exit(result.returncode)

    print(f"Generated protobuf code in {OUT_DIR}")

    # protoc --python_out generates nanit_pb2.py directly in OUT_DIR.
    generated = OUT_DIR / "nanit_pb2.py"
    if generated.exists():
        print(f"OK: {generated}")
    else:
        print(f"WARNING: Expected {generated} not found", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
