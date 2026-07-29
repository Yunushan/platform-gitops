#!/usr/bin/env python3
"""Atheris entrypoint for migration, cutover, and transition plan parsing."""

from __future__ import annotations

from pathlib import Path
import sys

import atheris


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if SCRIPTS.exists() and str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

with atheris.instrument_imports():
    import fuzz_forge_plans


def test_one_input(data: bytes) -> None:
    fuzz_forge_plans.exercise_input(data)


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
