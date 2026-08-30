#!/usr/bin/env python3
"""
propagate_nbc_to_children.py
============================

Post-processing step that runs on the enriched report (enriched_report_llm.txt)
**after** compatibility tagging and **before** the compatibility-list generation.

Problem it solves
-----------------
Two related semantic issues are fixed here:

1. FLAG-MODE ALL-BC PROPAGATION (original behaviour)
   When the flag mode (RFC 7950 strict) classifies a structural entry as
   <non-backward-compatible> but ALL of its attribute/constraint children are
   tagged <backward-compatible>, the downstream list generators see the parent
   as NBC but the children as BC.  This causes the final concise_final_report.json
   to split the change into separate BC and NBC entries, inflating the total count
   relative to the default (non-flag) mode.

   Fix: For every NBC parent where ALL children are BC → rewrite each child tag
   to <non-backward-compatible>.

2. SEMANTIC DELETION INHERITANCE (new behaviour)
   When a structural node is **deleted** (e.g. ``type deleted``, ``leaf deleted``),
   the deletion itself is always <non-backward-compatible> per schema-node-rule3.
   However, individual child attributes/constraints are tagged by their own rules
   in isolation — e.g. ``constraint deleted: ['pattern']`` is tagged
   <backward-compatible> because relaxing a pattern constraint is BC in isolation.
   This is technically correct per the rules but semantically misleading: the
   entire node is gone, so the child verdicts are irrelevant noise.

   Fix: For every parent entry whose **action is "deleted"** and whose tag is
   <non-backward-compatible>, rewrite ALL children (BC or NBC) to inherit the
   parent's tag.  This applies regardless of whether some children are already NBC.
   The ``unmarked`` section is intentionally left untouched.

   Note: The parent tag is inherited as-is (not hardcoded to NBC), so if a future
   rule ever marks a deletion as BC, the children would inherit BC too.

Algorithm
---------
A **two-pass** approach that preserves every line verbatim (including multi-line
continuation lines) and only rewrites the compatibility tag token on child lines:

  Pass 1 – collect blocks:
    Scan the file and group lines into logical blocks.  A block starts at a
    path-header line and ends just before the next path-header line.  Within
    a block, child lines are identified by their dotted index prefix (N.M…).
    Continuation lines (wrapped description text) are kept attached to the
    child or parent line they follow.

  Pass 2 – apply propagation:
    Rule A (deletion inheritance): if parent action is "deleted" and parent tag
      is NBC → rewrite ALL children to inherit parent tag.
    Rule B (all-BC propagation): if parent is NBC and ALL children are BC →
      rewrite children to NBC.
    All other lines (continuation, blank, etc.) are emitted unchanged.

Usage
-----
As a library function (called from cli.py):

    from yang_rag.comparator.report.propagate_nbc_to_children import propagate_nbc

    propagate_nbc(input_path, output_path)   # in-place: input_path == output_path is fine

As a standalone CLI tool:

    python -m yang_rag.comparator.report.propagate_nbc_to_children \\
        --input  output/enriched_report_llm.txt \\
        --output output/enriched_report_llm.txt   # overwrite in-place

Environment variables (mirrors the other report scripts):

    YANG_ENRICHED_FILE   – path to the enriched report (default: output/enriched_report_llm.txt)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Tag constants
# ---------------------------------------------------------------------------
_NBC_TAG = "non-backward-compatible"
_BC_TAG  = "backward-compatible"

# ---------------------------------------------------------------------------
# Line-classification regexes
# ---------------------------------------------------------------------------

# Path header: starts at column 0, non-whitespace, not a numbered entry
_PATH_LINE_RE = re.compile(r"^[^\s]")

# Parent entry:  "  N. (keyword action) [...] <tag>"
# Index is a single integer followed by a dot (e.g. "1.", "12.")
_PARENT_RE = re.compile(
    r"^(?P<indent>\s+)"
    r"(?P<index>\d+\.)\s+"
    r"\((?P<change_type>[^)]+)\)"   # (keyword action) — capture for deletion check
    r".*"
    r"<(?P<tag>[^>]+)>"
)

# Detect "deleted" action in the change_type group, e.g. "type deleted", "leaf deleted"
_DELETED_ACTION_RE = re.compile(r"\bdeleted\b", re.IGNORECASE)

# Child entry:  "     N.M[.K…] attribute/constraint action: ..."
# Index has at least two dotted components (e.g. "1.1", "9.1.1", "5.2")
_CHILD_RE = re.compile(
    r"^(?P<indent>\s+)"
    r"(?P<index>\d+(?:\.\d+){1,}\.?)\s+"
    r"(?:attribute|constraint)\s+"
    r".*"
    r"<(?P<tag>[^>]+)>"
)

# Locate the LAST <tag> occurrence in a line (for rewriting)
_LAST_TAG_RE = re.compile(r"<([^>]+)>(?!.*<[^>]+>)")


def _classify(line: str) -> str:
    """Return 'path', 'parent', 'child', or 'other'."""
    stripped = line.rstrip("\n")
    if stripped and _PATH_LINE_RE.match(stripped):
        return "path"
    if _PARENT_RE.match(stripped):
        return "parent"
    if _CHILD_RE.match(stripped):
        return "child"
    return "other"


def _is_deleted_action(line: str) -> bool:
    """Return True if the parent line's change_type contains 'deleted'."""
    m = _PARENT_RE.match(line.rstrip("\n"))
    if not m:
        return False
    return bool(_DELETED_ACTION_RE.search(m.group("change_type")))


