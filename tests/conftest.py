import pytest

from albatross import controls


@pytest.fixture(autouse=True)
def no_controls(monkeypatch):
    """No pause or budget scale is set unless a test sets one: the tests' fake pools don't answer agent_controls."""
    async def rows():
        return {}
    monkeypatch.setattr(controls, "_rows", rows)
    monkeypatch.setattr(controls, "_rungs", rows)        # and no agent culled
