# Progress: Live-chat feedback (0002)

**Status:** in_progress
**Merge policy:** auto-merge

**Test Postgres:** local container `bot-organizer-test-pg`,
`TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres`.
**Baseline:** 250 tests passing at `4833614`.

## Units
| Unit | Branch | Status | Commits | Verification | PR | Notes |
|------|--------|--------|---------|--------------|----|-------|
| S1 | 0002-S1-reply-formatting | complete | 364e053..HEAD | 5 defects fixed; tester PASS | — | word-boundary rule for emphasis |
| S2 | 0002-S2-reminders | complete | 29b1fb6..HEAD | 1 defect fixed; tester PASS | — | overdue repeats delivered one copy per poll |
| S3 | 0002-S3-list-details | complete | b625946..HEAD | tester PASS | — | table dropped at user's request; design example was self-contradictory |
| S4 | 0002-S4-private-replies | in_progress | — | — | — | |
| S5 | — | pending | — | — | — | |

## Findings to address
(none yet)

## Log
- 2026-08-27: Design merged (#25). Starting S1.
- 2026-08-27: S1 done. Subagent matched the story's behaviour table exactly;
  probing beyond it found 5 defects the table missed — a marker inside a word
  paired with the next one, so list_check_off became listcheckoff, file names
  and URLs were mangled, and 5*4 и 3*2 became 54 и 32. Added a word-boundary
  condition (CommonMark refuses intra-word _ emphasis for the same reason);
  corrected the story's table too. Suite: 275 passed.
- 2026-08-27: S2 done. The story said to advance a repeat by exactly one
  interval; the implementation matched it. Correct normally, wrong once
  anything falls behind — an overdue series delivered one missed copy per
  poll. Measured: worker down an hour with a 5-minute repeat = 12 stale
  messages; the bugs file's own 2025-07-20 mis-dating with a 30-minute repeat
  = 19352 messages over 13 days. Now skips missed occurrences while still
  stepping exactly one interval when merely late. Story corrected.
  Suite: 300 passed.
- 2026-08-27: S3 done. User asked to drop the table, so the HTML-sending task
  went with it — and with it the risk that one unescaped < in an item name
  makes Telegram drop the whole message. The implementer found a real
  contradiction in this design: the category rule was stated twice and the
  worked example showed the reverse order. It followed the rule and flagged
  it; example corrected. Known limitation accepted: an item name entirely
  wrapped in paired Markdown markers loses them (*звёздочка* -> звёздочка);
  everything else including 2*2, a_b_c and <тег> survives. Suite: 324 passed.
