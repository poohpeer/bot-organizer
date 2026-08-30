"""Turn docs/manual-tests.md into a checklist for one run.

Generated rather than copied. A second copy of the cases — in an issue
template, a project board, a spreadsheet — starts drifting the moment someone
edits a handler, and a stale manual test is worse than none: it sends a
person to check behaviour that no longer exists. This way the checklist is
whatever the file said at the moment the run was opened, always.

The parser is deliberately forgiving. It reads a document people write by
hand, and the failure it must never have is silently dropping a case: a
missing checkbox is a check nobody does. Anything it cannot parse is left out
loudly — see `main`, which refuses to print an empty list.

    python scripts/manual_test_checklist.py [--sections 3,4] [docs/manual-tests.md]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SOURCE = Path("docs/manual-tests.md")

# "## 3. Location and links" — the number is what a caller filters on, so a
# section without one (## Recording a run) is not a section of cases.
_SECTION = re.compile(r"^##\s+(\d+)\.\s+(.*?)\s*$")

# "### 3.1 A pin becomes the place" — one case, one heading. The body under
# it is Given/When/Then; the checkbox carries the title and the Then, which
# is what a person ticks against.
_CASE = re.compile(r"^###\s+(\d+\.\d+)\s+(.+?)\s*$")

# The only line of a case that belongs on the checkbox. Given and When are
# instructions you follow with the document open; Then is the thing you are
# deciding about, so it is what a one-line reminder has to carry.
_THEN = re.compile(r"^\s*-\s+\*\*Then\*\*\s+(.*)$")

# An aside to whoever maintains the list, not an instruction to whoever runs
# it: why a case cannot be automated. It ends the case's Then, so it is a
# terminator rather than something to cut out — stripping only the label left
# the explanation dangling on the end of the checkbox.
_ASIDE_OPENER = re.compile(r"^\s*-\s+\*\*Why not automatable\*\*", re.IGNORECASE)

# Section 0 is setup, not cases. Excluded by default rather than deleted from
# the document, where it belongs.
SETUP_SECTION = 0

_LINE_LIMIT = 240


@dataclass
class Case:
    id: str
    title: str
    detail: str = ""

    def render(self) -> str:
        line = f"- [ ] **{self.id}** {self.title}"
        if self.detail:
            line += f" — {self.detail}"
        return line if len(line) <= _LINE_LIMIT else line[: _LINE_LIMIT - 1].rstrip() + "…"


@dataclass
class Section:
    number: int
    title: str
    cases: list[Case] = field(default_factory=list)


def _tidy(text: str) -> str:
    """One line of prose out of however many the paragraph took."""
    return re.sub(r"\s+", " ", text).strip(" —–-.,;:")


def parse(markdown: str) -> list[Section]:
    """Every numbered case, grouped by the section it sits in.

    A case's Then runs to the next blank line or to the "why not automatable"
    aside, so an expectation split over several lines survives whole — people
    wrap prose, and a checklist that stopped at the first newline would cut
    half of them mid-sentence.
    """
    sections: list[Section] = []
    pending: Case | None = None
    collecting: list[str] = []

    def flush() -> None:
        nonlocal pending, collecting
        if pending is not None:
            pending.detail = _tidy(" ".join(collecting))
            sections[-1].cases.append(pending)
        pending, collecting = None, []

    for raw in markdown.splitlines():
        line = raw.rstrip()

        section = _SECTION.match(line)
        if section:
            flush()
            sections.append(Section(int(section.group(1)), section.group(2)))
            continue

        case = _CASE.match(line)
        if case and sections:
            flush()
            pending = Case(id=case.group(1), title=_tidy(case.group(2)))
            collecting = []
            continue

        if pending is None:
            continue
        if _ASIDE_OPENER.match(line):
            flush()
        elif collecting:
            # Already inside the Then; keep going until the paragraph ends.
            if line.strip():
                collecting.append(line)
            else:
                flush()
        else:
            then = _THEN.match(line)
            if then:
                collecting.append(then.group(1))

    flush()
    return sections


def render(sections: list[Section], wanted: set[int] | None = None) -> str:
    out: list[str] = []
    for section in sections:
        if section.number == SETUP_SECTION and (wanted is None or SETUP_SECTION not in wanted):
            continue
        if wanted is not None and section.number not in wanted:
            continue
        if not section.cases:
            continue
        out.append(f"### {section.number}. {section.title}")
        out.extend(case.render() for case in section.cases)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _sections_argument(value: str) -> set[int]:
    return {int(part) for part in re.split(r"[,\s]+", value.strip()) if part}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--sections", type=_sections_argument, default=None,
        help="only these section numbers, e.g. 3,4. All of them by default.",
    )
    args = parser.parse_args(argv)

    sections = parse(args.source.read_text(encoding="utf-8"))
    checklist = render(sections, args.sections)

    if not checklist.strip():
        # Loudly, not quietly. An empty checklist would open a run issue that
        # asks nobody to check anything, and look like a completed run.
        print(f"No cases found in {args.source}", file=sys.stderr)
        return 1

    print(checklist, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
