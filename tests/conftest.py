"""Shared test setup."""

import sys

import pytest


class OfflineOpenAI:
    """Stands in for the summarizer's OpenAI client: any API call fails fast.

    CompactionEngine builds its own client; without this, tests that pass a
    fake key would send real requests to api.openai.com. Tests that need a
    working summarizer replace `engine.summarizer.client` themselves.
    """

    def __init__(self, *args, **kwargs):
        self.responses = self
        self.chat = self
        self.completions = self

    def create(self, *args, **kwargs):
        raise ConnectionError("network access is disabled in tests")

    def close(self):
        pass


@pytest.fixture(autouse=True)
def offline_summarizer(monkeypatch):
    # The engine module can be imported as joy.* or src.joy.*: patch both.
    for name in ("joy.context_compaction", "src.joy.context_compaction"):
        module = sys.modules.get(name)
        if module is not None:
            monkeypatch.setattr(module, "OpenAI", OfflineOpenAI)
