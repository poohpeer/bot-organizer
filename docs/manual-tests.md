# Manual test plan

Everything here is a check the automated suite **cannot** make. That is the
only entry criterion: if a test could assert it, it belongs in `tests/`, not
in this file.

Three things put a check on this list.

1. **The real thing is mocked in tests.** Telegram, the model chain, the
   clock, the cluster. A test asserts what we *send*, never what Telegram
   *does* with it.
2. **The check is about judgement.** Whether the model picks the right tool
   or the right category is not a fact a test can pin down — a test can only
   assert what happens once a particular answer has been faked.
3. **The check is about appearance.** Whether a link is tappable, whether a
   report is readable on a phone, whether an emoji renders. Strings are
   testable; the thing a person sees is not.

Run the whole list after a change that touches routing, rendering, or the
model chain. Run the section for whatever changed otherwise.

**Two accounts are needed** — several cases turn on being a member or an
administrator, and you cannot be both people at once. A second Telegram
account in the same group is the cheapest way.

---

## 0. Before you start

| | |
|---|---|
| A group with the bot in it, with an **active session** | `@bot организуем пикник` |
| A **second account** in the same group, not an admin | for the permission cases |
| A private chat with the bot, `/start` pressed | a bot cannot DM someone who never started it |

---

## 1. Commands and permissions

Telegram's `get_chat_member` behaves differently in a group, a supergroup and
a channel, and every test mocks it. This section is the only thing that
exercises the real answer.

**1.1 `/admin` as an administrator** — the menu opens.

**1.2 `/admin` as an ordinary member** (second account) — «Настройки доступны
администраторам чата», and no keyboard.

**1.3 A member presses a button on a menu an admin opened.** The keyboard
stays live in the chat, so anyone can press it. Expected: an alert saying
it is for administrators, and nothing changes.
*Why not automatable:* the test mocks `get_chat_member`, so it asserts our
branch, not Telegram's answer.

**1.4 Demote yourself, then press a button on a menu you opened while admin.**
Expected: refused. This is the case the "asked every time, never cached"
rule exists for.

**1.5 `/status` and `/list` in the group** — the report and the list, posted
in the chat, any member.

**1.6 Upgrade the group to a supergroup** (add ~200 members, or convert in
settings). **The chat_id changes.** Expected: the session does not follow.
*Why it matters:* this is unrecoverable and silent. Decide what should
happen before it happens to a real event.

---

## 2. Private chat

**2.1 `/status` in a DM, one active event** — the report arrives privately
and **nothing appears in the group**. Check the group.

**2.2 `/list` in a DM** — the list only, no participants, no reminders.

**2.3 Two active events** (a second group with a session) — buttons appear,
labelled with the chat titles. Press one: the right event's report.

**2.4 Press a `/list` button and confirm you get the list, not the report.**
The verb rides in the callback data; a person who asked for the list must not
get the whole report.

**2.5 Leave the group, then `/status` in the DM.** Expected: «Я не нашёл
активных мероприятий». `decision_log` still remembers you spoke there —
being in it is not permission.
*Why not automatable:* the membership answer is Telegram's.

**2.6 A person who never pressed `/start` in the DM.** Ask the bot in the
group to nudge participants. Expected: the bot cannot DM them, and this does
not break the nudge for everyone else. Watch the log for `Forbidden`.

---

## 3. Location and links

**3.1 Drop a pin** (attach → location → send this location). Expected: place
set, no reply, `/status` shows `🔗 Map` under the name, and the link opens
the right spot.

**3.2 Share a place from Telegram's picker** (a named venue). Expected: the
name becomes the place.

**3.3 A pin when a place is already named, not addressed to the bot.**
Expected: **ignored**, place unchanged. Check the log says so.

**3.4 The same, but reply to the bot with the pin.** Expected: taken.

**3.5 Live location.** Not handled distinctly today: it arrives as an
ordinary pin and is taken once. The updates that follow arrive as
`edited_message`, which the bot does not read at all.
*Decide whether that is acceptable.* If it is, say so here; if not, it is a
change, not a test.

**3.6 A forwarded location** from another chat. Expected: same as a pin —
confirm that is what you want.

**3.7 Send a Waze link.** Expected: «Добавил ссылку на место», no question
asked, and no delay for a maps lookup.

**3.8 Send a short Google Maps link** (`maps.app.goo.gl/…`) — the case that
produced «Не смог открыть эту короткую ссылку». Expected: taken silently.

**3.9 Send a link with text: «давайте лучше сюда <link>».** Expected: the
link is stored, the place **keeps its name**.

**3.10 Tap `🔗 Map` in `/status`** on both iOS and Android. Expected: opens
the map app, points at the right place.
*Why not automatable:* a test asserts the anchor's HTML; whether Telegram
renders it as a tappable link is Telegram's business.

**3.11 A place name containing `<` or `&`** — set it, then `/status`.
Expected: shown literally, message not dropped. This is what the escaping
exists for, and a dropped message is exactly what it prevents.

---

## 4. What the model does

