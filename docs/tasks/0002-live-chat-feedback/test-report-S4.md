# Test report: S4 — Answering privately

**Design:** `docs/tasks/0002-live-chat-feedback/stories/S4-private-replies.md`
**Checked against:** branch `0002-S4-private-replies`
**Date:** 2026-08-27
**Verdict:** PASS

## Requirement checklist

**R5 — Answer me privately:** PASS

- *"the answer arrives as a DM to the person who asked, and the group sees at
  most a short acknowledgement"* — verified end to end: the send goes to
  `chat_id = <the asker's user id>`, and the group message is an
  acknowledgement rather than the content.
- *"if they have never started a chat with the bot, the bot says so in the
  group and tells them what to do — it does not silently drop the answer or
  claim to have sent it"* — `Forbidden` returns
  `{"status": "cannot_reach", ...}` and never raises. The instruction requires
  the group message to mention starting a chat **and** to withhold the private
  text; a test asserts both, including that the private string is absent.
- *"an ordinary question is still answered in the group"* — the existing tool
  loop is untouched; nothing about the normal path changed.

## The security property, verified rather than assumed

The recipient is bound by the router and is not a model-visible parameter.
That was the point of this story, so it was checked adversarially rather than
by reading the declaration:

```
1. модель видит параметры: ['text']
2. модель просила отправить 999999, ушло -> 111 (ПЕРЕХВАЧЕНО)
```

A tool call carrying `current_user_id=999999` and `session_id=4242` — the
shape a hallucination or a prompt injection would take — was overwritten with
the real asker's id and the real session. This is the third time this class of
hole has been closed in this project: `chat_id` on `reminder_set` and
`broadcast_message` in `0001`'s S4, then on `send_location` and
`archive_lookup` in S6. A model that could choose the recipient could DM anyone
in any chat the bot has ever seen.

## Why the Forbidden path is the load-bearing one

Telegram refuses to DM anyone who has never opened a private chat with the
bot. That is the **normal** state of most group members, not an edge case —
`0001`'s S4 found it the hard way, where an unhandled `Forbidden` aborted an
entire nudge run partway through.

So the failure is not merely caught, it is answered: the group is told the
message could not be delivered and what to do about it. The alternative
behaviours are both worse than the failure — silently dropping the answer
leaves the person waiting for a DM that will never arrive, and pasting the
content into the group is precisely what they were trying to avoid.

## QA note

A message with no `from_user` — a channel post — returns
`{"status": "failed", "detail": "no telegram user to message"}` without
attempting a send. Worth knowing that this is indistinguishable from a
transport failure in the return value; the log tells them apart.

## Verification method

The tool ran against a real PostgreSQL with Telegram mocked, including the
adversarial recipient-override check above. Suite: **335 passed**, up from 324.

Not verified live: whether the model actually calls this tool when someone
says "пошли в личку" rather than answering in the group anyway, and whether it
honours the instruction not to repeat the content after a `cannot_reach`. Both
are model behaviour and need a real chat — the second one is the one that
matters, because getting it wrong leaks exactly what the feature exists to
keep private.