def _get_tag(line: str) -> Optional[str]:
    """Return the last <tag> value in *line*, lower-cased, or None."""
    m = _LAST_TAG_RE.search(line.rstrip("\n"))
    return m.group(1).strip().lower() if m else None


def _replace_last_tag(line: str, new_tag: str) -> str:
    """Replace the last <tag> in *line* with *new_tag*, preserving the newline."""
    nl = "\n" if line.endswith("\n") else ""
    body = line.rstrip("\n")
    # Replace the last occurrence of <...>
    new_body = _LAST_TAG_RE.sub(f"<{new_tag}>", body)
    return new_body + nl


# ---------------------------------------------------------------------------
# Block data structure
# ---------------------------------------------------------------------------

class _Entry:
    """One logical entry (parent or child) together with its continuation lines."""

    __slots__ = ("main_line", "continuations", "kind", "tag")

    def __init__(self, main_line: str, kind: str):
        self.main_line: str = main_line
        self.continuations: List[str] = []
        self.kind: str = kind                    # 'parent' | 'child'
        self.tag: Optional[str] = _get_tag(main_line)

    def all_lines(self) -> List[str]:
        return [self.main_line] + self.continuations

    def with_tag(self, new_tag: str) -> "_Entry":
        """Return a new _Entry with the tag on the main line replaced."""
        new_entry = _Entry(_replace_last_tag(self.main_line, new_tag), self.kind)
        new_entry.continuations = self.continuations
        return new_entry


