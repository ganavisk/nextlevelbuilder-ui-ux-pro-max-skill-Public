"""Phone numbers on a call: the caller ID, and numbers the caller says.

The voice model writes down what the caller said ("303-555-0199", "(303)
555 0199", "+44 20 7946 0958"). `to_e164` accepts that, and refuses what is
clearly a mishearing rather than texting a stranger. A North American number
must have ten digits with a valid area code and exchange (neither starts with
0 or 1); an international number must be given with its "+" and country code.

No dependency on a numbering-plan library: every tenant today is in North
America, and the agent reads the number back before using it.
"""

import re

# Spoken when the caller gave a number we can't use. Nothing is saved, so the
# agent can ask again and retry.
NUMBER_UNCLEAR = (
    "Sorry, I didn't get the whole number. Could you say it again, starting with the area code?"
)

_NANP = re.compile(r"^1?([2-9]\d{2})([2-9]\d{2})(\d{4})$")


def caller_id_e164(raw: str | None) -> str | None:
    """The caller ID if it is a usable number, else None (blocked or
    anonymous calls arrive empty or as a placeholder). The carrier already
    sends E.164, so it is taken as is: the strict checks in `to_e164` are for
    numbers a person *said*, where a dropped digit is the likely error."""
    if raw and re.fullmatch(r"\+\d{8,15}", raw.strip()):
        return raw.strip()
    return None


def to_e164(raw: str | None) -> str | None:
    """`raw` as an E.164 number ("+13035550199"), or None if it can't be one."""
    if not raw:
        return None
    text = raw.strip()
    digits = re.sub(r"\D", "", text)
    if text.startswith("+") and not text.startswith("+1"):
        # International: trust the country code the caller gave; E.164 allows
        # up to 15 digits, and fewer than 8 is never a whole number.
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    match = _NANP.match(digits)
    if match is None:
        return None
    return "+1" + "".join(match.groups())


def spoken_digits(e164: str) -> str:
    """A number as the agent should read it back: grouped single digits
    ("3 0 3, 5 5 5, 0 1 9 9"). Speech engines read "303" as "three hundred
    three"; separate digits are read one by one, which is what lets a caller
    catch a wrong one."""
    digits = e164.lstrip("+")
    if len(digits) == 11 and digits.startswith("1"):
        groups = [digits[1:4], digits[4:7], digits[7:]]
    else:
        groups = [digits[i : i + 3] for i in range(0, len(digits), 3)]
    return ", ".join(" ".join(group) for group in groups)
