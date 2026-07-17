from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
PLACEHOLDER_PATTERN = re.compile(r"\b(?:TODO|TBD|TBC)\b", re.IGNORECASE)
MINIMUM_CONTENT_LENGTH = 80


def validate_release_notes(tag: str, path: Path) -> list[str]:
    errors: list[str] = []
    if TAG_PATTERN.fullmatch(tag) is None:
        errors.append(f"release tag must match vX.Y.Z: {tag}")
    if path.name != f"{tag}.md":
        errors.append(f"release notes filename must be {tag}.md")

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"cannot read release notes: {error}")
        return errors

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    normalized = " ".join(lines)
    if len(normalized) < MINIMUM_CONTENT_LENGTH:
        errors.append(f"release notes must contain at least {MINIMUM_CONTENT_LENGTH} characters")
    if normalized.casefold() in {tag.casefold(), f"release {tag}".casefold()}:
        errors.append("release notes must not be a one-line release placeholder")
    if PLACEHOLDER_PATTERN.search(content) is not None:
        errors.append("release notes must not contain TODO, TBD, or TBC placeholders")
    if not any(line.startswith("## ") for line in lines):
        errors.append("release notes must contain at least one level-two section")
    if not any(line.startswith("- ") and line.removeprefix("- ").strip() for line in lines):
        errors.append("release notes must contain at least one concrete bullet point")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate prepared GitHub release notes.")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--file", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    errors = validate_release_notes(args.tag, args.file)
    if errors:
        for error in errors:
            print(f"Release notes validation failed: {error}", file=sys.stderr)
        return 1
    print(f"Release notes are valid: {args.file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
