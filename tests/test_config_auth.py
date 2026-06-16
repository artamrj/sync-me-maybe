from __future__ import annotations

import pytest

from sync_me_maybe.auth import is_allowed
from sync_me_maybe.config import ConfigError, parse_user_ids


def test_parse_user_ids_accepts_commas_semicolons_and_spaces() -> None:
    assert parse_user_ids("123, 456;789") == {123, 456, 789}


def test_parse_user_ids_rejects_invalid_values() -> None:
    with pytest.raises(ConfigError):
        parse_user_ids("123, nope")


def test_is_allowed_requires_known_user_id() -> None:
    assert is_allowed(123, {123})
    assert not is_allowed(456, {123})
    assert not is_allowed(None, {123})
