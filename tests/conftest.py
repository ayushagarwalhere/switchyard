"""Tests never see real keys: every key variable is removed and ~/.switchyard is a temp folder.

Tests that need a key set a fake one themselves (and point Token Factory at a local stand-in).
"""
import os

import pytest

from switchyard import config

KEY_VARS = (*config.KEY_NAMES, "OPENAI_API_KEY", "TAVILY_API_KEY", "LANGSMITH_API_KEY", "ANTHROPIC_API_KEY")


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch, tmp_path_factory):
    for k in KEY_VARS:
        monkeypatch.delenv(k, raising=False)
    home = tmp_path_factory.mktemp("switchyard_home")
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "USER_ENV", home / ".env")
    monkeypatch.setenv("SWITCHYARD_HOME", str(home))
    # load_env() also reads the source checkout's .env/.env.local: point it somewhere empty
    monkeypatch.setattr(config, "SOURCE_ROOT", home)
    yield
    assert not any(os.environ.get(k, "").startswith(("v1.", "eyJ")) for k in config.KEY_NAMES), "a real key leaked into a test"
