from __future__ import annotations

import time
from pathlib import Path

from smartfs.suppression import SelfWriteSuppressor


def test_suppression_expires(tmp_path: Path) -> None:
    suppressor = SelfWriteSuppressor(default_ttl=0.1)
    path = tmp_path / "a.txt"
    assert not suppressor.is_suppressed(path)
    suppressor.mark(path)
    assert suppressor.is_suppressed(path)
    time.sleep(0.15)
    assert not suppressor.is_suppressed(path)


def test_suppression_clear(tmp_path: Path) -> None:
    suppressor = SelfWriteSuppressor(default_ttl=30)
    path = tmp_path / "a.txt"
    suppressor.mark(path)
    suppressor.clear(path)
    assert not suppressor.is_suppressed(path)
