"""Config validation."""
from pathlib import Path

import pytest

from src.config import Settings, Watchlist, load_settings, load_watchlist


def test_watchlist_loads_from_repo():
    w = load_watchlist(Path(__file__).parent.parent / "config/watchlist.yaml")
    assert len(w.companies) == 30
    assert len(w.people) == 39
    assert len(w.podcasts) == 3


def test_settings_loads_from_repo():
    s = load_settings(Path(__file__).parent.parent / "config/settings.yaml")
    assert s.schedule.timezone == "America/Chicago"
    assert s.budgets.whisper_wallclock_minutes == 60
    assert s.transcript.whisper_model == "base.en"


def test_company_all_names_includes_aliases():
    w = load_watchlist(Path(__file__).parent.parent / "config/watchlist.yaml")
    by_name = {c.name: c for c in w.companies}
    assert "SSI" in by_name["Safe Superintelligence"].all_names
    assert "Cursor" in by_name["Anysphere"].all_names


def test_empty_people_rejected():
    with pytest.raises(ValueError):
        Watchlist.model_validate({
            "companies": [],
            "people": [],
            "podcasts": [],
        })


def test_settings_defaults_apply():
    s = Settings.model_validate({})
    assert s.budgets.whisper_wallclock_minutes == 60.0
    assert s.budgets.assumed_whisper_rtf == 8.0
