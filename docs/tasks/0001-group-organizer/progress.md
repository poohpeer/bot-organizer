# Progress: Group organizer bot (0001)

**Status:** in_progress
**Merge policy:** auto-merge

**Test Postgres:** local Docker container `bot-organizer-test-pg`,
`TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres`.

## Units
| Unit | Branch | Status | Commits | Verification | PR | Notes |
|------|--------|--------|---------|--------------|----|-------|
| S1 | 0001-S1-data-model | in_progress | d6416ad..a550ead | review clean (2 findings fixed) | — | tester pending |
| S2 | — | pending | — | — | — | |
| S3 | — | blocked | — | — | — | waits on S1 |
| S4 | — | blocked | — | — | — | waits on S1, S2 |
| S5 | — | blocked | — | — | — | waits on S2 |
| S6 | — | blocked | — | — | — | waits on S1, S5 |
| S7 | — | blocked | — | — | — | waits on S1, S2, S3 |
| S8 | — | blocked | — | — | — | waits on S1, S3 |
| S9 | — | blocked | — | — | — | waits on S2, S3, S4, S5, S6, S7 |
| S10 | — | blocked | — | — | — | waits on S8, S9 |

## Findings to address
(none yet)

## Log
- 2026-08-24: Design merged to main. Test Postgres started. Beginning S1.
