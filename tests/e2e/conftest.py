from __future__ import annotations

import pytest

from .harness import require_e2e_prerequisites


@pytest.fixture(scope="session", autouse=True)
def require_prerequisites() -> None:
    require_e2e_prerequisites()
