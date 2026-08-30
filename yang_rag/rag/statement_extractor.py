"""
yang_rag/rag/statement_extractor.py
------------------------------------
Extracts uncovered YANG extension statements from a comparator final_report.json
and builds accurate bare queries for RAG retrieval.

The bare query is constructed by reading the actual YANG file at the reported
line number (when available), so the syntax is always accurate (e.g.
``oc-ext:telemetry-on-change;`` rather than a guessed ``telemetry-on-change {};``).

Public API
----------
collect_uncovered_statements(report_path, xml_rules_path, search_dirs=None)
    -> list[StatementEntry]

build_bare_query(keyword, yang_file, line_no, value=None)
    -> str
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List


# ---------------------------------------------------------------------------
# Core YANG RFC 7950 built-in statements — never sent to RAG
# ---------------------------------------------------------------------------
YANG_BUILTINS: frozenset[str] = frozenset({
    'leaf', 'leaf-list', 'container', 'list', 'choice', 'case',
    'anydata', 'anyxml', 'grouping', 'uses', 'augment', 'rpc',
    'input', 'output', 'notification', 'typedef', 'module', 'submodule',
    'import', 'include', 'revision', 'extension', 'feature', 'identity',
    'deviation', 'deviate', 'type', 'key', 'unique', 'mandatory',
    'config', 'status', 'description', 'reference', 'units', 'default',
    'when', 'must', 'min-elements', 'max-elements', 'ordered-by',
    'presence', 'namespace', 'prefix', 'organization', 'contact',
    'yang-version', 'belongs-to', 'path', 'require-instance',
    'fraction-digits', 'length', 'pattern', 'range', 'enum', 'bit',
    'position', 'value', 'base', 'if-feature', 'refine', 'name',
    'action', 'error-message', 'error-app-tag', 'modifier',
})

# Context window (lines above/below) when extracting a YANG snippet
_CONTEXT_WINDOW = 5


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class StatementEntry:
    """A single uncovered extension keyword ready for RAG retrieval."""
    keyword: str          # bare keyword name (no prefix), e.g. 'telemetry-on-change'
    bare_query: str       # accurate YANG snippet for RAG, e.g. 'oc-ext:telemetry-on-change;'
    yang_file: str        # path to the YANG file where the keyword appears
    line_no: Optional[int]  # 1-based line number in yang_file
    is_structural: bool = False  # True if the extension has a block body {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_covered_keywords(xml_rules_path: Path) -> frozenset[str]:
    """
    Parse compatibility_rules.xml and return all keyword/attribute/constraint
    names that already have rules.  Grows automatically as the XML grows.
    """
    covered: set[str] = set()
    try:
        tree = ET.parse(str(xml_rules_path))
        root = tree.getroot()
        for elem in root.iter():
            # Cover all element types used in compatibility_rules.xml:
            # <structural> (new schema), <keyword> (legacy), <attribute>, <constraint>
            # A keyword added by Phase 2 may land in any of these categories
            # depending on what the generated rule uses — we must recognise all of
            # them so the re-run does not re-process already-covered keywords.
            if elem.tag in ('structural', 'keyword', 'attribute', 'constraint') and elem.text:
                covered.add(elem.text.strip())
            name = elem.get('name', '').strip()
            if name:
                covered.add(name)
    except Exception:
        pass
    return frozenset(covered)


def _resolve_yang_file(yang_file: str, search_dirs: Optional[List[str]] = None) -> Optional[Path]:
    """
    Resolve a YANG filename (may be bare name or relative/absolute path)
    to an existing Path.  Searches search_dirs if the direct path doesn't exist.
    """
    if not yang_file:
        return None
    p = Path(yang_file)
    if p.exists():
        return p
    name = p.name
    for d in (search_dirs or []):
        for found in Path(d).rglob(name):
            return found
    return None


def _extract_yang_snippet(yang_file: str, line_no: Optional[int],
                           keyword: str,
                           search_dirs: Optional[List[str]] = None,
                           window: int = _CONTEXT_WINDOW) -> str:
    """
    Extract a YANG code snippet around the usage location of a keyword.

    Returns the surrounding lines as a string, or just ``keyword;`` on failure.
    This is the authoritative source for the bare query — it reads the actual
    YANG syntax (e.g. ``oc-ext:telemetry-on-change;``) rather than guessing.
    """
    path = _resolve_yang_file(yang_file, search_dirs)
    if path is None:
        return f'{keyword};'
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        if line_no and 1 <= line_no <= len(lines):
            target_idx = line_no - 1
        else:
            target_idx = next(
                (i for i, ln in enumerate(lines) if keyword in ln),
                None
            )
            if target_idx is None:
                return f'{keyword};'
        start = max(0, target_idx - window)
        end = min(len(lines), target_idx + window + 1)
        return '\n'.join(lines[start:end])
    except Exception:
        return f'{keyword};'


def _line_contains_keyword_as_statement(line: str, keyword: str) -> bool:
    """
    Return True only when *keyword* appears as a YANG statement keyword on *line*,
    not merely as part of an identifier or module/submodule name.

    A keyword is a statement when it appears as:
      - a prefixed extension:  ``smiv2:defval``, ``oc-ext:telemetry-on-change``
      - an unquoted bare word at the start of the stripped line (possibly preceded
        by whitespace), followed by a space, ``{``, or ``;``

    This prevents ``defval`` from matching ``submodule demo-defval {`` where
    ``defval`` is part of the module name, not a statement keyword.
    """
    stripped = line.strip()
    # Match prefixed form: any-prefix:keyword (e.g. smiv2:defval, oc-ext:telemetry-on-change)
    if re.search(r'(?<![:\w])[\w][\w\-]*:' + re.escape(keyword) + r'(?=[\s;{]|$)', stripped):
        return True
    # Match bare keyword as the first token of the statement (unquoted, at word boundary)
    # Must be followed by whitespace, ;, or { — not by - or alphanumeric (part of a name)
    if re.match(r'^' + re.escape(keyword) + r'(?=[\s;{]|$)', stripped):
        return True
    return False


def _extract_single_line(yang_file: str, line_no: Optional[int],
                          keyword: str,
                          search_dirs: Optional[List[str]] = None) -> str:
    """
    Extract just the single line containing the keyword usage.
    Used to build the minimal bare query (e.g. ``oc-ext:telemetry-on-change;``).
    Falls back to ``keyword;`` on failure.

    Search strategy (in order):
    1. Check the exact line at ``line_no`` — if it contains the keyword as a statement.
    2. Scan forward from ``line_no`` (the reported line is often the *parent* node's
       line; the extension statement appears on a later line inside the same block).
    3. Scan the whole file for the keyword as a statement (not as part of a name).
    """
    path = _resolve_yang_file(yang_file, search_dirs)
    if path is None:
        return f'{keyword};'
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        total = len(lines)

        # 1. Exact line check
        if line_no and 1 <= line_no <= total:
            line = lines[line_no - 1].strip()
            if _line_contains_keyword_as_statement(line, keyword):
                return line

        # 2. Scan forward from line_no (extension is usually inside the parent block)
        start = (line_no - 1) if (line_no and 1 <= line_no <= total) else 0
        for ln in lines[start:]:
            stripped = ln.strip()
            if _line_contains_keyword_as_statement(stripped, keyword):
                return stripped

        # 3. Full-file scan (keyword as statement, not as part of a name)
        for ln in lines:
            stripped = ln.strip()
            if _line_contains_keyword_as_statement(stripped, keyword):
                return stripped

    except Exception:
        pass
    return f'{keyword};'


def build_bare_query(keyword: str,
                     yang_file: str,
                     line_no: Optional[int],
                     value: Optional[str] = None,
                     search_dirs: Optional[List[str]] = None) -> str:
    """
    Build an accurate bare query for RAG retrieval.

    Strategy (in priority order):
    1. Read the actual line from the YANG file at line_no — most accurate.
    2. Search the YANG file for the first occurrence of the keyword.
    3. Fall back to a synthesised query based on the value from the report.

    The returned string is the minimal YANG statement line, e.g.:
      ``oc-ext:telemetry-on-change;``
      ``oc-ext:posix-pattern "^[0-9]+$";``
      ``tailf:callpoint "snmp" { tailf:internal; }``
    """
    # Try to read from the actual YANG file first
    if yang_file:
        line = _extract_single_line(yang_file, line_no, keyword, search_dirs)
        if keyword in line:
            return line

    # Fall back to synthesised query from report value
    if value and str(value).strip() not in ('None', 'null', '{}', ''):
        val = str(value).strip()
        # Numeric values don't need quotes
        if re.match(r'^[0-9]+(\.[0-9]+)?$', val):
            return f'{keyword} {val};'
        return f'{keyword} "{val}";'

    # Flag-type extension (no value, no block)
    return f'{keyword};'


def _is_structural(value: Optional[str]) -> bool:
    """Return True if the value looks like a block body (structural extension)."""
    if not value:
        return False
    v = str(value).strip()
    return (v.startswith('{') and v.endswith('}')) or (v.startswith('[') and v.endswith(']'))


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def _resolve_to_absolute(yang_file: str, search_dirs: Optional[List[str]] = None) -> str:
    """Resolve a YANG filename to an absolute path string.
    Returns the original string if resolution fails.
    """
    resolved = _resolve_yang_file(yang_file, search_dirs)
    if resolved is not None:
        return str(resolved.resolve())
    return yang_file


def collect_uncovered_statements(
    report_path: "str | Path",
    xml_rules_path: "str | Path",
    search_dirs: Optional[List[str]] = None,
) -> List[StatementEntry]:
    """
    Read a comparator ``final_report.json`` (or ``concise_final_report.json``)
    and return a list of :class:`StatementEntry` objects for every extension
    keyword/attribute in the ``unmarked`` section that is NOT already covered
    by the XML rules and is NOT a YANG builtin.

    Parameters
    ----------
    report_path:
        Path to the ``final_report.json`` produced by the comparator.
    xml_rules_path:
        Path to ``compatibility_rules.xml`` — used to build the covered-keyword
        set so already-known extensions are skipped.
    search_dirs:
        Optional list of directories to search when resolving bare YANG
        filenames to absolute paths.

    Returns
    -------
    List of :class:`StatementEntry`, deduplicated by keyword name.
    """
    report_path = Path(report_path)
    xml_rules_path = Path(xml_rules_path)

    try:
        report = json.loads(report_path.read_text(encoding='utf-8'))
    except Exception as exc:
        raise ValueError(f"Cannot read report: {report_path}: {exc}") from exc

    covered = _load_covered_keywords(xml_rules_path)
    skip_set = YANG_BUILTINS | covered

    seen: set[str] = set()
    entries: List[StatementEntry] = []

    def _add(keyword: str, yang_file: str, line_no: Optional[int],
             value: Optional[str]) -> None:
        if not keyword or keyword in skip_set or keyword in seen:
            return
        seen.add(keyword)
        # Resolve yang_file to an absolute path so downstream tools
        # (simple_rule_updater_cli) can find it regardless of working directory.
        resolved_yang_file = _resolve_to_absolute(yang_file, search_dirs) if yang_file else ''
        bare = build_bare_query(keyword, resolved_yang_file, line_no, value, search_dirs)
        structural = _is_structural(value)
        entries.append(StatementEntry(
            keyword=keyword,
            bare_query=bare,
            yang_file=resolved_yang_file,
            line_no=line_no,
            is_structural=structural,
        ))

    for item in report.get('unmarked', []):
        kw = item.get('keyword', '').strip()
        yang_file = item.get('file', '')
        line_no = item.get('line_number') or None
        if line_no is not None:
            try:
                line_no = int(line_no)
            except (TypeError, ValueError):
                line_no = None

        # Process the top-level keyword
        _add(kw, yang_file, line_no, None)

        # Process attributes
        for attr in item.get('attributes', []):
            ak = attr.get('attribute', '').strip()
            af = attr.get('file', yang_file)
            al = attr.get('line_number') or None
            if al is not None:
                try:
                    al = int(al)
                except (TypeError, ValueError):
                    al = None
            av = attr.get('new_value') or attr.get('old_value') or attr.get('value')
            _add(ak, af, al, av)

    return entries
