"""What the real classifier chain actually makes of a group's own name.

Everything else about extraction is tested against a mocked `extract`, which
can only check our side of the contract: that the instruction says the right
thing and that the answer is post-processed correctly. The «ЛесаТуса» failure
lived on the other side — the instruction was well-formed, the chain answered,
and the answer was empty because nothing had told the model that a Russian
title is often one glued word. A mocked test cannot see that, and
`test_the_instruction_covers_a_glued_title` only notices if the paragraph is
deleted outright — not if it stops working, or if a model swap undoes it.

Skipped unless AI_PROXY_URL points at a reachable ai-proxy, so CI and the
normal suite are unaffected. The cluster runs on the homelab box and `k.sh`
drives kubectl there over SSH, so a port-forward started through it listens on
*that* machine — reaching it from here takes a tunnel as well:

    ssh -i ~/.ssh/id_ed25519_homelab admin@10.100.102.12 \
        'powershell -NoProfile -Command "kubectl port-forward svc/ai-proxy 8787:8787"' &
    ssh -i ~/.ssh/id_ed25519_homelab -N -L 8787:127.0.0.1:8787 admin@10.100.102.12 &
    AI_PROXY_URL=http://localhost:8787 uv run pytest tests/test_group_info_live.py

These are the only tests here that spend a real model call, and they are the
only ones that can fail because the chain changed rather than because the
code did. Treat a failure as a prompt to re-tune the instruction in
bot/group_info.py against the current chain, the way the glued-title
paragraph was tuned.
"""

import datetime as dt
import os

import pytest

import bot.group_info as group_info

pytestmark = pytest.mark.skipif(
    not os.environ.get("AI_PROXY_URL"),
    reason="AI_PROXY_URL not set — point it at ai-proxy to run the live checks",
)

# Fixed, so "17/9" resolves to one known answer instead of to whenever the
# suite happens to run.
TODAY = dt.date(2026, 10, 12)


def _starts(value, stem: str) -> bool:
    """Cheap stemming: the chain may inflect, and an inflected form is a
    wording question, not the failure these tests exist to catch."""
    return isinstance(value, str) and value.strip().casefold().startswith(stem)


def _is_cyrillic(value: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in value)


@pytest.mark.parametrize("title,activity,place", [
    # Observed live on 2026-10-11: answered, and answered with nothing — the
    # chain read it as one meaningless word, so the greeting said nothing and
    # the session that followed learnt nothing from the title.
    ("ЛесаТуса", "тус", "лес"),
    # The regression the first fix introduced: split correctly, then reported
    # as 'picnic' — an English label for a Russian group.
    ("ПикникНаМоре", "пикник", "мор"),
])
async def test_a_glued_title_is_split_into_its_parts(title, activity, place):
    event = await group_info.extract_event(title, None, TODAY)

    assert event["answered"], "the chain did not answer at all"
    assert _starts(event["activity_type"], activity), event
    assert _starts(event["place"], place), event
    assert _is_cyrillic(event["place"]), f"translated instead of reported: {event}"


async def test_a_place_and_a_date_glued_together_are_still_split():
    """«Море 17/9» — the title from the group where this last shipped
    working. The date must not end up inside `place`, and a bare 17/9 is
    day/month resolved forward, not 9 September of this year."""
    event = await group_info.extract_event("Море 17/9", None, TODAY)

    assert _starts(event["place"], "мор"), event
    assert not any(ch.isdigit() for ch in event["place"]), event
    assert event["event_date"] == "2027-09-17", event


async def test_a_name_that_names_no_place_stays_empty():
    """The other half of the glued-title rule: splitting words apart must not
    turn a group's own name into somewhere they are going. «Друзья» is a
    chat, not a destination."""
    event = await group_info.extract_event("Друзья", None, TODAY)

    assert event["answered"]
    assert event["place"] is None, event
    assert event["place_kind"] == group_info.NOT_A_PLACE, event
