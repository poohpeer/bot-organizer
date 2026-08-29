"""The generator that turns docs/manual-tests.md into a run checklist.

Tested because it parses a document people write by hand, and the failure it
must never have is silently dropping a case: a missing checkbox is a check
nobody does, and nothing downstream would notice.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.manual_test_checklist import main, parse, render  # noqa: E402

SOURCE = Path(__file__).resolve().parents[1] / "docs" / "manual-tests.md"

SAMPLE = """# Manual test plan

Preamble that mentions **bold text** and is not a case.

## 0. Before you start

| a | b |
|---|---|

## 1. Commands and permissions

**1.1 `/admin` as an administrator** — the menu opens.

**1.2 A member presses a button** — Expected: an alert, and
nothing changes.
*Why not automatable:* the test mocks it, so it asserts our branch.

## 2. Private chat

**2.1 `/status` in a DM** — the report arrives privately.

## Recording a run

One issue per run.
"""


def test_every_numbered_case_becomes_one_checkbox():
    checklist = render(parse(SAMPLE))

    assert checklist.count("- [ ] ") == 3
    assert "**1.1**" in checklist and "**1.2**" in checklist and "**2.1**" in checklist


def test_the_setup_section_is_not_a_case():
    """Section 0 is what you need before you start, not something to tick."""
    assert "0. Before you start" not in render(parse(SAMPLE))


def test_a_section_with_no_numbered_cases_is_left_out():
    assert "Recording a run" not in render(parse(SAMPLE))


def test_an_expectation_wrapped_over_two_lines_survives_whole():
    """People wrap prose. A checklist that stopped at the first newline would
    cut half the expectations mid-sentence."""
    checklist = render(parse(SAMPLE))

    assert "Expected: an alert, and nothing changes" in checklist


def test_the_maintainer_aside_is_left_out():
    """"Why not automatable" is a note to whoever edits the list, not an
    instruction to whoever runs it."""
    checklist = render(parse(SAMPLE))

    assert "mocks it" not in checklist
    assert "**1.2**" in checklist, "the case itself still has to be there"


def test_sections_keep_their_order_and_headings():
    checklist = render(parse(SAMPLE))

    assert checklist.index("### 1. Commands and permissions") < checklist.index("### 2. Private chat")


def test_only_the_sections_asked_for():
    checklist = render(parse(SAMPLE), wanted={2})

    assert "**2.1**" in checklist
    assert "**1.1**" not in checklist


def test_bold_text_outside_a_section_is_not_mistaken_for_a_case():
    assert "bold text" not in render(parse(SAMPLE))


def test_a_document_with_no_cases_produces_nothing():
    assert render(parse("# Nothing here\n\nJust prose.\n")).strip() == ""


def test_a_very_long_case_is_cut_rather_than_wrapped():
    long_one = "## 1. S\n\n**1.1 T** — " + ("word " * 200) + ".\n"

    line = render(parse(long_one)).splitlines()[1]

    assert len(line) <= 240 and line.endswith("…")


# --- against the real document -------------------------------------------

def test_the_real_plan_parses_into_cases():
    """A rename of a heading, or a case written a new way, must not quietly
    empty the checklist."""
    sections = parse(SOURCE.read_text(encoding="utf-8"))
    cases = [case for section in sections for case in section.cases]

    assert len(cases) >= 40, "the plan has ~49 cases; far fewer means the parser stopped seeing them"
    assert all(case.title for case in cases), "a checkbox with no text is unusable"


def test_every_case_id_is_unique_in_the_real_plan():
    """Two cases sharing an id makes a run ambiguous to report on."""
    ids = [case.id for section in parse(SOURCE.read_text(encoding="utf-8")) for case in section.cases]

    assert len(ids) == len(set(ids)), f"duplicate ids: {sorted({i for i in ids if ids.count(i) > 1})}"


def test_a_case_id_matches_the_section_it_is_in():
    """A copy-paste that leaves 3.4 sitting in section 4 sends whoever filters
    by section to the wrong place."""
    for section in parse(SOURCE.read_text(encoding="utf-8")):
        for case in section.cases:
            assert case.id.split(".")[0] == str(section.number), \
                f"case {case.id} is in section {section.number}"


# --- the command line -----------------------------------------------------

def test_an_empty_result_is_an_error_not_an_empty_run(tmp_path, capsys):
    """Loudly. An empty checklist would open a run issue that asks nobody to
    check anything, and look like a completed run."""
    empty = tmp_path / "empty.md"
    empty.write_text("# Nothing\n")

    assert main([str(empty)]) == 1
    assert "No cases found" in capsys.readouterr().err


def test_it_imports_nothing_outside_the_standard_library():
    """The workflow calls it with plain `python`, before anything is
    installed, so a single non-stdlib import breaks opening a run — not a
    test run, the actual button.

    Checked by reading the imports rather than by running the file: running
    it here uses the project's own interpreter, which has every dependency
    installed and would happily import asyncpg. That version of this test
    passed a sabotage run that added `import asyncpg` to the script.
    """
    import ast

    source = (SOURCE.parents[1] / "scripts" / "manual_test_checklist.py").read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    outside = imported - sys.stdlib_module_names
    assert not outside, f"the run workflow has no venv: {sorted(outside)}"


def test_it_runs_as_a_script():
    result = subprocess.run(
        [sys.executable, "scripts/manual_test_checklist.py", str(SOURCE)],
        capture_output=True, text=True, cwd=SOURCE.parents[1],
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("- [ ] ") >= 40
