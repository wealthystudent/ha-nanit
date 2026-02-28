#!/usr/bin/env python3
"""Generate betterproto Python code from nanit.proto."""

import subprocess
import sys
from pathlib import Path

PROTO_DIR = Path(__file__).parent.parent / "proto"
OUT_DIR = Path(__file__).parent.parent / "aionanit" / "proto"


def main() -> None:
    """Run protoc with betterproto plugin."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{PROTO_DIR}",
        f"--python_betterproto_out={OUT_DIR}",
        str(PROTO_DIR / "nanit.proto"),
    ]

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"STDERR: {result.stderr}", file=sys.stderr)
        sys.exit(result.returncode)

    print(f"Generated protobuf code in {OUT_DIR}")

    # Rename the generated file from nanit.py to the correct location
    # betterproto generates into a package based on the proto package name
    generated_dir = OUT_DIR / "nanit"
    if generated_dir.is_dir():
        # Move __init__.py from nanit/ subdirectory up as nanit.py
        init_file = generated_dir / "__init__.py"
        if init_file.exists():
            target = OUT_DIR / "nanit.py"
            target.write_text(init_file.read_text())
            # Clean up the generated subdirectory
            init_file.unlink()
            generated_dir.rmdir()
            print(f"Moved generated code to {target}")


if __name__ == "__main__":
    main()
