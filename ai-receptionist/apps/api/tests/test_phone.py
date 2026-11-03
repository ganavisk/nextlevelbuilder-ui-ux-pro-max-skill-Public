"""Numbers a caller says, turned into numbers we can text, and read back."""

import pytest

from app.core.phone import caller_id_e164, spoken_digits, to_e164


@pytest.mark.parametrize(
    "said",
    [
        "303-555-0199",
        "(303) 555 0199",
        "303.555.0199",
        "3035550199",
        "1 303 555 0199",
        "+1 303 555 0199",
    ],
)
def test_the_ways_a_north_american_number_is_written_down(said: str) -> None:
    assert to_e164(said) == "+13035550199"


@pytest.mark.parametrize(
    "said",
    [
        "555-0199",  # no area code
        "303-555-019",  # a dropped digit
        "303-555-01999",  # an extra one
        "103-555-0199",  # area codes never start with 1
        "303-055-0199",  # nor do exchanges
        "",
        "call me back",
    ],
)
def test_a_misheard_number_is_refused_not_texted(said: str) -> None:
    assert to_e164(said) is None


def test_an_international_number_keeps_its_country_code() -> None:
    assert to_e164("+44 20 7946 0958") == "+442079460958"
    assert to_e164("+44 20") is None  # too short to be whole


def test_the_caller_id_is_trusted_as_the_carrier_sent_it() -> None:
    assert caller_id_e164("+15550001111") == "+15550001111"
    assert caller_id_e164("") is None  # blocked caller ID
    assert caller_id_e164("anonymous") is None


def test_a_number_is_read_back_one_digit_at_a_time() -> None:
    """ "303" is read as "three hundred three"; spaced digits are read one by
    one, which is what lets a caller catch a wrong digit."""
    assert spoken_digits("+13035550199") == "3 0 3, 5 5 5, 0 1 9 9"
