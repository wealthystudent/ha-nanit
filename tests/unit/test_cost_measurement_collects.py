"""The opt-in cost measurement can be collected where ``resource`` does not exist (Windows)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

COST = Path(__file__).parent / "recorder" / "test_statistics_cost.py"


def test_the_cost_measurement_imports_without_the_resource_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "resource", None)  # as on Windows: importing it fails
    spec = importlib.util.spec_from_file_location("_cost_measurement_without_resource", COST)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert module.pytestmark.name == "skipif"
