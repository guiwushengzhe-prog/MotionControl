"""Shared test setup.

The one thing that has to be global: user data now resolves through
%LOCALAPPDATA%\\MotionControl rather than the program folder, so without this a
test that constructs a GameProfileStore, a SceneLayoutManager or a VoiceService
would read and overwrite the developer's own mappings, calibration and paired
devices. Pointing MOTIONCONTROL_USER_DIR at a per-test directory makes that
impossible rather than merely unlikely.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path, monkeypatch):
    """Give every test its own user-data directory."""
    root = tmp_path / "user-data"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MOTIONCONTROL_USER_DIR", str(root))
    return root