Every test fakes the model's answer. Nothing below can be asserted, only
observed.

**4.1 «добавь бутылку пива»** when the list has 2 бут. Expected: «пиво: было
2 бут., стало 1 бут.» — *not* the whole list, and not silence. The number is
wrong on purpose: the point is that a replacement announces itself.

**4.2 «добавь ещё бутылку пива».** Expected: refused with the amount that is
there and a request for the new total.

**4.3 «должно быть две бутылки пива».** Expected: applied.

**4.4 «покажи список».** Expected: the rendered list, character for
character — not the model's own summary.

**4.5 Ask a question, answer the bot's reply, check it understood.**
"сколько всего?" → "две" must not produce "чего именно две?". This is what
conversation history exists for, and it broke twice.

**4.6 Rename the chat to `<место> <дата>`** — e.g. «Море 20/11». Expected:
place and date updated within a minute, **no message in the chat**.

**4.7 Rename the chat to something that is not a place** — «Друзья».
Expected: nothing recorded, nothing said.
*Why not automatable:* whether the model answers `place_kind: нет` is
judgement.

**4.8 A bare date in the title: `31/8`.** Expected: 31 August of the
**next** occurrence, not a past year.

**4.9 Say something that needs no reply** — «ок, понял». Expected: silence,
not «Записал.»

---

## 5. The model chain

**5.1 Exhaust Groq** (send many messages quickly, or set the chain to
`gemini` only in `/admin` and wait for a 429). Expected: the chain moves on
and the answer still arrives.

**5.2 Put `codex:` first in `/admin`, then ask something needing a tool** —
«покажи список». Expected: an answer built from the real list, within about
ten seconds. This is the only way to see the CLI providers serving a tool
turn over MCP; in normal traffic Groq answers first and they are never
reached.

**5.3 Turn every model off in `/admin`.** Expected: the chain falls back to
the deployment default and the bot still answers — an empty selection is not
a broken bot.

**5.4 Reorder the chain and watch the log.** Expected: the new order is used
on the very next message.

---

## 6. Time

Nothing here can be faked; the clock has to actually pass.

**6.1 Set a reminder for two minutes from now.** Expected: it arrives, once,
at the right local time.

**6.2 A repeating reminder with an end** — «каждые 2 минуты до <время>».
Expected: repeats, then stops **exactly** at that time.

**6.3 Cancel a repeating reminder** — expected: no further occurrences.

**6.4 A reminder before the group's timezone is known.** Expected: the bot
says it assumed a zone and asks; after `set_timezone`, already-scheduled
reminders are corrected.

**6.5 Leave the chat silent past the auto-close window.** Expected: the
closing question, then the session closes with a summary.

---

## 7. Rendering on a real client

**7.1 A list of 40+ items.** Expected: readable, and **the message actually
arrives**.
*Known gap:* nothing splits a message at Telegram's 4096-character limit. A
long enough report will be rejected by the API and the group sees nothing.
This case is here to find out where that line falls in practice.

**7.2 Item names with emoji, brackets, quotes.** Expected: shown as typed.

**7.3 A reply where the model wrote Markdown** — `**жирный**`. Expected:
plain text, no stray asterisks.

**7.4 The full `/status` on a narrow phone screen.** Expected: the place name
readable at a glance, the `🔗 Map` line not wrapping into it.

---

## 8. Cluster

**8.1 Restart the bot pod.** Expected: the session, list and settings survive
— they are in Postgres, not memory.

**8.2 Restart the Postgres pod.** Expected: same. The PVC is the thing being
checked.

**8.3 Redeploy after a codex/claude login.** Expected: the CLIs are still
logged in. Their credentials live on a mounted volume precisely because
`/root` is wiped on every redeploy.

**8.4 Stop and start the Docker Desktop cluster.** Expected: data survives.
*Not the same as 8.2:* `local-path` PVC data lives inside the kind node
container, so it survives a stop/start and is **lost if that container is
recreated**. Worth knowing before it happens.

---

## Recording a run

**Actions → Manual test run → Run workflow.** It opens an issue whose
checklist is generated from this file, one checkbox per case, grouped by
section. Two optional inputs: a note for the title ("after #73") and a
section filter ("3,4") when only part of it is worth running.

Tick as you go. Anything that fails gets its own issue, linked from the line.

The checklist is generated, never copied, so it is always whatever this file
said at the moment the run was opened. Add a case in a PR and the next run
has it.

**Why the cases live here and not in a test-management tool.** They change
with the code — case 3.5 exists because live location is unhandled *today* —
so they belong in the same commit and the same review as the change that
moves them. A copy in Drive or TestRail drifts the moment someone edits a
handler, and a stale manual test is worse than none: it sends a person to
check behaviour that no longer exists.

Run *results* are a different thing and can live wherever is convenient. A
GitHub issue per run costs nothing and needs no new account. If you ever want
real run history — pass/fail per case over time, who ran it, on which build —
Qase or Testomat have free tiers that do that properly. That is worth adding
when there is a second person running these, and not before.
