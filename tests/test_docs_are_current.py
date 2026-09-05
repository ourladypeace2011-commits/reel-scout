"""Documentation that names things must name things that exist.

🔴 **Why this file exists.** An audit on 2026-09-05 found `README.md` and
`README.zh.md` advertising an MCP tool called `get_transcript`. `git log -S`
returns nothing: **it has never existed**, in any version, since the MCP server
was written. It had been in the README for six weeks.

That was not the only one. `docs/commands.md` said "8 tools" against 19, listed
six `db` subcommands against eight and four export formats against six, and
omitted five whole CLI commands. `docs/roadmap.md` gave the version as v1.3.1
while `__version__` read 1.4.0, and stated the schema as v18 on one line and
v17 on the next -- **directly above a warning it had written itself about
exactly that mistake in the version before**. `SKILL.md` -- which ships inside
the wheel, under the heading "do **not** invent flags beyond these" -- was
missing `--force-keyframes`.

Every one of those is prose asserting something checkable, and nothing was
checking. Prose does not rot faster than code; it rots at the same rate and
nobody notices, because there is no failing test to notice with. So these
assertions are executable now: the docs are read, the values are derived from
the code, and a mismatch is a red test rather than a reader's wasted afternoon.

Deliberately narrow: only claims that are **enumerable from the code** are
checked here. Prose describing intent, tradeoffs or history is not, and should
not be -- a gate that demanded exact wording would be reformatted into
uselessness within a month.
"""
from __future__ import annotations

import argparse
import io
import os
import re
from typing import Dict, List, Set

import pytest

import reel_scout
from reel_scout import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    with io.open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _mcp_tool_names() -> Set[str]:
    src = _read("reel_scout", "mcp", "tools.py")
    return set(re.findall(r'"name":\s*"([a-z_][a-z0-9_]*)"', src))


def _cli_parser() -> argparse.ArgumentParser:
    """The real parser, built by running `main` with a patched `parse_args`."""
    from reel_scout import cli

    captured: Dict[str, argparse.ArgumentParser] = {}
    original = argparse.ArgumentParser.parse_args

    def grab(self, *a, **kw):
        captured.setdefault("p", self)
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = grab
    try:
        try:
            cli.main([])
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_args = original
    assert "p" in captured, "could not reach the CLI parser"
    return captured["p"]


def _subparsers(parser: argparse.ArgumentParser) -> Dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _flags(parser: argparse.ArgumentParser) -> Set[str]:
    out: Set[str] = set()
    for action in parser._actions:
        for opt in action.option_strings:
            if opt.startswith("--") and opt != "--help":
                out.add(opt)
    return out


# --- the tool that never existed --------------------------------------------

@pytest.mark.parametrize("doc", ["README.md", "README.zh.md",
                                 os.path.join("docs", "commands.md"),
                                 "SKILL.md"])
def test_every_mcp_tool_a_doc_names_actually_exists(doc):
    real = _mcp_tool_names()
    text = _read(doc)
    # Only backticked identifiers that look like tool names, and only those the
    # real set could plausibly contain -- this is a check for *wrong* names,
    # not a ban on writing prose.
    named = set(re.findall(r"`([a-z_][a-z0-9_]{3,})`", text))
    suspects = {n for n in named
                if n.startswith(("ingest_", "batch_", "list_", "show_", "get_"))}
    missing = sorted(suspects - real)
    assert not missing, (
        "%s names MCP tools that do not exist: %s (real: %s)"
        % (doc, missing, sorted(real)))


def test_the_tool_count_in_commands_md_is_the_real_count():
    text = _read("docs", "commands.md")
    m = re.search(r"(\d+) tools:", text)
    assert m, "docs/commands.md should state how many MCP tools there are"
    assert int(m.group(1)) == len(_mcp_tool_names())


def test_commands_md_lists_every_mcp_tool():
    text = _read("docs", "commands.md")
    block = text[text.index(" tools:"):]
    block = block[:block.index("\n\n")]
    listed = set(re.findall(r"`([a-z_][a-z0-9_]*)`", block))
    assert listed == _mcp_tool_names()


# --- the flag list that ships inside the wheel -------------------------------

def test_skill_md_enumerates_exactly_the_real_analyze_flags():
    # `SKILL.md` is force-included in the wheel and tells an agent "do **not**
    # invent flags beyond these". A list that is missing one is worse than no
    # list: it makes a real flag look invented.
    analyze = _subparsers(_cli_parser())["analyze"]
    text = _read("SKILL.md")
    block = text[text.index("Real flags"):]
    block = block[:block.index("\n\n> ")]
    listed = set(re.findall(r"^- `(--[a-z0-9\-]+)", block, re.MULTILINE))
    listed |= set(re.findall(r"` / `(--[a-z0-9\-]+)", block))
    assert listed == _flags(analyze), (
        "SKILL.md flag list drifted. missing=%s extra=%s"
        % (sorted(_flags(analyze) - listed), sorted(listed - _flags(analyze))))


# --- the subcommand tables ---------------------------------------------------

@pytest.mark.parametrize("command", ["db", "export"])
def test_commands_md_subcommand_tables_match_the_parser(command):
    subs = _subparsers(_cli_parser())
    text = _read("docs", "commands.md")
    if command == "db":
        m = re.search(r"`db \{([a-z0-9,\-]+)\}`", text)
        real = set(_subparsers(subs["db"]))
    else:
        m = re.search(r"`export --format ([a-z0-9\\|]+)`", text)
        action = [a for a in subs["export"]._actions if "--format" in a.option_strings]
        real = set(action[0].choices or [])
    assert m, "docs/commands.md should document `%s`" % command
    listed = set(re.split(r"[,|]", m.group(1).replace("\\", "")))
    assert listed == real, ("`%s` drifted. missing=%s extra=%s"
                            % (command, sorted(real - listed), sorted(listed - real)))


# --- the two numbers the roadmap kept contradicting itself about -------------

def test_the_roadmap_states_both_numbers_on_one_checked_line():
    """The current version and schema, read off the single line that claims them.

    🔴 **Scoped to one line, and that scoping is the design.** The paragraph
    around it quotes what *earlier* versions of this line wrongly said --
    "schema v9", "schema v15" -- because recording the mistake is the point of
    the note. A gate reading the whole block flags those quotations, which is a
    false alarm about a correct sentence, and false alarms are how gates get
    switched off. The first two versions of this test did exactly that.

    So there is one authoritative line, matched exactly, and everything around
    it is free prose. That split is what makes the check possible at all.
    """
    text = _read("docs", "roadmap.md")
    line = [ln for ln in text.split("\n") if ln.startswith("**目前版本**")]
    assert len(line) == 1, "there must be exactly one current-state line"
    m = re.search(r"\*\*目前版本\*\*：\*\*v([0-9.]+)\*\*.*?"
                  r"\*\*DB schema\*\*：\*\*v(\d+)\*\*", line[0])
    assert m, "the current-state line should give a version and a schema: %r" % line[0]
    assert m.group(1) == reel_scout.__version__
    assert int(m.group(2)) == db.SCHEMA_VERSION
