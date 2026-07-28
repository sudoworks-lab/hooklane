"""Validate the tag used by local application image verification."""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Final


DEFAULT_IMAGE_TAG: Final = "0.1.1"
APPLICATION_IMAGE_NAMES: Final = (
    "hooklane-api",
    "hooklane-worker",
    "hooklane-mock-sink",
)
IMAGE_TAG_PATTERN: Final = re.compile(r"(?:0\.1\.1|git-[0-9a-f]{40})\Z")


class ImageTagError(ValueError):
    """The local image tag is outside the immutable verification contract."""


def validate_image_tag(value: str) -> str:
    if not IMAGE_TAG_PATTERN.fullmatch(value):
        raise ImageTagError(
            "IMAGE_TAG must be exactly 0.1.1 or git-<40 lowercase hexadecimal characters>"
        )
    return value


def resolve_image_tag(value: str | None = None) -> str:
    candidate = os.environ.get("IMAGE_TAG", DEFAULT_IMAGE_TAG) if value is None else value
    return validate_image_tag(candidate)


def application_images(image_tag: str) -> tuple[str, ...]:
    tag = validate_image_tag(image_tag)
    return tuple(f"{name}:{tag}" for name in APPLICATION_IMAGE_NAMES)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-tag", default=None)
    return parser.parse_args()


def main() -> int:
    try:
        resolve_image_tag(parse_args().image_tag)
    except ImageTagError as error:
        print(f"[fail] image tag contract: {error}", file=sys.stderr)
        return 1
    print("[ok] local image tag contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
