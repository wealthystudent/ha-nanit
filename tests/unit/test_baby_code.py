"""The per-baby code: a fixed hash that must never drift."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant

from aionanit.models import Baby
from custom_components.nanit.baby_code import BABY_CODE_PREFIX, baby_code
from custom_components.nanit.coordinator import NanitCareLogCoordinator

# Synthetic uids and their codes, worked out once with sha256sum:
#   printf 'ha-nanit/baby-code/v1\0baby_test_0001' | sha256sum | cut -c1-32
# If one of these changes, every calendar and statistic built on the code moves.
VECTORS = {
    "baby_test_0001": "71dc386253a3e9dd0777854a0c04fc5a",
    "baby_1": "065aa64db3013a5d186dcef038a36890",
    "bébé": "9effe2205b4ca2dc07384195a1214ea1",
}


@pytest.mark.parametrize(("uid", "code"), VECTORS.items())
def test_the_code_of_a_fixed_uid_never_changes(uid: str, code: str) -> None:
    assert baby_code(uid) == code


def test_the_input_encoding_is_the_documented_one() -> None:
    assert BABY_CODE_PREFIX == b"ha-nanit/baby-code/v1\x00"
    uid = "baby_test_0001"
    expected = hashlib.sha256(b"ha-nanit/baby-code/v1\x00baby_test_0001").hexdigest()[:32]
    assert baby_code(uid) == expected


def test_the_prefix_makes_the_code_specific_to_this_use() -> None:
    plain = hashlib.sha256(b"baby_test_0001").hexdigest()[:32]
    assert baby_code("baby_test_0001") != plain


def test_the_code_is_32_lowercase_hex_characters_and_hides_the_uid() -> None:
    code = baby_code("baby_test_0001")
    assert len(code) == 32
    assert set(code) <= set("0123456789abcdef")
    assert "baby" not in code


def test_different_uids_get_different_codes() -> None:
    assert len({baby_code(uid) for uid in ("a", "b", "baby_1", "baby_2", "A")}) == 5


def test_any_string_has_a_code() -> None:
    # A lone surrogate can come out of a JSON answer; it still has a code.
    assert len(baby_code("\ud800")) == 32


def test_an_empty_uid_has_no_code() -> None:
    with pytest.raises(ValueError):
        baby_code("")


def test_the_care_log_reader_is_named_by_the_code(hass: HomeAssistant) -> None:
    baby = Baby(uid="baby_test_0001", name="Nursery", camera_uid="cam_1")
    reader = NanitCareLogCoordinator(hass, MagicMock(), MagicMock(), baby)
    assert reader.name == "nanit_71dc386253a3e9dd0777854a0c04fc5a_care_logs"
    assert baby.uid not in reader.name


def test_the_display_name_fallback_holds_no_part_of_the_uid() -> None:
    from custom_components.nanit.baby_code import baby_display_name

    assert baby_display_name("Nursery", "baby_test_0001") == "Nursery"
    assert baby_display_name("<b>Nursery</b>", "baby_test_0001") == "Nursery"
    assert baby_display_name("", "baby_test_0001") == "Baby 71dc3862"
    assert baby_display_name(None, "baby_test_0001") == "Baby 71dc3862"
