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
| S2 | — | next | — | — | — | |
| S3 | — | pending | — | — | — | |
| S4 | — | pending | — | — | — | |
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
