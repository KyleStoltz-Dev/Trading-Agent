import pytest

from app.services.conversations import normalize_session_name


def test_session_names_are_predictable_slugs() -> None:
    assert normalize_session_name("Gold NY Review") == "gold-ny-review"
    assert normalize_session_name("Daily 2026/07/23") == "daily-2026-07-23"


def test_empty_session_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="letters or numbers"):
        normalize_session_name("***")
