import pytest

from james_monitoring.config import parse_config
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.runtime import ConsoleBus, Runtime


def make_raw(tmp_path, **over):
    raw = {
        "company": "CuriousDevs", "timezone": "Asia/Kolkata",
        "owner": {"name": "Pankaj", "telegram_user_id": 111},
        "llm": {"provider": "fake", "model": "fake"},
        "workspace": {"path": str(tmp_path / "ws")},
        "telegram": {"group_chat_id": -100},
        "budget": {"daily_tokens_per_agent": 1_000_000},
        "team": [
            {"id": "james", "name": "James", "role": "Delivery manager", "monitor": True},
            {"id": "sofia", "name": "Sofia", "role": "Frontend lead", "projects": ["site"]},
            {"id": "marcus", "name": "Marcus", "role": "Backend lead"},
        ],
        "projects": {"site": {"repo": ""}},
    }
    raw.update(over)
    return raw


@pytest.fixture
def cfg(tmp_path):
    return parse_config(make_raw(tmp_path), base_dir=tmp_path)


@pytest.fixture
def rt(cfg):
    return Runtime(cfg, FakeLLM(), bus=ConsoleBus(quiet=True))
