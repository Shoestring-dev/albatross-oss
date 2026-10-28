"""Model switches (albatross/llm/openrouter.py) and the shadow Strategist's source naming."""
from albatross.agents import strategist
from albatross.api import intel
from albatross.config import settings
from albatross.llm import openrouter


def test_overrides_parse_and_apply(monkeypatch):
    monkeypatch.setattr(settings, "llm_model_overrides", " events_news = typesafe/jev-router , bad, reports=x/y ")
    assert openrouter.overrides() == {"events_news": "typesafe/jev-router", "reports": "x/y"}
    assert openrouter.model_for("events_news", openrouter.FLASH) == "typesafe/jev-router"
    assert openrouter.model_for("strategist", openrouter.FLASH) == openrouter.FLASH


def test_routed_model_is_recorded():
    assert openrouter.recorded("typesafe/jev-router", "deepseek/deepseek-v4.1-flash") == "typesafe/jev-router -> deepseek/deepseek-v4.1-flash"
    assert openrouter.recorded(openrouter.FLASH, openrouter.FLASH) == openrouter.FLASH
    assert openrouter.recorded(openrouter.FLASH, None) == openrouter.FLASH


def test_shadow_source_is_its_own_graded_source():
    src = strategist.shadow_source("typesafe/jev-router")
    assert src == "strategist@jev-router" and src != strategist.AGENT
    assert intel.source_name(src) == "Strategist on jev-router (trial)"
