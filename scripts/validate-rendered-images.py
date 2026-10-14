#!/usr/bin/env python3
"""Fail closed when any rendered Kubernetes image reference is not digest pinned."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from typing import Any

import yaml

DIGEST_PIN = re.compile(r"@sha256:[0-9a-f]{64}$")


def image_references(value: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key == "image" and isinstance(child, str):
                yield child_path, child
            yield from image_references(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from image_references(child, f"{path}[{index}]")


def main() -> int:
    documents = [item for item in yaml.safe_load_all(sys.stdin) if item]
    invalid = [
        (path, image)
        for item in documents
        for path, image in image_references(item)
        if not DIGEST_PIN.search(image)
    ]
    if invalid:
        for path, image in invalid:
            print(f"unpinned rendered image at {path}: {image}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