class _Block:
    """One path-block: path header line + one parent entry + zero-or-more child entries."""

    __slots__ = ("path_line", "parent", "children", "pre_parent_others")

    def __init__(self, path_line: str):
        self.path_line: str = path_line
        self.parent: Optional[_Entry] = None
        self.children: List[_Entry] = []
        # Lines between the path header and the parent (rare, but preserve them)
        self.pre_parent_others: List[str] = []

    def all_lines(self) -> List[str]:
        out = [self.path_line]
        out.extend(self.pre_parent_others)
        if self.parent:
            out.extend(self.parent.all_lines())
        for child in self.children:
            out.extend(child.all_lines())
        return out

    def apply_propagation(self) -> "_Block":
        """
        Apply two propagation rules in priority order:

        Rule A — Semantic deletion inheritance:
          If the parent action is "deleted" AND the parent tag is NBC,
          rewrite ALL tagged children to inherit the parent's tag.
          This applies regardless of whether some children are already NBC.

        Rule B — All-BC propagation (original flag-mode fix):
          If parent is NBC and ALL children are BC → rewrite children to NBC.
          (Only applied when Rule A does not trigger.)
        """
        if self.parent is None or not self.children:
            return self

        parent_tag = (self.parent.tag or "").lower()
        if parent_tag != _NBC_TAG:
            return self

        # Rule A: parent action is "deleted" → inherit parent tag to all children
        if _is_deleted_action(self.parent.main_line):
            new_block = _Block(self.path_line)
            new_block.parent = self.parent
            new_block.pre_parent_others = self.pre_parent_others
            # Inherit parent tag to every child that has a tag (BC or NBC)
            new_block.children = [
                c.with_tag(parent_tag) if c.tag else c
                for c in self.children
            ]
            return new_block

        # Rule B: all children are BC → propagate NBC
        child_tags = [(c.tag or "").lower() for c in self.children]

        # If at least one child is already NBC → no action needed
        if any(t == _NBC_TAG for t in child_tags):
            return self

        # All children are BC → propagate NBC
        new_block = _Block(self.path_line)
        new_block.parent = self.parent
        new_block.pre_parent_others = self.pre_parent_others
        new_block.children = [
            c.with_tag(_NBC_TAG) if c.tag and c.tag.lower() == _BC_TAG else c
            for c in self.children
        ]
        return new_block


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_blocks(lines: List[str]) -> Tuple[List[str], List[_Block]]:
    """
    Split *lines* into a preamble (lines before the first path header) and
    a list of _Block objects.

    Continuation lines (wrapped description text) are attached to the last
    _Entry (parent or child) they follow, so the original line order is
    always preserved when blocks are serialised back.
    """
    preamble: List[str] = []
    blocks: List[_Block] = []
    current_block: Optional[_Block] = None
    last_entry: Optional[_Entry] = None   # last parent or child seen

    for raw in lines:
        kind = _classify(raw)

        if kind == "path":
            # Flush current block
            if current_block is not None:
                blocks.append(current_block)
            current_block = _Block(raw)
            last_entry = None

        elif kind == "parent":
            if current_block is None:
                # Orphan parent before any path header — treat as preamble
                preamble.append(raw)
                continue
            if current_block.parent is None:
                entry = _Entry(raw, "parent")
                current_block.parent = entry
                last_entry = entry
            else:
                # Second parent under the same path header (rare).
                # Flush the current block and start a new one reusing the path.
                blocks.append(current_block)
                new_block = _Block(current_block.path_line)
                entry = _Entry(raw, "parent")
                new_block.parent = entry
                current_block = new_block
                last_entry = entry

        elif kind == "child":
            if current_block is None:
                preamble.append(raw)
                continue
            entry = _Entry(raw, "child")
            current_block.children.append(entry)
            last_entry = entry

        else:  # "other" — blank line, continuation text, etc.
            if last_entry is not None:
                # Attach to the last entry (parent or child)
                last_entry.continuations.append(raw)
            elif current_block is not None:
                # Between path header and first parent
                current_block.pre_parent_others.append(raw)
            else:
                preamble.append(raw)

    # Flush last block
    if current_block is not None:
        blocks.append(current_block)

    return preamble, blocks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def propagate_nbc(input_path: str, output_path: str) -> int:
    """
    Read *input_path*, apply NBC propagation, write result to *output_path*.

    Returns the number of child lines that were rewritten.

    *input_path* and *output_path* may be the same file (in-place rewrite).
    """
    src = Path(input_path)
    if not src.exists():
        raise FileNotFoundError(f"Enriched report not found: {input_path}")

    raw_lines = src.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

    preamble, blocks = _parse_blocks(raw_lines)

    rewritten_count = 0
    out_lines: List[str] = list(preamble)

    for block in blocks:
        new_block = block.apply_propagation()
        # Count rewritten child main lines
        for orig, new in zip(block.children, new_block.children):
            if orig.main_line != new.main_line:
                rewritten_count += 1
        out_lines.extend(new_block.all_lines())

    Path(output_path).write_text("".join(out_lines), encoding="utf-8")

    if rewritten_count:
        print(
            f"[propagate_nbc] Rewrote {rewritten_count} child line(s) "
            f"(Rule A: deletion inheritance + Rule B: all-BC propagation) in: {output_path}"
        )
    else:
        print(f"[propagate_nbc] No changes needed in: {output_path}")

    return rewritten_count


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    default_input = os.environ.get("YANG_ENRICHED_FILE", "output/enriched_report_llm.txt")

    ap = argparse.ArgumentParser(
        description=(
            "Propagate <non-backward-compatible> tags from parent entries to "
            "their children in an enriched YANG compatibility report, when the "
            "parent is NBC but all children are BC."
        )
    )
    ap.add_argument(
        "--input", "-i",
        default=default_input,
        help=f"Path to the enriched report (default: {default_input})",
    )
    ap.add_argument(
        "--output", "-o",
        default=None,
        help=(
            "Path to write the updated report. "
            "Defaults to --input (in-place rewrite)."
        ),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing any file.",
    )

    args = ap.parse_args(argv)
    output = args.output or args.input

    if args.dry_run:
        src = Path(args.input)
        if not src.exists():
            print(f"ERROR: file not found: {args.input}", file=sys.stderr)
            return 1
        raw_lines = src.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        preamble, blocks = _parse_blocks(raw_lines)
        total = 0
        for block in blocks:
            new_block = block.apply_propagation()
            for orig, new in zip(block.children, new_block.children):
                if orig.main_line != new.main_line:
                    total += 1
                    print(f"  WOULD REWRITE: {orig.main_line.rstrip()!r}")
                    print(f"             → : {new.main_line.rstrip()!r}")
        print(f"\n[dry-run] Would rewrite {total} child line(s).")
        return 0

    try:
        propagate_nbc(args.input, output)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
