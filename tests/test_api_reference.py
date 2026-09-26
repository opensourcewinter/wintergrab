"""docs/api.md is written from the code: it must say what the code has."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "api_reference.py"


def test_the_api_reference_is_up_to_date() -> None:
    pytest.importorskip("pyarrow")  # (every documented module imports)
    spec = importlib.util.spec_from_file_location("api_reference", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--check"]) == 0, "run: python scripts/api_reference.py"
