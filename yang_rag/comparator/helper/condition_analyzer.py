"""
Semantic analyzer for YANG condition constraint changes (when/must).

This module provides intelligent analysis of condition changes to determine
if they represent narrowing (breaking) or relaxing (compatible) changes.

Supported patterns:
1. Enum equality: "../admin-state = 'enable'"
2. Number comparisons: "../number = 46", "../number != 46"
3. Boolean comparisons: "../tunnel = 'false'"
4. Flag/presence checks: "../../fwd-service", "not(../../fwd-service)"
5. Multi-condition (OR/AND): "../../router-name = 'Base' or ../../router-name = 'management'"
6. Complex expressions with XPath functions

For complex cases that cannot be determined by rules, the analyzer returns
NEEDS_LLM_ANALYSIS to signal that DSPy-based assistance is required.

XPath Resolution:
When comparing path-based conditions (equality, inequality, comparisons), the
analyzer can optionally resolve relative XPath expressions to absolute schema
paths. This enables detecting semantic changes where the XPath string changes
but the meaning changes (e.g., "../../../config/type" vs "../../config/type"
pointing to different nodes).
"""

import re
import os
from typing import Optional, Tuple, Dict, Any, List, Set
from enum import Enum
from dataclasses import dataclass

# ── Process-level caches ──────────────────────────────────────────────────────
# These caches survive across ConditionAnalyzer instances within the same
# process, eliminating redundant pyang loads for the same YANG files.
#
# Key conventions:
#   _identity_enum_cache : abs_yang_file_path → Optional[Set[str]]
#   _prefix_map_cache    : (abs_yang_file_path, sorted_search_dirs) → Dict[str, str]
#
# The XPathResolver cache lives in xpath_resolver.py (get_cached_xpath_resolver).
_identity_enum_cache: Dict[str, Optional[Set]] = {}
_identity_enum_bit_cache: Dict[str, Optional[Dict[str, Set[str]]]] = {}
_prefix_map_cache: Dict[tuple, Dict[str, str]] = {}


class ConditionType(Enum):
    """Types of conditions we can analyze."""
    PRESENCE = "presence"           # when "../../fwd-service"
    EQUALITY = "equality"           # when "../x = 'value'"
    INEQUALITY = "inequality"       # when "../x != 'value'"
    COMPARISON = "comparison"       # when "../x > 5", when "../x >= 5"
    OR_CLAUSE = "or_clause"         # when "a or b"
    AND_CLAUSE = "and_clause"       # when "a and b"
    NOT_CLAUSE = "not_clause"       # when "not(condition)"
    COMPLEX = "complex"             # Complex XPath expressions
    UNKNOWN = "unknown"


class ChangeDirection(Enum):
    """Direction of constraint change."""
    NARROWED = "narrowed"                   # More restrictive (breaking)
    RELAXED = "relaxed"                     # Less restrictive (compatible)
    EQUIVALENT = "equivalent"               # Logically equivalent
    INCOMPARABLE = "incomparable"           # Cannot determine relationship
    NEEDS_LLM_ANALYSIS = "needs_llm_analysis"  # Complex case requiring DSPy assistance


@dataclass
class ParsedCondition:
    """Represents a parsed when/must condition."""
    type: ConditionType
    raw: str
    path: Optional[str] = None          # XPath reference like "../admin-state"
    operator: Optional[str] = None      # =, !=, <, >, <=, >=
    value: Optional[str] = None         # Compared value
    subconditions: List['ParsedCondition'] = None  # For OR/AND clauses
    is_negated: bool = False            # For NOT clauses
    
    def __post_init__(self):
        if self.subconditions is None:
            self.subconditions = []


class ConditionAnalyzer:
    """Analyzes YANG condition changes (when/must) for semantic compatibility."""
    
    # Regex patterns for different condition types
    PATTERNS = {
        # Presence check: when "../../fwd-service"
        'presence': re.compile(r'''^\s*["']?([a-zA-Z0-9_\-./:\[\]@]+)["']?\s*$'''),
        
        # Equality: when "../admin-state = 'enable'"
        'equality': re.compile(r'''^\s*["']?([a-zA-Z0-9_\-./:\[\]@]+)["']?\s*=\s*["']([^"']+)["']\s*$'''),
        
        # Inequality: when "../number != 46"
        'inequality': re.compile(r'''^\s*["']?([a-zA-Z0-9_\-./:\[\]@]+)["']?\s*!=\s*["']?([^"'\s]+)["']?\s*$'''),
        
        # Comparison: when "../count > 5"
        'comparison': re.compile(r'''^\s*["']?([a-zA-Z0-9_\-./:\[\]@]+)["']?\s*(<=?|>=?)\s*["']?([^"'\s]+)["']?\s*$'''),
        
        # NOT clause: when "not(../../fwd-service)"
        'not_clause': re.compile(r'''^\s*not\s*\(\s*(.+?)\s*\)\s*$''', re.IGNORECASE),
        
        # OR clause: when "a = 'x' or b = 'y'"
        'or_clause': re.compile(r'''\s+or\s+''', re.IGNORECASE),
        
        # AND clause: when "a = 'x' and b = 'y'"
        'and_clause': re.compile(r'''\s+and\s+''', re.IGNORECASE),
    }
    
    def __init__(self,
                 enable_xpath_resolution: bool = True,
                 old_yang_file: Optional[str] = None,
                 new_yang_file: Optional[str] = None,
                 context_path: Optional[str] = None,
                 search_dirs: Optional[List[str]] = None,
                 old_pyang_broken_lines: Optional[Set[int]] = None,
                 new_pyang_broken_lines: Optional[Set[int]] = None):
        """
        Initialize the analyzer.
        
        Args:
            enable_xpath_resolution: Enable XPath resolution for path comparisons (default: True)
            old_yang_file: Path to old YANG file (required if xpath resolution enabled)
            new_yang_file: Path to new YANG file (required if xpath resolution enabled)
            context_path: Schema path of context node (where when/must is attached)
            search_dirs: Additional directories for YANG imports/includes
            old_pyang_broken_lines: Set of line numbers in old_yang_file where pyang
                reports XPATH_NODE_NOT_FOUND errors (broken must/when conditions).
                For equality conditions (path = value), the pyang broken flag is only
                used to override path_valid when the XPath resolver could NOT confirm
                the LHS path as valid.  If the resolver already returned True (path
                exists in schema), the pyang error is assumed to be about the RHS
                (e.g. an unquoted identity value) and the override is skipped.
            new_pyang_broken_lines: Same as old_pyang_broken_lines but for new_yang_file.
        """
        self.stats = {
            'total': 0,
            'narrowed': 0,
            'relaxed': 0,
            'equivalent': 0,
            'incomparable': 0,
            'needs_llm_analysis': 0
        }
        
        # XPath resolution configuration
        self.enable_xpath_resolution = enable_xpath_resolution
        self.old_yang_file = old_yang_file
        self.new_yang_file = new_yang_file
        self.context_path = self._sanitize_context_path(context_path)
        self.search_dirs = search_dirs

        # Pyang-validated broken condition lines (XPATH_NODE_NOT_FOUND* errors).
        # These are sets of 1-based line numbers in the respective YANG files where
        # pyang has confirmed that a must/when XPath expression is always-FALSE.
        # None means "no pyang data was provided"; an empty set means "pyang ran
        # and found no XPATH errors" — these are semantically different:
        # - None → do not use pyang to override path validity
        # - empty set → pyang ran cleanly → new conditions are valid (path_valid=True)
        self._old_pyang_broken_lines: Optional[Set[int]] = old_pyang_broken_lines
        self._new_pyang_broken_lines: Optional[Set[int]] = new_pyang_broken_lines

        # Lazy-loaded cache: condition text → bool (True = pyang confirmed broken)
        # Built on first use from the YANG file + broken line numbers.
        self._old_pyang_broken_conditions: Optional[Set[str]] = None
        self._new_pyang_broken_conditions: Optional[Set[str]] = None
        # Lazy-loaded cache: (normalized_condition_text, attachment_anchor_context)
        # for pyang-broken conditions. anchor context may be '' when unavailable.
        self._old_pyang_broken_condition_contexts: Optional[Set[Tuple[str, str]]] = None
        self._new_pyang_broken_condition_contexts: Optional[Set[Tuple[str, str]]] = None
        
        # Lazy-load XPath resolvers
        self._old_resolver = None
        self._new_resolver = None
        
        # Lazy-load prefix→defining_module maps (built from pyang import statements)
        # Maps: prefix_string → canonical_module_name (the module that defines the identity)
        # e.g. {'mb': 'module-c', 'mc': 'module-c'} when both prefixes resolve to module-c
        self._old_prefix_map: Optional[Dict[str, str]] = None
        self._new_prefix_map: Optional[Dict[str, str]] = None
        # Debug output can be disabled with YANG_CONDITION_DEBUG=0
        self._debug_enabled = os.environ.get("YANG_CONDITION_DEBUG", "1") not in {"0", "false", "False"}
        self._last_analysis_details: Dict[str, Any] = {}

    @staticmethod
    def _sanitize_context_path(context_path: Optional[str]) -> Optional[str]:
        """
        Normalize a context path that may contain a duplicated module-name segment
        or a trailing YANG sub-statement keyword (e.g. '/type', '/leafref', '/uses').

        Report lines produced by the comparator sometimes have the form:
            ``module-name/module-name/grouping/container/leaf``
        (no leading ``/``, module name repeated).  The XPath resolver fails on
        such paths because it tries to find a child named ``module-name`` under
        the module root (also named ``module-name``).

        This method:
        1. Adds a leading ``/`` if absent.
        2. Strips the redundant second segment when it equals the first.
        3. Strips trailing YANG sub-statement keywords (type, leafref, uses, etc.)
           so that XPath resolution starts from the correct data-tree node.
           e.g. '.../admin-group-name/type' → '.../admin-group-name'

        Examples::
            "openconfig-terminal-device/openconfig-terminal-device/terminal-device-top/..."
            → "/openconfig-terminal-device/terminal-device-top/..."

            "/openconfig-acl/acl/acl-sets"  (already clean)
            → "/openconfig-acl/acl/acl-sets"

            "/openconfig-mpls/mpls-top/mpls/.../admin-group-name/type"
            → "/openconfig-mpls/mpls-top/mpls/.../admin-group-name"
        """
        if not context_path:
            return context_path
        # Ensure leading slash
        path = context_path if context_path.startswith('/') else '/' + context_path
        parts = [p for p in path.split('/') if p]
        if len(parts) >= 2 and parts[0] == parts[1]:
            parts = parts[1:]  # drop the duplicate first segment
        result = '/' + '/'.join(parts) if parts else path
        # Strip trailing YANG sub-statement keywords (not data-tree nodes).
        # These appear when the context path points to a sub-statement node
        # (e.g. the 'type' sub-statement of a leaf) rather than the leaf itself.
        # Stripping them ensures XPath resolution starts from the correct leaf node.
        try:
            from yang_rag.comparator.helper.xpath_comparison import normalise_context_path
            result = normalise_context_path(result)
        except ImportError:
            pass
        return result

    def _debug(self, message: str):
        """Emit analyzer debug logs with a stable prefix for log filtering."""
        if self._debug_enabled:
            print(f"debug {message}")

    def _prioritized_search_dirs(self, yang_file: str) -> List[str]:
        """Return search dirs with *yang_file* directory forced to highest priority."""
        from pathlib import Path as _Path

        file_dir = str(_Path(yang_file).parent)
        ordered: List[str] = [file_dir]
        for d in list(self.search_dirs or []):
            if d != file_dir:
                ordered.append(d)
        return ordered

    # ── Pyang broken-condition helpers ──────────────────────────────────────

    @staticmethod
    def _normalize_condition_text_for_match(condition_text: str) -> str:
        """Normalize condition text for reliable equality matching.

        Keeps inner quotes intact and only strips one pair of outer quotes if
        the entire condition string is quoted.
        """
        text = (condition_text or '').strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            text = text[1:-1].strip()
        # Collapse whitespace/newlines to a single space for stable comparison.
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _build_pyang_broken_conditions(self, yang_file: str, broken_lines: Set[int]) -> Set[str]:
        """
        Read *yang_file* and extract the condition text (argument of ``must`` or
        ``when``) for every line number in *broken_lines*.

        The YANG grammar places the condition string on the same line as the
        ``must``/``when`` keyword, or on the immediately following lines if the
        string is multi-line.  We use a simple regex scan: for each broken line
        we look for a ``must``/``when`` keyword and capture its quoted argument.

        Returns a set of normalised condition strings (stripped of outer quotes
        and leading/trailing whitespace).
        """
        if not broken_lines or not yang_file:
            return set()
        try:
            with open(yang_file, 'r', encoding='utf-8') as fh:
                lines = fh.readlines()
        except OSError:
            return set()

        # Pattern: optional whitespace, keyword, whitespace, then a quoted string
        # (single or double quotes, possibly spanning multiple lines — we handle
        # the simple single-line case here; multi-line is rare for must/when).
        _kw_pat = re.compile(
            r'''^\s*(?:must|when)\s+["'](.+?)["']\s*(?:\{|;)?\s*$'''
        )
        # Broader fallback: capture everything after the keyword up to end-of-line
        _kw_broad = re.compile(
            r'''^\s*(?:must|when)\s+["'](.+)$'''
        )

        result: Set[str] = set()
        for lineno in broken_lines:
            idx = lineno - 1  # 0-based
            if idx < 0 or idx >= len(lines):
                continue
            line = lines[idx]
            m = _kw_pat.match(line)
            if m:
                result.add(self._normalize_condition_text_for_match(m.group(1)))
                continue
            # Try broad match (condition may span multiple lines)
            m2 = _kw_broad.match(line)
            if m2:
                # Collect continuation until we find the closing quote
                partial = m2.group(1)
                # If the partial already ends with a closing quote, strip it
                for q in ('"', "'"):
                    if partial.endswith(q) or partial.rstrip().endswith(q + ' {') or partial.rstrip().endswith(q + ';'):
                        partial = partial.rstrip().rstrip('{').rstrip(';').strip().strip(q)
                        break
                else:
                    # Multi-line: accumulate until closing quote found
                    j = idx + 1
                    while j < len(lines) and j < idx + 10:
                        partial += ' ' + lines[j].strip()
                        if '"' in lines[j] or "'" in lines[j]:
                            break
                        j += 1
                    # Strip trailing quote/brace/semicolon
                    partial = partial.strip().strip('"').strip("'").rstrip('{').rstrip(';').strip()
                result.add(self._normalize_condition_text_for_match(partial))
        return result

    def _get_pyang_broken_conditions(self, which: str) -> Optional[Set[str]]:
        """
        Return the set of condition strings that pyang has confirmed are broken
        (always-FALSE) in the specified YANG file.

        Args:
            which: ``'old'`` or ``'new'``

        Returns:
            - ``None`` if no pyang broken-line data was provided (cannot determine).
            - An empty set if pyang ran and found no XPATH errors (all conditions valid).
            - A non-empty set of normalised condition strings that pyang flagged as broken.
        """
        if which == 'old':
            if self._old_pyang_broken_lines is None:
                return None  # No pyang data provided for old file
            if self._old_pyang_broken_conditions is None:
                self._old_pyang_broken_conditions = self._build_pyang_broken_conditions(
                    self.old_yang_file or '', self._old_pyang_broken_lines
                )
                if self._old_pyang_broken_conditions:
                    self._debug(
                        f"pyang_broken_conditions old={self._old_pyang_broken_conditions}"
                    )
            return self._old_pyang_broken_conditions
        else:
            if self._new_pyang_broken_lines is None:
                return None  # No pyang data provided for new file
            if self._new_pyang_broken_conditions is None:
                self._new_pyang_broken_conditions = self._build_pyang_broken_conditions(
                    self.new_yang_file or '', self._new_pyang_broken_lines
                )
                if self._new_pyang_broken_conditions:
                    self._debug(
                        f"pyang_broken_conditions new={self._new_pyang_broken_conditions}"
                    )
            return self._new_pyang_broken_conditions

    def _build_pyang_broken_condition_contexts(
        self,
        yang_file: str,
        broken_lines: Set[int],
    ) -> Set[Tuple[str, str]]:
        """
        Build context-aware pyang broken condition entries as
        (normalized_condition, attachment_anchor_context).

        The context is inferred from the owner of each broken ``when``/``must``
        statement using RFC7950 attachment rules (augment/wrapper/data-node).
        """
        if not yang_file or not broken_lines:
            return set()

        entries: Set[Tuple[str, str]] = set()

        try:
            try:
                from .pyang_utils import PyangContext, PyangStatementHelper
            except ImportError:
                from yang_rag.comparator.helper.pyang_utils import PyangContext, PyangStatementHelper

            search_dirs = self._prioritized_search_dirs(yang_file)
            ctx = PyangContext(search_dirs=search_dirs)
            mod = ctx.load_module(yang_file)
            if mod is not None:
                for stmt in self._iter_statements_recursive(mod):
                    kw = PyangStatementHelper.get_keyword(stmt)
                    if kw not in {'when', 'must'}:
                        continue
                    pos = getattr(stmt, 'pos', None)
                    line = getattr(pos, 'line', None)
                    if line not in broken_lines:
                        continue
                    cond = self._normalize_condition_text_for_match(
                        PyangStatementHelper.get_argument(stmt) or ''
                    )
                    if not cond:
                        continue
                    owner = getattr(stmt, 'parent', None)
                    anchor = self._condition_attachment_anchor_from_owner(owner) or ''
                    entries.add((cond, anchor))
        except Exception:
            # Fall back to text-only entries if AST extraction fails.
            pass

        # Ensure we at least preserve text-only broken markers.
        for cond in self._build_pyang_broken_conditions(yang_file, broken_lines):
            entries.add((cond, ''))

        return entries

    def _get_pyang_broken_condition_contexts(self, which: str) -> Optional[Set[Tuple[str, str]]]:
        """Return context-aware pyang broken entries for ``which`` side."""
        if which == 'old':
            if self._old_pyang_broken_lines is None:
                return None
            if self._old_pyang_broken_condition_contexts is None:
                self._old_pyang_broken_condition_contexts = self._build_pyang_broken_condition_contexts(
                    self.old_yang_file or '', self._old_pyang_broken_lines
                )
            return self._old_pyang_broken_condition_contexts
        else:
            if self._new_pyang_broken_lines is None:
                return None
            if self._new_pyang_broken_condition_contexts is None:
                self._new_pyang_broken_condition_contexts = self._build_pyang_broken_condition_contexts(
                    self.new_yang_file or '', self._new_pyang_broken_lines
                )
            return self._new_pyang_broken_condition_contexts

    def _is_pyang_broken(
        self,
        condition_raw: str,
        which: str,
        report_context: Optional[str] = None,
        resolver=None,
    ) -> bool:
        """
        Return True if *condition_raw* (the raw XPath string from the YANG file,
        without outer quotes) is in the set of pyang-confirmed broken conditions
        for the specified side.

        Returns False if no pyang data is available (broken set is None) or if
        the condition is not in the broken set.

        Matching is done after stripping outer whitespace and quotes so that
        minor formatting differences do not cause false negatives.
        """
        broken = self._get_pyang_broken_conditions(which)
        if not broken:  # None or empty set → not broken
            return False
        needle = self._normalize_condition_text_for_match(condition_raw)
        if needle not in broken:
            return False

        # Context-aware filtering: if we can infer an attachment anchor for the
        # current analysis, require a matching broken entry anchor (or unknown '').
        broken_ctx = self._get_pyang_broken_condition_contexts(which)
        if not broken_ctx:
            return True

        candidate_anchors = {anchor for cond, anchor in broken_ctx if cond == needle}
        if not candidate_anchors:
            return True

        # When the condition text appears at only ONE attachment point in the schema,
        # the broken signal is unambiguous — return True without anchor matching.
        # Anchor matching is only needed when the same condition text appears at
        # multiple attachment points (to avoid false positives from text collisions).
        concrete_anchors = {a for a in candidate_anchors if a}
        if len(concrete_anchors) == 1:
            return True

        if not report_context or resolver is None:
            return True

        current_anchor = self._infer_condition_attachment_context(
            resolver=resolver,
            condition_raw=condition_raw,
            report_context=report_context,
        )
        if hasattr(resolver, '_canonicalize_context_path'):
            try:
                current_anchor = resolver._canonicalize_context_path(current_anchor)
            except Exception:
                pass

        if current_anchor in candidate_anchors:
            return True

        # Unknown-anchor entries are treated as permissive fallback only when no
        # concrete anchors were available for this condition text.
        if not concrete_anchors and '' in candidate_anchors:
            return True

        return False

    # ── End pyang broken-condition helpers ───────────────────────────────────

    def get_last_analysis_details(self) -> Dict[str, Any]:
        """Return structured details from the most recent analyze_change call."""
        # Shallow copy is enough here; values are primitives/small nested dicts.
        return dict(self._last_analysis_details)
    
    def parse_condition(self, condition: str) -> ParsedCondition:
        """
        Parse a when/must condition string into structured form.
        
        Args:
            condition: The raw condition string
            
        Returns:
            ParsedCondition object
        """
        condition = condition.strip()
        
        # Remove outer quotes if present
        if (condition.startswith('"') and condition.endswith('"')) or \
           (condition.startswith("'") and condition.endswith("'")):
            condition = condition[1:-1].strip()
        
        # Normalize YANG string concatenation issues: 'value'or -> 'value' or
        # This handles cases where multi-line string concat lacks proper spacing
        condition = re.sub(r"(['\"])(or|and)(\s+)", r"\1 \2\3", condition, flags=re.IGNORECASE)
        
        # Check for NOT clause first
        m = self.PATTERNS['not_clause'].match(condition)
        if m:
            inner = self.parse_condition(m.group(1))
            return ParsedCondition(
                type=ConditionType.NOT_CLAUSE,
                raw=condition,
                subconditions=[inner],
                is_negated=True
            )
        
        # Check for OR clause
        if self.PATTERNS['or_clause'].search(condition):
            parts = re.split(r'\s+or\s+', condition, flags=re.IGNORECASE)
            return ParsedCondition(
                type=ConditionType.OR_CLAUSE,
                raw=condition,
                subconditions=[self.parse_condition(p.strip()) for p in parts]
            )
        
        # Check for AND clause
        if self.PATTERNS['and_clause'].search(condition):
            parts = re.split(r'\s+and\s+', condition, flags=re.IGNORECASE)
            return ParsedCondition(
                type=ConditionType.AND_CLAUSE,
                raw=condition,
                subconditions=[self.parse_condition(p.strip()) for p in parts]
            )
        
        # Check for comparison operators
        m = self.PATTERNS['comparison'].match(condition)
        if m:
            return ParsedCondition(
                type=ConditionType.COMPARISON,
                raw=condition,
                path=m.group(1),
                operator=m.group(2),
                value=m.group(3)
            )
        
        # Check for inequality
        m = self.PATTERNS['inequality'].match(condition)
        if m:
            return ParsedCondition(
                type=ConditionType.INEQUALITY,
                raw=condition,
                path=m.group(1),
                operator='!=',
                value=m.group(2)
            )
        
        # Check for equality (including single dot notation like ". = 0")
        m = self.PATTERNS['equality'].match(condition)
        if m:
            return ParsedCondition(
                type=ConditionType.EQUALITY,
                raw=condition,
                path=m.group(1),
                operator='=',
                value=m.group(2)
            )
        
        # Special case for single dot with operator (must expressions)
        if re.match(r'^\s*\.\s*[!=<>]+\s*', condition):
            # Try equality again with more permissive pattern
            m = re.match(r'^\s*(\.)[\s]*=[\s]*["\']?([^"\']+?)["\']?\s*$', condition)
            if m:
                return ParsedCondition(
                    type=ConditionType.EQUALITY,
                    raw=condition,
                    path=m.group(1),
                    operator='=',
                    value=m.group(2).strip()
                )
        
        # Check for simple presence
        m = self.PATTERNS['presence'].match(condition)
        if m:
            # Exclude if it looks like a complex XPath function call
            if '(' in condition or '[' in condition or any(func in condition.lower() 
                   for func in ['current()', 'count(', 'string(', 'number(', 'boolean(']):
                return ParsedCondition(
                    type=ConditionType.COMPLEX,
                    raw=condition
                )
            return ParsedCondition(
                type=ConditionType.PRESENCE,
                raw=condition,
                path=m.group(1)
            )
        
        # Complex or unknown
        return ParsedCondition(
            type=ConditionType.COMPLEX if any(c in condition for c in '()[]') else ConditionType.UNKNOWN,
            raw=condition
        )
    
    def analyze_change(self, old_condition: Optional[str], new_condition: Optional[str]) -> Tuple[ChangeDirection, str]:
        """
        Analyze the semantic change between two conditions.
        
        Args:
            old_condition: Original condition (None if added)
            new_condition: New condition (None if deleted)
            
        Returns:
            Tuple of (ChangeDirection, explanation)
        """
        self._last_analysis_details = {
            'old_condition': old_condition,
            'new_condition': new_condition,
            'context_path': self.context_path,
        }
        self.stats['total'] += 1
        self._debug(
            f"analyze_change start old={old_condition!r} new={new_condition!r} "
            f"context={self.context_path!r}"
        )
        
        # Handle addition/deletion
        if old_condition is None and new_condition is not None:
            # Constraint added - always narrowing (more restrictions)
            self.stats['narrowed'] += 1
            self._last_analysis_details['final'] = {
                'direction': ChangeDirection.NARROWED.value,
                'explanation': "Condition added (new restriction)",
            }
            return ChangeDirection.NARROWED, "Condition added (new restriction)"
        
        if old_condition is not None and new_condition is None:
            # Constraint removed - always relaxing (fewer restrictions)
            self.stats['relaxed'] += 1
            self._last_analysis_details['final'] = {
                'direction': ChangeDirection.RELAXED.value,
                'explanation': "Condition removed (restriction lifted)",
            }
            return ChangeDirection.RELAXED, "Condition removed (restriction lifted)"
        
        if old_condition == new_condition:
            self.stats['equivalent'] += 1
            self._last_analysis_details['final'] = {
                'direction': ChangeDirection.EQUIVALENT.value,
                'explanation': "Conditions are identical",
            }
            return ChangeDirection.EQUIVALENT, "Conditions are identical"
        
        # Parse both conditions
        old_parsed = self.parse_condition(old_condition)
        new_parsed = self.parse_condition(new_condition)
        
        # Delegate to specific analyzers based on type
        result = self._analyze_parsed(old_parsed, new_parsed)
        
        # Update stats
        if result[0] == ChangeDirection.NARROWED:
            self.stats['narrowed'] += 1
        elif result[0] == ChangeDirection.RELAXED:
            self.stats['relaxed'] += 1
        elif result[0] == ChangeDirection.EQUIVALENT:
            self.stats['equivalent'] += 1
        elif result[0] == ChangeDirection.INCOMPARABLE:
            self.stats['incomparable'] += 1
        elif result[0] == ChangeDirection.NEEDS_LLM_ANALYSIS:
            self.stats['needs_llm_analysis'] += 1
        
        self._debug(
            f"analyze_change decision={result[0].value} reason={result[1]}"
        )
        self._last_analysis_details['final'] = {
            'direction': result[0].value,
            'explanation': result[1],
        }
        return result
    
    def _is_syntactically_broken(self, condition_raw: str) -> bool:
        """
        Detect syntactically broken XPath conditions.

        A condition is broken if it has:
          - Unclosed single quotes (odd number of single quotes)
          - Unclosed double quotes (odd number of double quotes)

        These indicate malformed XPath that was never valid — the condition
        is always-FALSE (or always-TRUE) regardless of data values.

        Examples of broken conditions:
          "../opmode = 'WPA3_SAE' or ../opemode = 'WPA3_2_SAE_TRANSITION"
          (missing closing quote on last value)
        """
        single_quotes = condition_raw.count("'")
        double_quotes = condition_raw.count('"')
        return (single_quotes % 2 != 0) or (double_quotes % 2 != 0)

    def _analyze_parsed(self, old: ParsedCondition, new: ParsedCondition) -> Tuple[ChangeDirection, str]:
        """Analyze change between two parsed conditions."""

        # ── Syntactic validity check ──────────────────────────────────────────
        # Before any semantic analysis, check if either condition is syntactically
        # broken (e.g. unclosed quotes). A broken condition is always-FALSE.
        # Apply the validity framework: broken→valid = RELAXED (BC),
        # valid→broken = NARROWED (NBC), both broken = EQUIVALENT.
        old_broken = self._is_syntactically_broken(old.raw)
        new_broken = self._is_syntactically_broken(new.raw)
        if old_broken or new_broken:
            if old_broken and new_broken:
                return ChangeDirection.EQUIVALENT, (
                    f"Both conditions are syntactically broken (unclosed quotes) → "
                    f"both always-FALSE → EQUIVALENT."
                )
            elif old_broken and not new_broken:
                return ChangeDirection.RELAXED, (
                    f"Old condition is syntactically broken (unclosed quotes, always-FALSE): "
                    f"'{old.raw[:80]}...'. New condition is valid. "
                    f"→ RELAXED (BC): fix applied."
                )
            else:  # not old_broken and new_broken
                return ChangeDirection.NARROWED, (
                    f"New condition is syntactically broken (unclosed quotes, always-FALSE): "
                    f"'{new.raw[:80]}...'. Old condition was valid. "
                    f"→ NARROWED (NBC): break introduced."
                )

        # First check if both are same simple type - handle them specifically
        # before checking for complex/unknown
        if old.type == new.type and old.type not in (ConditionType.COMPLEX, ConditionType.UNKNOWN):
            if old.type == ConditionType.PRESENCE:
                return self._analyze_presence(old, new)
            elif old.type == ConditionType.EQUALITY:
                return self._analyze_equality(old, new)
            elif old.type == ConditionType.INEQUALITY:
                return self._analyze_inequality(old, new)
            elif old.type == ConditionType.COMPARISON:
                return self._analyze_comparison(old, new)
        
        # ── Unified handler: mixed UNKNOWN ↔ EQUALITY and UNKNOWN ↔ UNKNOWN ──
        #
        # This handles all cases where one or both sides of an equality condition
        # cannot be parsed as a standard quoted EQUALITY (e.g. unquoted identifiers,
        # unquoted node references, or mixed quoted/unquoted).
        #
        # Framework (per user rules):
        #   Left side  (path like "../config/type"):  validate it resolves to a real schema node
        #   Right side (value like 'PROT_OTN' or PROT_OTN): validate it is a known
        #              identity/enum/bit (quoted or unquoted) or a real schema node (unquoted)
        #
        # Validity matrix for a single condition:
        #   left_valid AND right_valid  → condition is semantically meaningful (valid)
        #   left_valid AND NOT right_valid → right side is broken (always-FALSE)
        #   NOT left_valid AND right_valid → left side is broken (always-FALSE)
        #   NOT left_valid AND NOT right_valid → both broken (always-FALSE)
        #
        # Change classification:
        #   old_valid=False, new_valid=True  → fix (broken→valid) → RELAXED (BC)
        #   old_valid=True,  new_valid=False → break (valid→broken) → NARROWED (NBC)
        #   old_valid=False, new_valid=False → both broken → EQUIVALENT
        #   old_valid=True,  new_valid=True  → both valid, check semantic equivalence:
        #       same path + same value (or equivalent identity/enum rename) → EQUIVALENT
        #       same path + different value (genuinely different constraint) → NARROWED (NBC)
        #       different path (resolves to same node) + same value → EQUIVALENT
        #       different path (resolves to different node) → NARROWED (NBC)
        #       uncertain → NEEDS_LLM_ANALYSIS
        #
        # Note: bits and enums are NOT prefixed; identities MAY have prefix (e.g. 'oc-bgpt:IPV4_UNICAST')
        mixed_types = {ConditionType.UNKNOWN, ConditionType.EQUALITY}
        if old.type in mixed_types and new.type in mixed_types and \
                (old.type == ConditionType.UNKNOWN or new.type == ConditionType.UNKNOWN):
            result = self._analyze_mixed_equality(old, new)
            if result is not None:
                return result

        # ── COMPLEX type: try XPathFunctionParser before escalating to LLM ──
        # XPathFunctionParser handles built-in XPath functions (current(), not(),
        # derived-from(), count(), contains(), starts-with(), etc.) by extracting
        # the primary structural path and stripping predicates/functions.
        # If both conditions parse to the same primary path and same value/function
        # arguments, they are EQUIVALENT. Otherwise escalate to LLM.
        if old.type in (ConditionType.COMPLEX, ConditionType.UNKNOWN) or \
           new.type in (ConditionType.COMPLEX, ConditionType.UNKNOWN):
            try:
                try:
                    from yang_rag.comparator.helper.xpath_function_resolver import XPathFunctionParser
                except ImportError:
                    from .xpath_function_resolver import XPathFunctionParser

                # Build a minimal context for the parser (no resolver needed for structural comparison)
                class _MinCtx:
                    context_node_path = self.context_path or ''
                    old_resolver = None
                    new_resolver = None

                ctx = _MinCtx()
                parser = XPathFunctionParser()
                old_parsed_fn = parser.parse(old.raw.strip(), ctx)
                new_parsed_fn = parser.parse(new.raw.strip(), ctx)

                # If both have no resolvable path (e.g. re-match on literals) and
                # the raw expressions are identical → EQUIVALENT
                if old_parsed_fn.no_path and new_parsed_fn.no_path:
                    if old.raw.strip() == new.raw.strip():
                        return ChangeDirection.EQUIVALENT, (
                            f"Both conditions have no resolvable path and are identical: '{old.raw}'"
                        )
                    # Different no-path expressions → LLM
                    return ChangeDirection.NEEDS_LLM_ANALYSIS, (
                        f"Both conditions have no resolvable path but differ: '{old.raw}' → '{new.raw}'"
                    )

                # If both parsed successfully (no error), compare primary paths
                if not old_parsed_fn.error and not new_parsed_fn.error:
                    old_primary = (old_parsed_fn.primary_path or '').strip()
                    new_primary = (new_parsed_fn.primary_path or '').strip()

                    if old_primary and new_primary and old_primary == new_primary:
                        # Same structural path — compare function arguments
                        old_identity = old_parsed_fn.identity_arg
                        new_identity = new_parsed_fn.identity_arg
                        if old_identity is not None or new_identity is not None:
                            # derived-from / derived-from-or-self comparison
                            if old_identity == new_identity:
                                return ChangeDirection.EQUIVALENT, (
                                    f"Same derived-from path and identity: '{old.raw}' → '{new.raw}'"
                                )
                            # Different identity argument — check rename
                            if old_identity and new_identity:
                                old_id_clean = old_identity.strip("'\"")
                                new_id_clean = new_identity.strip("'\"")
                                is_rename, rename_expl = self._check_value_is_rename(old_id_clean, new_id_clean)
                                if is_rename is True:
                                    return ChangeDirection.EQUIVALENT, (
                                        f"derived-from identity renamed: '{old_id_clean}' → '{new_id_clean}'. {rename_expl}"
                                    )
                                elif is_rename is False:
                                    return ChangeDirection.NARROWED, (
                                        f"derived-from identity changed: '{old_id_clean}' → '{new_id_clean}'. {rename_expl} → NARROWED (NBC)."
                                    )
                            return ChangeDirection.NEEDS_LLM_ANALYSIS, (
                                f"Same path, different derived-from identity: '{old.raw}' → '{new.raw}'"
                            )

                        # Same primary path, same functions found, no identity arg
                        # Check if the full raw expressions are semantically equivalent
                        # by comparing canonical (prefix-resolved) forms.
                        #
                        # Strategy: expand all prefixes to module names using
                        # _resolve_path_prefix_to_canonical, then for any remaining
                        # unqualified predicate attribute (e.g. [name=...]), add the
                        # module name of the immediately preceding prefixed path step
                        # (e.g. openconfig-interfaces:interface → openconfig-interfaces:name).
                        # This handles the common case where a prefix is added to a
                        # predicate attribute name (e.g. [name=...] → [oc-if:name=...]).
                        import re as _re

                        def _get_self_module_name(pm: dict, yang_file: str) -> str:
                            """Return the current module's own name from the prefix map.

                            The module's own prefix is recorded in the prefix map as
                            own_prefix → own_module_name (see _build_prefix_map).
                            We identify it by matching the YANG file stem against the
                            module names in the map.  Falls back to the YANG file stem
                            if no match is found.
                            """
                            if not yang_file:
                                return ''
                            import os as _os
                            stem = _os.path.splitext(_os.path.basename(yang_file))[0]
                            # Prefer exact match in prefix map values
                            for mod_name in pm.values():
                                if mod_name == stem:
                                    return mod_name
                            return stem

                        def _canonicalize_condition(expr: str, pm: dict, self_module: str) -> str:
                            """Expand all prefixes and qualify unqualified predicate attrs.

                            Three-step normalization:
                            1. Expand known prefixes to full module names
                               (e.g. oc-acl:acl → openconfig-acl:acl).
                            2. Qualify unqualified predicate attributes with the module
                               name of the preceding path step
                               (e.g. openconfig-acl:acl-set[name=…] →
                                     openconfig-acl:acl-set[openconfig-acl:name=…]).
                            3. Strip the *current* module's own name from path steps
                               that belong to it (since unqualified names in the same
                               module are semantically identical to module-name:localname).
                               Cross-module references (different module name) are kept
                               as-is so they remain distinguishable.
                            """
                            # Step 1: expand known prefixes to module names
                            canonical = self._resolve_path_prefix_to_canonical(expr, pm)
                            # Step 2: for each predicate [attr=...] where attr has no
                            # module qualifier, infer the module from the preceding step.
                            # Pattern: find module:step[attr=...] and qualify attr.
                            def _qualify_predicate(m):
                                module = m.group(1)   # e.g. 'openconfig-interfaces'
                                step = m.group(2)     # e.g. 'interface'
                                attr = m.group(3)     # e.g. 'name'
                                rest = m.group(4)     # e.g. '=current()/..'
                                return f'{module}:{step}[{module}:{attr}{rest}'
                            canonical = _re.sub(
                                r'([a-zA-Z_][\w.-]+):([a-zA-Z_][\w.-]+)\[([a-zA-Z_][\w.-]+)(=)',
                                _qualify_predicate,
                                canonical
                            )
                            # Step 3: strip the current module's own name prefix so that
                            # 'openconfig-acl:acl' and 'acl' compare as equal.
                            # Only strip when the module name matches the current module;
                            # cross-module references (different module name) are kept.
                            if self_module:
                                escaped = _re.escape(self_module)
                                canonical = _re.sub(
                                    r'\b' + escaped + r':([a-zA-Z_][\w.-]*)',
                                    r'\1',
                                    canonical
                                )
                            return canonical

                        old_fns = sorted(old_parsed_fn.functions_found)
                        new_fns = sorted(new_parsed_fn.functions_found)
                        if old_fns == new_fns and old.raw.strip() == new.raw.strip():
                            return ChangeDirection.EQUIVALENT, (
                                f"Identical complex conditions: '{old.raw}'"
                            )
                        # Try canonical prefix resolution before escalating to LLM
                        if old_fns == new_fns:
                            try:
                                old_pm, new_pm = self._get_prefix_maps()
                                old_self_mod = _get_self_module_name(old_pm, self.old_yang_file or '')
                                new_self_mod = _get_self_module_name(new_pm, self.new_yang_file or '')
                                old_canonical = _canonicalize_condition(old.raw.strip(), old_pm, old_self_mod)
                                new_canonical = _canonicalize_condition(new.raw.strip(), new_pm, new_self_mod)
                                if old_canonical == new_canonical:
                                    return ChangeDirection.EQUIVALENT, (
                                        f"Equivalent complex conditions (prefix qualification added/removed): "
                                        f"'{old.raw}' → '{new.raw}' — same canonical form"
                                    )
                            except Exception:
                                pass
                        # Same structural path but different predicate content.
                        # Count predicates in old vs new to determine direction:
                        #   new has MORE predicates → NARROWED (AND logic, more restrictive)
                        #   new has FEWER predicates → RELAXED (less restrictive)
                        #   same count but different content → NEEDS_LLM_ANALYSIS
                        def _count_predicates(expr: str) -> int:
                            """Count top-level [...] predicate blocks in an XPath expression."""
                            count = 0
                            depth = 0
                            in_single = False
                            in_double = False
                            for ch in expr:
                                if ch == "'" and not in_double:
                                    in_single = not in_single
                                elif ch == '"' and not in_single:
                                    in_double = not in_double
                                elif not in_single and not in_double:
                                    if ch == '[':
                                        if depth == 0:
                                            count += 1
                                        depth += 1
                                    elif ch == ']':
                                        depth -= 1
                            return count

                        old_pred_count = _count_predicates(old.raw.strip())
                        new_pred_count = _count_predicates(new.raw.strip())

                        if new_pred_count > old_pred_count:
                            return ChangeDirection.NARROWED, (
                                f"Leafref path gained {new_pred_count - old_pred_count} additional predicate(s): "
                                f"'{old.raw}' → '{new.raw}'. "
                                f"XPath predicates are conjunctive (AND), so more predicates = more restrictive = NARROWED (NBC)."
                            )
                        elif new_pred_count < old_pred_count:
                            return ChangeDirection.RELAXED, (
                                f"Leafref path lost {old_pred_count - new_pred_count} predicate(s): "
                                f"'{old.raw}' → '{new.raw}'. "
                                f"Fewer predicates = less restrictive = RELAXED (BC)."
                            )
                        # Same predicate count but different content → LLM
                        return ChangeDirection.NEEDS_LLM_ANALYSIS, (
                            f"Same primary path but different function expressions: '{old.raw}' → '{new.raw}'. "
                            f"Functions: {old_fns} → {new_fns}"
                        )

                    # Different primary paths or parse failed → LLM
            except Exception:
                pass  # Fall through to LLM

            return ChangeDirection.NEEDS_LLM_ANALYSIS, f"Complex condition requires deeper analysis: '{old.raw}' → '{new.raw}'"
        
        # Handle NOT clauses
        if old.type == ConditionType.NOT_CLAUSE and new.type == ConditionType.NOT_CLAUSE:
            # Both negated - analyze inner conditions with flipped logic
            inner_result = self._analyze_parsed(old.subconditions[0], new.subconditions[0])
            # Flip narrowed/relaxed
            if inner_result[0] == ChangeDirection.NARROWED:
                return ChangeDirection.RELAXED, f"NOT clause: inner narrowed → outer relaxed: {inner_result[1]}"
            elif inner_result[0] == ChangeDirection.RELAXED:
                return ChangeDirection.NARROWED, f"NOT clause: inner relaxed → outer narrowed: {inner_result[1]}"
            return inner_result
        
        # Handle mixed NOT/non-NOT
        if old.type == ConditionType.NOT_CLAUSE or new.type == ConditionType.NOT_CLAUSE:
            # One is negated, other isn't - check if they're complementary
            if old.type == ConditionType.NOT_CLAUSE:
                inner = old.subconditions[0]
                if self._conditions_equivalent(inner, new):
                    return ChangeDirection.NEEDS_LLM_ANALYSIS, f"Changed from NOT({inner.raw}) to {new.raw} (complementary — logic inverted, requires deeper analysis)"
            else:
                inner = new.subconditions[0]
                if self._conditions_equivalent(old, inner):
                    return ChangeDirection.NEEDS_LLM_ANALYSIS, f"Changed from {old.raw} to NOT({inner.raw}) (complementary — logic inverted, requires deeper analysis)"
            return ChangeDirection.NEEDS_LLM_ANALYSIS, "NOT clause mixed with non-NOT — cannot determine relationship without deeper analysis"
        
        # Handle OR clauses (including when one is OR and other isn't)
        if old.type == ConditionType.OR_CLAUSE or new.type == ConditionType.OR_CLAUSE:
            # Always call with (old, new) - _analyze_or_clauses handles all cases
            return self._analyze_or_clauses(old, new)
        
        # Handle AND clauses
        if old.type == ConditionType.AND_CLAUSE and new.type == ConditionType.AND_CLAUSE:
            return self._analyze_and_clauses(old, new)
        
        # Handle SIMPLE → AND_CLAUSE: Adding AND clause is NARROWED (more restrictive)
        # Example: "admin-state = 'enabled'" → "admin-state = 'enabled' and ip-address"
        if old.type not in (ConditionType.OR_CLAUSE, ConditionType.AND_CLAUSE) and \
           new.type == ConditionType.AND_CLAUSE:
            # Check if old condition is preserved in new AND clause
            if old.raw in new.raw:
                return ChangeDirection.NARROWED, f"Added AND requirement(s) to existing condition: '{old.raw}' → '{new.raw}'"
            else:
                # Old condition not preserved - needs deeper analysis
                return ChangeDirection.NEEDS_LLM_ANALYSIS, f"Complex AND clause change: '{old.raw}' → '{new.raw}'"
        
        # Handle AND_CLAUSE → SIMPLE: Removing AND clause is RELAXED (less restrictive)
        if old.type == ConditionType.AND_CLAUSE and \
           new.type not in (ConditionType.OR_CLAUSE, ConditionType.AND_CLAUSE):
            # Check if new condition was in old AND clause
            if new.raw in old.raw:
                return ChangeDirection.RELAXED, f"Removed AND requirement(s) from condition: '{old.raw}' → '{new.raw}'"
            else:
                # New condition not in old - needs deeper analysis
                return ChangeDirection.NEEDS_LLM_ANALYSIS, f"Complex AND clause removal: '{old.raw}' → '{new.raw}'"
        
        # Handle mixed OR/AND (complex - needs DSPy)
        if old.type in (ConditionType.OR_CLAUSE, ConditionType.AND_CLAUSE) or \
           new.type in (ConditionType.OR_CLAUSE, ConditionType.AND_CLAUSE):
            return ChangeDirection.NEEDS_LLM_ANALYSIS, f"Mixed OR/AND logic requires deeper analysis: '{old.raw}' → '{new.raw}'"
        
        # Different types - needs analysis (e.g. EQUALITY changed to PRESENCE or vice versa)
        return ChangeDirection.NEEDS_LLM_ANALYSIS, (
            f"Condition type changed: {old.type.value} → {new.type.value} "
            f"('{old.raw}' → '{new.raw}') — requires deeper analysis"
        )
    
    def _get_xpath_resolvers(self):
        """Lazy-load XPath resolvers for old and new YANG files.

        Uses the process-level ``get_cached_xpath_resolver`` so that the same
        YANG file is parsed by pyang at most once per process, regardless of
        how many ``ConditionAnalyzer`` instances are created.
        """
        if not self.enable_xpath_resolution:
            return None, None

        if self._old_resolver is None and self.old_yang_file:
            try:
                try:
                    from yang_rag.comparator.helper.xpath_resolver import get_cached_xpath_resolver
                except ImportError:
                    from .xpath_resolver import get_cached_xpath_resolver
                self._old_resolver = get_cached_xpath_resolver(self.old_yang_file, self.search_dirs)
            except Exception as e:
                print(f"[ConditionAnalyzer] Failed to load old XPath resolver: {e}")

        if self._new_resolver is None and self.new_yang_file:
            try:
                try:
                    from yang_rag.comparator.helper.xpath_resolver import get_cached_xpath_resolver
                except ImportError:
                    from .xpath_resolver import get_cached_xpath_resolver
                self._new_resolver = get_cached_xpath_resolver(self.new_yang_file, self.search_dirs)
            except Exception as e:
                print(f"[ConditionAnalyzer] Failed to load new XPath resolver: {e}")

        return self._old_resolver, self._new_resolver

    def _build_prefix_map(self, yang_file: str) -> Dict[str, str]:
        """
        Build a map of import prefix → canonical defining module name using pyang.

        For each `import` statement in the YANG module, this resolves the prefix
        to the actual module being imported. It then recursively follows the import
        chain to find the canonical defining module for each identity.

        Example:
            module-a imports module-b { prefix mb; }
            module-b imports module-c { prefix mc; }
            module-c defines identity L2P2P

            Result: {'mb': 'module-b', 'mc': 'module-c', 'ma': 'module-a'}
            When resolving 'mb:L2P2P': prefix 'mb' → module-b, but L2P2P is
            defined in module-c (which module-b imports). The canonical name is
            'module-c:L2P2P' regardless of the import chain depth.

        Args:
            yang_file: Path to the YANG file to build the prefix map for

        Returns:
            Dict mapping prefix string → imported module name (direct import only)
            Returns empty dict if pyang loading fails

        Results are cached in the process-level ``_prefix_map_cache`` so that
        the same YANG file is parsed at most once per (file, search_dirs) pair.
        """
        from pathlib import Path as _Path
        abs_yang = str(_Path(yang_file).resolve())
        search_dirs = self._prioritized_search_dirs(yang_file)
        cache_key = (abs_yang, tuple(sorted(search_dirs)))

        if cache_key in _prefix_map_cache:
            return _prefix_map_cache[cache_key]

        try:
            try:
                from yang_rag.comparator.helper.pyang_utils import PyangContext
            except ImportError:
                from .pyang_utils import PyangContext

            ctx = PyangContext(search_dirs=search_dirs)
            mod = ctx.load_module(yang_file)
            if mod is None:
                _prefix_map_cache[cache_key] = {}
                return {}

            prefix_map: Dict[str, str] = {}

            def _collect_prefixes_from_stmt(stmt_node):
                """Collect prefix→module mappings from a module or submodule statement."""
                # Add the module/submodule's own prefix
                own_pfx = stmt_node.search_one('prefix')
                if own_pfx:
                    prefix_map[own_pfx.arg] = stmt_node.arg
                # Add all imported module prefixes
                for sub in stmt_node.substmts:
                    if sub.keyword == 'import':
                        imported_module_name = sub.arg
                        prefix_stmt = sub.search_one('prefix')
                        if prefix_stmt:
                            prefix_map[prefix_stmt.arg] = imported_module_name

            # Collect from the main module
            _collect_prefixes_from_stmt(mod)

            # Also collect from all included submodules so that prefixes defined
            # in submodule import statements (e.g. 'oc-ospf-types' imported in
            # openconfig-ospfv2-lsdb) are available when validating when/must
            # conditions that live in those submodules.
            try:
                pyang_ctx = ctx.ctx
                module_name = mod.arg
                for (_mod_name, _rev), sub_stmt in pyang_ctx.modules.items():
                    kw = getattr(sub_stmt, 'keyword', None)
                    if kw != 'submodule':
                        continue
                    # Check belongs-to
                    for child in getattr(sub_stmt, 'substmts', []):
                        if getattr(child, 'keyword', None) == 'belongs-to':
                            if getattr(child, 'arg', None) == module_name:
                                _collect_prefixes_from_stmt(sub_stmt)
                                break
            except Exception:
                pass  # Fall back to main module only

            _prefix_map_cache[cache_key] = prefix_map
            return prefix_map
        except Exception:
            _prefix_map_cache[cache_key] = {}
            return {}

    def _resolve_identity_to_canonical(
        self,
        prefix: str,
        local_name: str,
        prefix_map: Dict[str, str],
        yang_file: str
    ) -> str:
        """
        Resolve a prefixed identity value to its canonical 'defining_module:local_name' form.

        This follows the import chain to find the module that actually DEFINES the identity,
        not just the module that imports it.

        Example:
            prefix='mb', local_name='L2P2P', prefix_map={'mb': 'module-b'}
            module-b imports module-c which defines L2P2P
            → returns 'module-c:L2P2P'

        Args:
            prefix:     The namespace prefix (e.g. 'mb')
            local_name: The identity local name (e.g. 'L2P2P')
            prefix_map: Dict of prefix → imported module name
            yang_file:  Path to the YANG file (for search path)

        Returns:
            Canonical identity string 'defining_module:local_name', or
            'prefix:local_name' if resolution fails
        """
        if prefix not in prefix_map:
            return f'{prefix}:{local_name}'

        imported_module_name = prefix_map[prefix]

        try:
            try:
                from yang_rag.comparator.helper.pyang_utils import PyangContext
            except ImportError:
                from .pyang_utils import PyangContext

            search_dirs = self._prioritized_search_dirs(yang_file)

            ctx = PyangContext(search_dirs=search_dirs)
            # Load the imported module to check if it defines the identity
            # or re-imports it from another module
            # We search for the module by name in the search path
            import os
            for d in search_dirs:
                candidate = os.path.join(d, f'{imported_module_name}.yang')
                if os.path.exists(candidate):
                    imported_mod = ctx.load_module(candidate)
                    if imported_mod is None:
                        break
                    # Check if this module defines the identity directly
                    for stmt in imported_mod.substmts:
                        if stmt.keyword == 'identity' and stmt.arg == local_name:
                            return f'{imported_module_name}:{local_name}'
                    # Identity not defined here — check if it's re-imported
                    # Build prefix map for the imported module and recurse
                    for stmt in imported_mod.substmts:
                        if stmt.keyword == 'import':
                            sub_prefix_stmt = stmt.search_one('prefix')
                            if sub_prefix_stmt:
                                # Try to find the identity in this sub-import
                                sub_result = self._resolve_identity_to_canonical(
                                    sub_prefix_stmt.arg, local_name,
                                    {sub_prefix_stmt.arg: stmt.arg},
                                    candidate
                                )
                                if not sub_result.startswith(sub_prefix_stmt.arg + ':'):
                                    # Successfully resolved to a deeper module
                                    return sub_result
                    break
        except Exception:
            pass

        return f'{imported_module_name}:{local_name}'

    def _validate_xpath_rhs(
        self,
        raw_value: str,
        yang_file: Optional[str],
        prefix_map: Optional[Dict[str, str]],
        resolver=None,
        lhs_path: Optional[str] = None,
        context_path: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Validate the right-hand side of an XPath equality condition.

        CRITICAL DISTINCTION — quoted vs unquoted:
          Quoted:   'PROT_ETHERNET' or 'oc-bgpt:IPV4_UNICAST'
                    → XPath string literal → valid if it is a known identity/enum/bit
          Unquoted: PROT_OTN, LOGICAL_CHANNEL, assignment-type
                    → XPath location path (node selector) → valid ONLY if it resolves
                      to a real schema node. An unquoted identifier that happens to
                      match an identity/enum name is NOT valid — it is a node selector
                      that evaluates to an empty node-set → comparison is always-FALSE.

        Handles three forms:
          1. Quoted string literal: 'PROT_ETHERNET' or 'oc-bgpt:IPV4_UNICAST'
             → valid if the local name (after stripping prefix) is a known
               identity/enum/bit in the referenced module (or the current module).
          2. Unquoted identifier: PROT_OTN, LOGICAL_CHANNEL, prot-ethernet
             → valid ONLY if it resolves as a real schema node via the resolver.
               Identity/enum names are NOT valid here — they are node selectors.
          3. Unquoted path: ../config/type
             → valid if it resolves to a real schema node via the resolver.

        Returns:
            (is_valid: bool, reason: str)
            is_valid=True  → the value is semantically meaningful
            is_valid=False → the value is broken/undefined (always-FALSE)
            is_valid=None  → cannot determine (insufficient info)
        """
        if raw_value is None:
            return None, "No value provided"

        raw_stripped = raw_value.strip()
        # Determine if the original value was quoted
        is_quoted = (raw_stripped.startswith("'") and raw_stripped.endswith("'")) or \
                    (raw_stripped.startswith('"') and raw_stripped.endswith('"'))
        stripped = raw_stripped.strip("'\"")
        lhs_value_kind = self._infer_lhs_value_kind(lhs_path, resolver, context_path)

        if is_quoted:
            # ── Case 1: Quoted string literal ──
            # Valid if it is a known identity/enum/bit (with or without prefix).
            if not yang_file:
                return None, "Cannot validate quoted value without module info"

            # Check with prefix (e.g. 'oc-bgpt:IPV4_UNICAST')
            if ':' in stripped:
                prefix, local = stripped.split(':', 1)
                if prefix_map and prefix in prefix_map:
                    import os
                    search_dirs = self._prioritized_search_dirs(yang_file)
                    module_name = prefix_map[prefix]
                    for d in search_dirs:
                        candidate = os.path.join(d, f'{module_name}.yang')
                        if os.path.exists(candidate):
                            names_by_kind = self._collect_identity_enum_bit_names(candidate)
                            if names_by_kind is not None:
                                id_names = names_by_kind.get('identity', set())
                                enum_names = names_by_kind.get('enum', set())
                                bit_names = names_by_kind.get('bit', set())
                                in_identity = local in id_names
                                in_enum = local in enum_names
                                in_bit = local in bit_names

                                if lhs_value_kind == 'identityref':
                                    if in_identity:
                                        return True, (
                                            f"Prefixed identity value '{stripped}' is valid for identityref LHS"
                                        )
                                    return False, (
                                        f"Prefixed value '{stripped}' is not a known identity for identityref LHS"
                                    )

                                if lhs_value_kind in {'enumeration', 'bits'}:
                                    if in_enum or in_bit:
                                        return False, (
                                            f"Prefixed value '{stripped}' is invalid for {lhs_value_kind} LHS "
                                            f"(enum/bit labels are unprefixed)"
                                        )
                                    return False, (
                                        f"Prefixed value '{stripped}' is not valid for {lhs_value_kind} LHS"
                                    )

                                # Unknown LHS type: discriminate by symbol kind where possible.
                                if in_identity and not in_enum and not in_bit:
                                    return True, (
                                        f"Prefixed value '{stripped}' resolved as identity in module '{module_name}'"
                                    )
                                if (in_enum or in_bit) and not in_identity:
                                    return False, (
                                        f"Prefixed value '{stripped}' resolved as enum/bit label; "
                                        f"enum/bit labels are unprefixed"
                                    )
                                if in_identity and (in_enum or in_bit):
                                    return None, (
                                        f"Prefixed value '{stripped}' is ambiguous (identity and enum/bit match)"
                                    )
                                return False, (
                                    f"Prefixed value '{stripped}': local name '{local}' not in module '{module_name}'"
                                )
                            break
                    return None, f"Could not load module for prefix '{prefix}'"
                else:
                    return None, f"Unknown prefix '{prefix}' in value '{stripped}'"
            else:
                # No prefix — check in current module and all imported modules
                names_by_kind = self._collect_identity_enum_bit_names(yang_file)
                if names_by_kind is not None:
                    id_names = names_by_kind.get('identity', set())
                    enum_names = names_by_kind.get('enum', set())
                    bit_names = names_by_kind.get('bit', set())

                    in_identity = stripped in id_names
                    in_enum = stripped in enum_names
                    in_bit = stripped in bit_names

                    if lhs_value_kind == 'identityref' and in_identity:
                        return True, f"Quoted identity value '{stripped}' is valid for identityref LHS"

                    if lhs_value_kind == 'enumeration' and in_enum:
                        return True, f"Quoted enum value '{stripped}' is valid for enumeration LHS"

                    if lhs_value_kind == 'bits' and in_bit:
                        return True, f"Quoted bit value '{stripped}' is valid for bits LHS"

                    if in_identity or in_enum or in_bit:
                        return True, f"Quoted value '{stripped}' found in module"
                    # Check imported modules — but with different rules per identity vs enum/bit:
                    #
                    # - identity values from IMPORTED modules require a prefix.
                    #   An unqualified identity value is only valid when the identity
                    #   is defined in the same module or an included submodule.
                    #   → return False (invalid) if found only in an imported module.
                    #
                    # - enum/bit labels are never prefixed regardless of where they
                    #   are defined (they are local to the type definition).
                    #   → return True if found in an imported module's enum/bit.
                    if prefix_map:
                        import os
                        search_dirs = self._prioritized_search_dirs(yang_file)
                        for mod_name in prefix_map.values():
                            for d in search_dirs:
                                candidate = os.path.join(d, f'{mod_name}.yang')
                                if os.path.exists(candidate):
                                    imp_names_by_kind = self._collect_identity_enum_bit_names(candidate)
                                    if imp_names_by_kind:
                                        imp_id = imp_names_by_kind.get('identity', set())
                                        imp_enum = imp_names_by_kind.get('enum', set())
                                        imp_bit = imp_names_by_kind.get('bit', set())

                                        imp_in_identity = stripped in imp_id
                                        imp_in_enum = stripped in imp_enum
                                        imp_in_bit = stripped in imp_bit

                                        # Identity in imported module → prefix is mandatory → INVALID
                                        if imp_in_identity and lhs_value_kind in ('identityref', None):
                                            return False, (
                                                f"Unqualified identity value '{stripped}' found in imported module "
                                                f"'{mod_name}' — prefix required"
                                            )

                                        # Enum/bit in imported module → no prefix needed → VALID
                                        if lhs_value_kind == 'enumeration' and imp_in_enum:
                                            return True, (
                                                f"Quoted enum value '{stripped}' found in imported module '{mod_name}'"
                                            )
                                        if lhs_value_kind == 'bits' and imp_in_bit:
                                            return True, (
                                                f"Quoted bit value '{stripped}' found in imported module '{mod_name}'"
                                            )
                                        if lhs_value_kind is None and (imp_in_enum or imp_in_bit):
                                            return True, (
                                                f"Quoted enum/bit value '{stripped}' found in imported module '{mod_name}'"
                                            )
                                    break
                    if lhs_value_kind == 'identityref':
                        return False, (
                            f"Quoted value '{stripped}' is not a valid identity for identityref LHS "
                            f"(not found in module/imports)"
                        )
                    if lhs_value_kind == 'enumeration':
                        return False, (
                            f"Quoted value '{stripped}' is not a valid enum label for enumeration LHS "
                            f"(not found in module/imports)"
                        )
                    if lhs_value_kind == 'bits':
                        return False, (
                            f"Quoted value '{stripped}' is not a valid bit label for bits LHS "
                            f"(not found in module/imports)"
                        )

                    return False, f"Quoted value '{stripped}' not found in module or imports (always-FALSE)"
                return None, "Could not load module for identity/enum check"

        else:
            # ── Case 2/3: Unquoted identifier or path ──
            # In XPath 1.0, an unquoted name after '=' is a LOCATION PATH (node selector).
            # The expression "path = UNQUOTED" means: select child elements named UNQUOTED
            # from the node-set returned by 'path'. Since 'path' is typically a leaf node
            # (not a container), it has no children → the comparison is ALWAYS FALSE.
            #
            # CRITICAL: Even if UNQUOTED happens to match an identity/enum name or a schema
            # node name, it is still invalid as an RHS value — it is a node selector, not a
            # string literal. The correct form is always quoted: 'UNQUOTED'.
            #
            # Therefore: any unquoted RHS in an equality condition is ALWAYS broken (False).
            return False, (
                f"Unquoted RHS '{stripped}' is a location path (node selector) in XPath 1.0, "
                f"not a string literal. Leaf nodes have no children → comparison is always-FALSE."
            )

    def _infer_lhs_value_kind(
        self,
        lhs_path: Optional[str],
        resolver,
        context_path: Optional[str],
    ) -> Optional[str]:
        """
        Infer the LHS leaf value kind for a condition path.

        Returns one of: 'identityref', 'enumeration', 'bits', or None if unknown.

        When the direct resolution fails (e.g. because context_path is an abstract
        grouping path that does not exist in the instantiated schema tree), this method
        falls back to ``resolve_xpath_in_data_context`` to find an instantiated context
        where the path can be resolved.  This prevents false-negative ``new=None``
        results in ``_validate_xpath_rhs`` that would otherwise cause the
        ``_classify_prefix_variant`` helper to return ``NEEDS_LLM_ANALYSIS`` (and thus
        a conservative NBC tag) even when the change is clearly RELAXED (BC).
        """
        if not lhs_path or resolver is None or not context_path:
            return None

        try:
            resolved, _ctx_used, _fallback = self._resolve_xpath_with_parent_fallback(
                resolver, context_path, lhs_path
            )
            if resolved.success and resolved.target_node is not None:
                target = resolved.target_node
                if getattr(target, 'keyword', None) not in {'leaf', 'leaf-list'}:
                    return None
                type_stmt = target.search_one('type')
                return self._extract_value_kind_from_type_stmt(type_stmt)

            # Direct resolution failed — try data_context_fallback via
            # resolve_xpath_in_data_context (used when context_path is a grouping
            # path that has no direct schema node).
            if hasattr(resolver, 'resolve_xpath_in_data_context'):
                try:
                    candidates = resolver.resolve_xpath_in_data_context(context_path, lhs_path)
                except Exception:
                    candidates = []

                for candidate in (candidates or []):
                    if not getattr(candidate, 'success', False):
                        continue
                    target = getattr(candidate, 'target_node', None)
                    if target is None:
                        continue
                    if getattr(target, 'keyword', None) not in {'leaf', 'leaf-list'}:
                        continue
                    type_stmt = target.search_one('type')
                    kind = self._extract_value_kind_from_type_stmt(type_stmt)
                    if kind is not None:
                        self._debug(
                            f"_infer_lhs_value_kind data_context_fallback "
                            f"path={lhs_path!r} context={context_path!r} → kind={kind!r}"
                        )
                        return kind

            return None
        except Exception:
            return None

    def _extract_value_kind_from_type_stmt(
        self,
        type_stmt,
        visited: Optional[Set[int]] = None,
    ) -> Optional[str]:
        """Resolve a YANG type statement to identityref/enumeration/bits when possible."""
        if type_stmt is None:
            return None

        if visited is None:
            visited = set()

        stmt_id = id(type_stmt)
        if stmt_id in visited:
            return None
        visited.add(stmt_id)

        base = getattr(type_stmt, 'arg', None)
        if base in {'identityref', 'enumeration', 'bits'}:
            return base

        spec = getattr(type_stmt, 'i_type_spec', None)
        if spec is not None:
            spec_name = spec.__class__.__name__.lower()
            if 'identity' in spec_name:
                return 'identityref'
            if 'enum' in spec_name:
                return 'enumeration'
            if 'bit' in spec_name:
                return 'bits'

        typedef_stmt = getattr(type_stmt, 'i_typedef', None)
        if typedef_stmt is not None:
            typedef_type = typedef_stmt.search_one('type')
            resolved = self._extract_value_kind_from_type_stmt(typedef_type, visited)
            if resolved:
                return resolved

        return None

    def _analyze_mixed_equality(
        self,
        old: 'ParsedCondition',
        new: 'ParsedCondition',
    ) -> Optional[Tuple['ChangeDirection', str]]:
        """
        Unified handler for mixed UNKNOWN ↔ EQUALITY and UNKNOWN ↔ UNKNOWN cases.

        Parses both sides of the equality condition (path = value) from the raw
        expression, validates each side independently, then classifies the change
        based on the validity matrix described in _analyze_parsed.

        Returns None if the conditions cannot be parsed as equality expressions
        (caller should fall through to NEEDS_LLM_ANALYSIS).
        """
        import re as _re_me

        # Broad equality pattern: path = value (quoted or unquoted)
        # Handles: ../x = 'val', ../x = VAL, ../x = prefix:VAL, ../x = val-with-hyphens
        _eq_pat = _re_me.compile(
            r'''^\s*(["']?[a-zA-Z0-9_\-./:\[\]@]+["']?)\s*=\s*(["'][^"']*["']|[a-zA-Z][a-zA-Z0-9_\-:]*)\s*$'''
        )

        old_m = _eq_pat.match(old.raw.strip())
        new_m = _eq_pat.match(new.raw.strip())

        if not old_m or not new_m:
            return None  # Cannot parse — fall through

        old_path_raw = old_m.group(1).strip().strip("'\"")
        old_val_raw  = old_m.group(2).strip()
        new_path_raw = new_m.group(1).strip().strip("'\"")
        new_val_raw  = new_m.group(2).strip()

        self._debug(
            "mixed_equality parsed "
            f"old_lhs={old_path_raw!r} old_rhs={old_val_raw!r} "
            f"new_lhs={new_path_raw!r} new_rhs={new_val_raw!r}"
        )
        self._last_analysis_details['mixed_equality'] = {
            'old_lhs': old_path_raw,
            'old_rhs': old_val_raw,
            'new_lhs': new_path_raw,
            'new_rhs': new_val_raw,
        }

        old_val_is_quoted = old_val_raw.startswith(("'", '"'))
        new_val_is_quoted = new_val_raw.startswith(("'", '"'))
        old_val_clean = old_val_raw.strip("'\"")
        new_val_clean = new_val_raw.strip("'\"")

        # ── Validate left side (path) ──
        old_resolver, new_resolver = self._get_xpath_resolvers()
        old_prefix_map, new_prefix_map = self._get_prefix_maps()

        def _path_valid(path, resolver, context_path):
            if not resolver or not context_path:
                return None  # Unknown
            try:
                r, _resolved_context, _fallback_used = self._resolve_xpath_with_parent_fallback(
                    resolver, context_path, path
                )
                if r.success:
                    return True

                error_text = (r.error or '').lower()
                path_text = (path or '').strip()
                # Resolver edge cases (typically cross-module absolute/prefixed paths)
                # should be escalated to LLM, not treated as definitely broken.
                if path_text.startswith('/') and (
                    'expanded schema tree' in error_text or
                    'not found under parent' in error_text
                ):
                    return None

                # ── Grouping-context fallback ──────────────────────────────────
                # When the context_path is a grouping-level path (e.g.
                # /module/my-grouping/state), relative XPaths like
                # "../config/protocol-type" cannot be resolved directly because
                # groupings are not instantiated data nodes.  Try resolving from
                # each instantiation point of the grouping instead.
                try:
                    inst_results = resolver.resolve_xpath_from_grouping_context(
                        context_path, path
                    )
                    if inst_results:
                        return True
                except Exception:
                    pass

                return False
            except Exception:
                return None

        old_path_valid = _path_valid(old_path_raw, old_resolver, self.context_path)
        new_path_valid = _path_valid(new_path_raw, new_resolver, self.context_path)

        # ── Override path validity using pyang validation results ──
        # Pyang's XPATH_NODE_NOT_FOUND* errors flag the *whole* condition as broken,
        # but for equality conditions (path = value) the error may be caused by the
        # RHS (e.g. an unquoted identity value treated as a location path) rather than
        # the LHS path.  We therefore only apply the pyang broken override when the
        # XPath resolver could NOT confirm the LHS path as valid (i.e. returned None
        # or False).  If the resolver already returned True, the path exists in the
        # schema and we trust that result — the pyang error is about the RHS.
        #
        # Conversely, if pyang did NOT report an error for the new condition, we treat
        # the new LHS path as valid (True) — overriding a False/None from the XPath
        # resolver which may fail to resolve downward paths (e.g. "config/leaf") from
        # grouping contexts.
        if self._old_pyang_broken_lines is not None and self._is_pyang_broken(
            old.raw,
            'old',
            report_context=self.context_path,
            resolver=old_resolver,
        ):
            if old_path_valid is True:
                # XPath resolver confirmed the LHS path exists → pyang error is about
                # the RHS (e.g. unquoted identity value), not the path.  Do not override.
                self._debug(
                    f"pyang_override old_path_valid: skipping override "
                    f"(resolver confirmed path valid, pyang error is likely RHS-only: {old.raw!r})"
                )
            else:
                if old_path_valid is not False:
                    self._debug(
                        f"pyang_override old_path_valid: {old_path_valid!r} → False "
                        f"(pyang confirmed broken and resolver did not confirm valid: {old.raw!r})"
                    )
                old_path_valid = False

        if self._new_pyang_broken_lines is not None:
            # We have pyang data for the new file (even if the set is empty, meaning
            # pyang ran and found no XPATH errors → new conditions are valid).
            # If the new condition is NOT in the broken set, pyang validated it
            # successfully → treat LHS as valid (True).
            if not self._is_pyang_broken(
                new.raw,
                'new',
                report_context=self.context_path,
                resolver=new_resolver,
            ):
                if new_path_valid is not True:
                    self._debug(
                        f"pyang_override new_path_valid: {new_path_valid!r} → True "
                        f"(pyang found no XPATH error for: {new.raw!r})"
                    )
                new_path_valid = True
            else:
                # New condition IS in the broken set → definitively broken, but only
                # override if the XPath resolver did not already confirm the path valid.
                # (Same reasoning as for old: pyang error may be RHS-only.)
                if new_path_valid is True:
                    self._debug(
                        f"pyang_override new_path_valid: skipping override "
                        f"(resolver confirmed path valid, pyang error is likely RHS-only: {new.raw!r})"
                    )
                else:
                    if new_path_valid is not False:
                        self._debug(
                            f"pyang_override new_path_valid: {new_path_valid!r} → False "
                            f"(pyang confirmed broken and resolver did not confirm valid: {new.raw!r})"
                        )
                    new_path_valid = False

        # ── Validate right side (value) ──
        old_rhs_valid, old_rhs_reason = self._validate_xpath_rhs(
            old_val_raw,
            self.old_yang_file,
            old_prefix_map,
            old_resolver,
            lhs_path=old_path_raw,
            context_path=self.context_path,
        )
        new_rhs_valid, new_rhs_reason = self._validate_xpath_rhs(
            new_val_raw,
            self.new_yang_file,
            new_prefix_map,
            new_resolver,
            lhs_path=new_path_raw,
            context_path=self.context_path,
        )

        self._debug(
            "mixed_equality resolution "
            f"old_lhs_valid={old_path_valid} old_rhs_valid={old_rhs_valid} old_rhs_reason={old_rhs_reason!r} "
            f"new_lhs_valid={new_path_valid} new_rhs_valid={new_rhs_valid} new_rhs_reason={new_rhs_reason!r}"
        )
        self._last_analysis_details['mixed_equality'].update({
            'old_lhs_valid': old_path_valid,
            'old_rhs_valid': old_rhs_valid,
            'old_rhs_reason': old_rhs_reason,
            'new_lhs_valid': new_path_valid,
            'new_rhs_valid': new_rhs_valid,
            'new_rhs_reason': new_rhs_reason,
        })

        # ── Determine overall condition validity ──
        # Strict rule: if EITHER side cannot be resolved (None) or is definitively
        # broken (False), the condition is always-FALSE.
        # Rationale: an unresolvable path means the node doesn't exist in the schema
        # → XPath returns empty node-set → comparison is always-FALSE.
        # Similarly, an unresolvable identity/enum/bit means the value is invalid
        # → comparison is always-FALSE.
        # Only True AND True → the condition is semantically valid.
        def _overall_valid(path_v, rhs_v):
            if path_v is None or rhs_v is None:
                return None
            if path_v is True and rhs_v is True:
                return True
            return False

        old_valid = _overall_valid(old_path_valid, old_rhs_valid)
        new_valid = _overall_valid(new_path_valid, new_rhs_valid)

        self._debug(
            f"mixed_equality overall_valid old={old_valid} new={new_valid}"
        )
        self._last_analysis_details['mixed_equality'].update({
            'old_overall_valid': old_valid,
            'new_overall_valid': new_valid,
        })

        # ── Classify based on validity matrix ──
        if old_valid is None or new_valid is None:
            self._debug("mixed_equality escalation reason=unresolved_xpath_edge_case")
            return ChangeDirection.NEEDS_LLM_ANALYSIS, (
                f"Unresolved XPath edge case during operand validation: "
                f"old='{old.raw}' (path_valid={old_path_valid}, rhs_valid={old_rhs_valid}: {old_rhs_reason}), "
                f"new='{new.raw}' (path_valid={new_path_valid}, rhs_valid={new_rhs_valid}: {new_rhs_reason}). "
                f"Requires LLM analysis."
            )

        if old_valid is False and new_valid is False:
            self._debug("mixed_equality decision=equivalent reason=both_broken")
            return ChangeDirection.EQUIVALENT, (
                f"Both conditions are broken (always-FALSE): "
                f"old='{old.raw}' (path_valid={old_path_valid}, rhs_valid={old_rhs_valid}: {old_rhs_reason}), "
                f"new='{new.raw}' (path_valid={new_path_valid}, rhs_valid={new_rhs_valid}: {new_rhs_reason}). "
                f"→ EQUIVALENT (both always-FALSE)."
            )

        if old_valid is False and new_valid is True:
            self._debug("mixed_equality decision=relaxed reason=broken_to_valid")
            return ChangeDirection.RELAXED, (
                f"Condition fixed (broken → valid): "
                f"old='{old.raw}' was always-FALSE ({old_rhs_reason}), "
                f"new='{new.raw}' is semantically valid ({new_rhs_reason}). "
                f"→ RELAXED (BC): new version accepts data that old rejected."
            )

        if old_valid is True and new_valid is False:
            self._debug("mixed_equality decision=narrowed reason=valid_to_broken")
            return ChangeDirection.NARROWED, (
                f"Condition broken (valid → broken): "
                f"old='{old.raw}' was semantically valid ({old_rhs_reason}), "
                f"new='{new.raw}' is always-FALSE ({new_rhs_reason}). "
                f"→ NARROWED (NBC): new version rejects data that old accepted."
            )

        if old_valid is True and new_valid is True:
            # Both valid — check semantic equivalence
            # Same path (or resolves to same node)?
            same_path = (old_path_raw == new_path_raw)
            if not same_path:
                # Try canonical path resolution
                if old_prefix_map or new_prefix_map:
                    old_canon = self._resolve_path_prefix_to_canonical(old_path_raw, old_prefix_map)
                    new_canon = self._resolve_path_prefix_to_canonical(new_path_raw, new_prefix_map)
                    same_path = (old_canon == new_canon)

            if same_path:
                # Same path — compare values
                if old_val_clean == new_val_clean:
                    self._debug("mixed_equality decision=equivalent reason=same_path_same_value")
                    return ChangeDirection.EQUIVALENT, (
                        f"Same path and same value (quoting changed only): "
                        f"'{old.raw}' → '{new.raw}' → EQUIVALENT."
                    )
                # Different values — check if it's an identity/enum rename
                is_rename, rename_expl = self._check_value_is_rename(old_val_clean, new_val_clean)
                if is_rename is True:
                    self._debug("mixed_equality decision=equivalent reason=rename_detected")
                    return ChangeDirection.EQUIVALENT, (
                        f"Identity/enum renamed: '{old_val_clean}' → '{new_val_clean}'. "
                        f"{rename_expl} → EQUIVALENT (BC)."
                    )
                elif is_rename is False:
                    self._debug("mixed_equality decision=narrowed reason=same_path_different_value_not_rename")
                    return ChangeDirection.NARROWED, (
                        f"Same path, different value (not a rename): "
                        f"'{old_val_clean}' → '{new_val_clean}'. "
                        f"{rename_expl} → NARROWED (NBC)."
                    )
                # is_rename is None — uncertain
                self._debug("mixed_equality escalation reason=same_path_different_value_uncertain_rename")
                return ChangeDirection.NEEDS_LLM_ANALYSIS, (
                    f"Same path, different value — cannot determine if rename or genuine change: "
                    f"'{old.raw}' → '{new.raw}'. Requires LLM analysis."
                )
            else:
                # Different paths — escalate to LLM
                self._debug("mixed_equality escalation reason=both_valid_paths_differ")
                return ChangeDirection.NEEDS_LLM_ANALYSIS, (
                    f"Both conditions valid but paths differ: "
                    f"'{old_path_raw}' → '{new_path_raw}'. "
                    f"Cannot determine semantic equivalence without deeper analysis."
                )

        # At least one side is uncertain (None) — escalate to LLM
        self._debug("mixed_equality escalation reason=operand_validation_incomplete")
        return ChangeDirection.NEEDS_LLM_ANALYSIS, (
            f"Cannot fully validate condition operands: "
            f"old='{old.raw}' (path={old_path_valid}, rhs={old_rhs_valid}), "
            f"new='{new.raw}' (path={new_path_valid}, rhs={new_rhs_valid}). "
            f"Requires LLM analysis."
        )

    def _get_prefix_maps(self) -> Tuple[Dict[str, str], Dict[str, str]]:
        """Lazy-load prefix maps for old and new YANG files."""
        if self._old_prefix_map is None:
            if self.old_yang_file and self.enable_xpath_resolution:
                self._old_prefix_map = self._build_prefix_map(self.old_yang_file)
            else:
                self._old_prefix_map = {}

        if self._new_prefix_map is None:
            if self.new_yang_file and self.enable_xpath_resolution:
                self._new_prefix_map = self._build_prefix_map(self.new_yang_file)
            else:
                self._new_prefix_map = {}

        return self._old_prefix_map, self._new_prefix_map

    def _resolve_xpath_with_parent_fallback(self, resolver, context_path: str, path_expr: str):
        """
        Resolve XPath from current context.

        Only data-context/grouping-aware fallback is allowed. We intentionally do
        not perform generic parent fallback for plain relative paths because that
        can incorrectly turn structurally invalid expressions into valid ones.
        """
        resolved = resolver.resolve_xpath(context_path, path_expr)

        # Prefer grouping/data-context-aware resolutions when available.
        # This handles report contexts that include grouping-only segments while
        # preserving the original direct resolution path for simple cases.
        if not resolved.success and hasattr(resolver, 'resolve_xpath_in_data_context'):
            try:
                resolved_candidates = resolver.resolve_xpath_in_data_context(context_path, path_expr)
            except Exception:
                resolved_candidates = []

            if resolved_candidates:
                best = resolved_candidates[0]
                best_context = getattr(best, 'context_path', context_path) or context_path
                self._debug(
                    "path_resolve data_context_fallback "
                    f"path={path_expr!r} original_context={context_path!r} "
                    f"resolved_context={best_context!r} abs={best.absolute_path!r}"
                )
                return best, best_context, True
        return resolved, context_path, False

    def _iter_statements_recursive(self, stmt):
        """Yield a statement and all descendants."""
        if stmt is None:
            return
        yield stmt
        try:
            from .pyang_utils import PyangStatementHelper
        except ImportError:
            from yang_rag.comparator.helper.pyang_utils import PyangStatementHelper

        for child in PyangStatementHelper.get_substmts(stmt):
            yield from self._iter_statements_recursive(child)

    def _condition_attachment_anchor_from_owner(self, owner_stmt) -> Optional[str]:
        """Return Rule 1/2/3 anchor context from a when/must owner statement."""
        if owner_stmt is None:
            return None

        try:
            from .pyang_utils import PyangStatementHelper
        except ImportError:
            from yang_rag.comparator.helper.pyang_utils import PyangStatementHelper

        wrapper_keywords = {'uses', 'choice', 'case'}
        data_keywords = {
            'container', 'list', 'leaf', 'leaf-list', 'anydata', 'anyxml',
            'rpc', 'action', 'notification', 'input', 'output'
        }

        owner_kw = PyangStatementHelper.get_keyword(owner_stmt)

        # Rule 1: when under augment uses augment target as context.
        if owner_kw == 'augment':
            aug_target = (PyangStatementHelper.get_argument(owner_stmt) or '').strip()
            if not aug_target:
                return None
            if aug_target.startswith('/'):
                return aug_target
            return '/' + aug_target.lstrip('/')

        # Rule 2: when under structural wrappers anchors at nearest parent data node.
        if owner_kw in wrapper_keywords:
            cur = getattr(owner_stmt, 'parent', None)
            while cur is not None:
                cur_kw = PyangStatementHelper.get_keyword(cur)
                if cur_kw in data_keywords:
                    p = PyangStatementHelper.build_path(cur, include_module=True)
                    return f"/{p}" if p else None
                cur = getattr(cur, 'parent', None)
            return None

        # Rule 3: when under data node anchors at that node.
        if owner_kw in data_keywords:
            p = PyangStatementHelper.build_path(owner_stmt, include_module=True)
            return f"/{p}" if p else None

        return None

    @staticmethod
    def _path_prefix_overlap_score(a: str, b: str) -> int:
        """Score common leading path segments between two absolute paths."""
        a_parts = [p for p in (a or '').split('/') if p]
        b_parts = [p for p in (b or '').split('/') if p]
        score = 0
        for x, y in zip(a_parts, b_parts):
            if x != y:
                break
            score += 1
        return score

    def _infer_condition_attachment_context(
        self,
        resolver,
        condition_raw: str,
        report_context: str,
        fallback_condition_raw: Optional[str] = None,
    ) -> str:
        """
        Infer effective context from actual when/must attachment statement.

        This applies YANG 7.21.5 anchoring rules and avoids relying solely on
        report paths that may point to expanded children under uses.
        """
        target = self._normalize_condition_text_for_match(condition_raw)
        if not target:
            return report_context

        module_stmt = getattr(resolver, 'module', None)
        if module_stmt is None:
            return report_context

        try:
            from .pyang_utils import PyangStatementHelper
        except ImportError:
            from yang_rag.comparator.helper.pyang_utils import PyangStatementHelper

        def _collect_anchors_by_condition_text(cond_text: str) -> List[str]:
            cond_target = self._normalize_condition_text_for_match(cond_text)
            if not cond_target:
                return []
            out: List[str] = []
            for stmt in self._iter_statements_recursive(module_stmt):
                kw = PyangStatementHelper.get_keyword(stmt)
                if kw not in {'when', 'must'}:
                    continue
                arg = self._normalize_condition_text_for_match(PyangStatementHelper.get_argument(stmt) or '')
                if arg != cond_target:
                    continue
                owner = getattr(stmt, 'parent', None)
                anchor = self._condition_attachment_anchor_from_owner(owner)
                if anchor:
                    out.append(anchor)
            return out

        anchors: List[str] = _collect_anchors_by_condition_text(condition_raw)

        # For OR/AND decomposition, subcondition text may not exist as a standalone
        # when/must statement in the AST. Fall back to the top-level condition text
        # from analyze_change so all branches inherit the same attachment anchor.
        if not anchors and fallback_condition_raw:
            anchors = _collect_anchors_by_condition_text(fallback_condition_raw)

        if not anchors:
            return report_context

        # Deduplicate while preserving order.
        uniq: List[str] = []
        seen = set()
        for a in anchors:
            if a in seen:
                continue
            seen.add(a)
            uniq.append(a)

        if not report_context:
            return uniq[0]

        # Prefer the anchor with the strongest shared prefix with report context.
        best = max(uniq, key=lambda a: self._path_prefix_overlap_score(a, report_context))
        if hasattr(resolver, '_canonicalize_context_path'):
            try:
                best = resolver._canonicalize_context_path(best)
            except Exception:
                pass

        if best != report_context:
            self._debug(
                "path_context attachment_anchor "
                f"condition={condition_raw!r} report_context={report_context!r} "
                f"anchored_context={best!r}"
            )
        return best

    def _classify_pyang_broken_transition(
        self,
        old_raw: str,
        new_raw: str,
    ) -> Optional[Tuple[ChangeDirection, str]]:
        """
        Use pyang's broken-XPath detection as an authoritative validity signal.

        This is used for changed equality paths to correctly handle YANG wrapper
        contexts (e.g. when attached to uses/choice/case/augment), where generic
        schema-path heuristics can be misleading.
        """
        if self._old_pyang_broken_lines is None or self._new_pyang_broken_lines is None:
            return None

        old_resolver, new_resolver = self._get_xpath_resolvers()

        old_broken = self._is_pyang_broken(
            old_raw,
            'old',
            report_context=self.context_path,
            resolver=old_resolver,
        )
        new_broken = self._is_pyang_broken(
            new_raw,
            'new',
            report_context=self.context_path,
            resolver=new_resolver,
        )

        if old_broken and not new_broken:
            return (
                ChangeDirection.RELAXED,
                "Pyang validation: old condition is broken (always-FALSE), new condition is valid "
                "→ RELAXED (BC).",
            )
        if not old_broken and new_broken:
            return (
                ChangeDirection.NARROWED,
                "Pyang validation: old condition is valid, new condition is broken (always-FALSE) "
                "→ NARROWED (NBC).",
            )
        if old_broken and new_broken:
            return (
                ChangeDirection.EQUIVALENT,
                "Pyang validation: both old and new conditions are broken (both always-FALSE) "
                "→ EQUIVALENT.",
            )

        return None

    def _is_decomposed_subcondition_analysis(self, old_raw: str, new_raw: str) -> bool:
        """
        Return True when we're analyzing a subcondition extracted from a larger
        OR/AND expression.

        Pyang broken-condition sets are currently keyed by condition text only,
        without attachment-context identity. Applying those signals to split
        subconditions can misclassify one OR/AND branch due to text collisions
        across different schema contexts.
        """
        top_old = (self._last_analysis_details.get('old_condition') or '').strip()
        top_new = (self._last_analysis_details.get('new_condition') or '').strip()

        if not top_old and not top_new:
            return False

        # If the top-level expression is compound but current raw differs from
        # the top-level text, this is a decomposed branch.
        top_compound = (
            (' or ' in top_old.lower()) or (' and ' in top_old.lower()) or
            (' or ' in top_new.lower()) or (' and ' in top_new.lower())
        )
        if not top_compound:
            return False

        return old_raw.strip() != top_old or new_raw.strip() != top_new

    def _resolve_and_compare_paths(
        self,
        old_path: str,
        new_path: str,
        old_condition_raw: Optional[str] = None,
        new_condition_raw: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Resolve XPath expressions and compare resolved paths.
        
        Args:
            old_path: Old XPath expression
            new_path: New XPath expression
            
        Returns:
            Tuple of (same_target: bool, explanation: str, resolution_status: Optional[str])
            resolution_status can be: 'both_valid', 'old_invalid', 'new_invalid',
            'both_invalid', 'edge_unresolved', None
        """
        if not self.enable_xpath_resolution or not self.context_path:
            # XPath resolution not enabled - fall back to string comparison
            self._debug(
                f"path_compare skipped resolution old_path={old_path!r} new_path={new_path!r} "
                "reason=xpath_resolution_disabled_or_missing_context"
            )
            return old_path == new_path, "XPath resolution not enabled", None
        
        old_resolver, new_resolver = self._get_xpath_resolvers()
        
        if old_resolver is None or new_resolver is None:
            self._debug(
                f"path_compare skipped resolution old_path={old_path!r} new_path={new_path!r} "
                "reason=xpath_resolver_unavailable"
            )
            return old_path == new_path, "XPath resolvers not available", None
        
        try:
            old_effective_context = self.context_path
            new_effective_context = self.context_path

            top_old_condition = self._last_analysis_details.get('old_condition')
            top_new_condition = self._last_analysis_details.get('new_condition')

            if old_condition_raw:
                old_effective_context = self._infer_condition_attachment_context(
                    old_resolver,
                    old_condition_raw,
                    self.context_path or '',
                    fallback_condition_raw=top_old_condition,
                )
            if new_condition_raw:
                new_effective_context = self._infer_condition_attachment_context(
                    new_resolver,
                    new_condition_raw,
                    self.context_path or '',
                    fallback_condition_raw=top_new_condition,
                )

            context_override_used = (
                (old_effective_context or '') != (self.context_path or '')
                or (new_effective_context or '') != (self.context_path or '')
            )

            # Resolve both paths
            old_resolved, old_context_used, old_fallback_used = self._resolve_xpath_with_parent_fallback(
                old_resolver, old_effective_context, old_path
            )
            new_resolved, new_context_used, new_fallback_used = self._resolve_xpath_with_parent_fallback(
                new_resolver, new_effective_context, new_path
            )

            self._debug(
                "path_compare resolved "
                f"old_path={old_path!r} old_success={old_resolved.success} old_abs={old_resolved.absolute_path!r} old_err={old_resolved.error!r} old_context={old_context_used!r} old_fallback={old_fallback_used} "
                f"new_path={new_path!r} new_success={new_resolved.success} new_abs={new_resolved.absolute_path!r} new_err={new_resolved.error!r} new_context={new_context_used!r} new_fallback={new_fallback_used}"
            )
            self._last_analysis_details['path_compare'] = {
                'old_path': old_path,
                'old_success': old_resolved.success,
                'old_abs': old_resolved.absolute_path,
                'old_error': old_resolved.error,
                'old_context_used': old_context_used,
                'old_fallback_used': old_fallback_used,
                'new_path': new_path,
                'new_success': new_resolved.success,
                'new_abs': new_resolved.absolute_path,
                'new_error': new_resolved.error,
                'new_context_used': new_context_used,
                'new_fallback_used': new_fallback_used,
            }

            def _is_unresolved_edge(path_expr: str, resolved_obj) -> bool:
                if resolved_obj is None or resolved_obj.success:
                    return False
                error_text = (resolved_obj.error or '').lower()
                path_text = (path_expr or '').strip()
                is_absolute = path_text.startswith('/')
                unresolved_markers = (
                    'expanded schema tree',
                    'not found under parent',
                    'module name is unavailable for schema traversal'
                )
                return is_absolute and any(marker in error_text for marker in unresolved_markers)
            
            old_valid = old_resolved.success
            new_valid = new_resolved.success

            # Only treat as 'edge_unresolved' when BOTH sides have unresolved edge
            # cases.  When only one side fails as an unresolved edge (e.g. an old
            # absolute path that no longer matches the new schema structure) and the
            # other side succeeds, fall through to the normal old_invalid/new_invalid
            # logic so the A–E matrix can classify it correctly (RELAXED / NARROWED).
            if _is_unresolved_edge(old_path, old_resolved) and _is_unresolved_edge(new_path, new_resolved):
                self._last_analysis_details['path_compare'].update({
                    'resolution_status': 'edge_unresolved',
                    'same_target': False,
                })
                return False, (
                    f"Unresolved XPath edge case detected: "
                    f"old='{old_path}' ({old_resolved.error}), "
                    f"new='{new_path}' ({new_resolved.error})."
                ), 'edge_unresolved'
            
            # Determine resolution status
            if not old_valid and not new_valid:
                # Both paths failed to resolve from the given context path.
                # This commonly happens when the context is a grouping-level path
                # (e.g. /module/my-grouping/container/leaf) and the relative XPath
                # navigates above the grouping root.
                # Try resolving from instantiated contexts (where the grouping is used).
                try:
                    old_inst_results = old_resolver.resolve_xpath_from_grouping_context(
                        self.context_path, old_path
                    )
                    new_inst_results = new_resolver.resolve_xpath_from_grouping_context(
                        self.context_path, new_path
                    )
                    if old_inst_results and new_inst_results:
                        # Compare resolved paths from instantiated contexts
                        old_paths = {r.absolute_path for r in old_inst_results if r.success}
                        new_paths = {r.absolute_path for r in new_inst_results if r.success}
                        if old_paths and new_paths:
                            if old_paths == new_paths:
                                return True, (
                                    f"Both resolve to same node via grouping instantiation: "
                                    f"{old_paths.pop()}"
                                ), 'both_valid'
                            else:
                                return False, (
                                    f"Different nodes via grouping instantiation: "
                                    f"old={old_paths}, new={new_paths}"
                                ), 'both_valid'
                except Exception:
                    pass

                resolution_status = 'both_invalid'
                explanation = (
                    f"Both paths invalid: "
                    f"'{old_path}' ({old_resolved.error}), "
                    f"'{new_path}' ({new_resolved.error})"
                )
                # Both invalid = both constraints not enforced = no behavior change
                return True, explanation, resolution_status
            elif not old_valid:
                # ── Grouping-context fallback for old relative path ────────────
                # When the old path is relative (e.g. "../../../../../../logical-channels/
                # channel/index") and the new path is absolute (e.g.
                # "/oc-opt-term:terminal-device/oc-opt-term:logical-channels/..."),
                # the old path may fail to resolve from the grouping-level context_path
                # because it navigates above the grouping root.  Try resolving it from
                # each instantiation point of the grouping and compare with the new
                # absolute path's resolved node.
                if not old_path.startswith('/') and new_valid:
                    # ── Forward-tail + up_count ≥ anchor_depth guard (relative→absolute) ──
                    # When the old path is relative (e.g. "../../../../../../logical-channels/
                    # channel/index") and the new path is absolute and resolves successfully,
                    # the old path may fail to resolve from the grouping-level context_path
                    # because it navigates above the grouping root.
                    #
                    # Algorithm:
                    #   1. Count "../" steps (up_count) and extract the forward tail.
                    #   2. Suffix check: the normalized new absolute path must end with
                    #      '/' + old_forward (the forward tail after all the "../").
                    #   3. Depth guard: up_count must be ≥ anchor_depth, where
                    #      anchor_depth = len(abs_segs) - len(fwd_segs).
                    #      Rationale: to reach an anchor that is anchor_depth levels deep
                    #      from the schema root, the relative path must navigate at least
                    #      anchor_depth levels up (to get above the anchor point) and then
                    #      back down via the forward tail.  If up_count < anchor_depth, the
                    #      relative path cannot possibly reach the anchor from any context.
                    #      Additionally require up_count > len(fwd_segs) so the path
                    #      navigates more levels up than it descends — a necessary condition
                    #      for the old path to have navigated above the grouping root.
                    #
                    # Example (case [172]):
                    #   old: "../../../../../../logical-channels/channel/index"
                    #     up_count=6, fwd_segs=['logical-channels','channel','index'] (3)
                    #   new_abs_norm: "/terminal-device/logical-channels/channel/index"
                    #     abs_segs=4, anchor_depth = 4 - 3 = 1
                    #   up_count=6 ≥ anchor_depth=1  ✓
                    #   up_count=6 > fwd_segs=3  ✓
                    #   suffix: ends with '/logical-channels/channel/index'  ✓
                    #   → EQUIVALENT
                    #
                    # Counter-example (false positive guard):
                    #   old: "../state/type"  (up_count=1, fwd_segs=2)
                    #   new_abs_norm: "/a/b/c/d/state/type"  (abs_segs=6, anchor_depth=4)
                    #   up_count=1 < anchor_depth=4  ✗  → not triggered
                    #
                    #   old: "../../state/type"  (up_count=2, fwd_segs=2)
                    #   new_abs_norm: "/a/b/state/type"  (abs_segs=4, anchor_depth=2)
                    #   up_count=2 ≥ anchor_depth=2  ✓  but up_count=2 > fwd_segs=2  ✗
                    #   → not triggered (ambiguous: could be same or different node)
                    old_forward = old_path
                    up_count = 0
                    while old_forward.startswith('../'):
                        old_forward = old_forward[3:]
                        up_count += 1
                    new_abs_norm = self._normalize_absolute_path(new_resolved.absolute_path)

                    if old_forward and new_abs_norm and up_count > 0:
                        fwd_segs = [s for s in old_forward.split('/') if s]
                        abs_segs = [s for s in new_abs_norm.split('/') if s]
                        anchor_depth = len(abs_segs) - len(fwd_segs)
                        suffix_matches = (
                            anchor_depth >= 0
                            and new_abs_norm.endswith('/' + old_forward)
                        )
                        depth_guard_ok = (
                            up_count >= anchor_depth
                            and up_count > len(fwd_segs)
                        )
                        if suffix_matches and depth_guard_ok:
                            self._debug(
                                "path_compare heuristic_equivalent "
                                f"old_path={old_path!r} new_path={new_path!r} "
                                f"old_forward={old_forward!r} new_abs_norm={new_abs_norm!r} "
                                f"up_count={up_count} anchor_depth={anchor_depth} "
                                f"fwd_segs={len(fwd_segs)}"
                            )
                            self._last_analysis_details['path_compare'].update({
                                'resolution_status': 'both_valid',
                                'same_target': True,
                            })
                            return True, (
                                f"Forward-tail match: new absolute '{new_abs_norm}' ends with "
                                f"'{old_forward}', up_count={up_count} ≥ anchor_depth={anchor_depth} "
                                f"and up_count > fwd_segs={len(fwd_segs)} "
                                f"— same node (relative→absolute refactoring in grouping context)"
                            ), 'both_valid'

                    # ── Grouping-context fallback ─────────────────────────────────
                    if not context_override_used:
                        try:
                            old_inst_results = old_resolver.resolve_xpath_from_grouping_context(
                                self.context_path, old_path
                            )
                            if old_inst_results:
                                old_inst_paths = {
                                    self._normalize_absolute_path(r.absolute_path)
                                    for r in old_inst_results if r.success and r.absolute_path
                                }
                                if old_inst_paths and new_abs_norm and new_abs_norm in old_inst_paths:
                                    self._debug(
                                        "path_compare grouping_fallback_equivalent "
                                        f"old_path={old_path!r} new_path={new_path!r} "
                                        f"old_inst={old_inst_paths} new_abs={new_abs_norm!r}"
                                    )
                                    self._last_analysis_details['path_compare'].update({
                                        'resolution_status': 'both_valid',
                                        'same_target': True,
                                    })
                                    return True, (
                                        f"Both resolve to same node via grouping instantiation: "
                                        f"old relative '{old_path}' → {old_inst_paths}, "
                                        f"new absolute '{new_path}' → {new_abs_norm}"
                                    ), 'both_valid'
                        except Exception:
                            pass

                resolution_status = 'old_invalid'
                explanation = (
                    f"Old path invalid ('{old_path}': {old_resolved.error}), "
                    f"new path valid → {new_resolved.absolute_path}. "
                    f"Constraint now enforces (was broken before)."
                )
                return False, explanation, resolution_status
            elif not new_valid:
                resolution_status = 'new_invalid'
                # Before declaring different targets, check if the old absolute path
                # and the new relative path share a common suffix — indicating they
                # likely refer to the same node (e.g. absolute path from another module
                # vs relative path from a grouping context that can't be resolved).
                old_abs_path = old_resolved.absolute_path
                new_raw_path = new_path.lstrip('.')
                # Strip leading slashes and '../' from new path to get the forward part
                new_forward = new_path
                while new_forward.startswith('../'):
                    new_forward = new_forward[3:]
                # IMPORTANT: apply this heuristic ONLY for explicit absolute→relative
                # refactors (old XPath starts with '/').
                #
                # If both old and new paths are relative and one side fails to resolve,
                # a shared suffix like 'state/type' is too weak and causes false
                # EQUIVALENT classifications (as seen in ospfv2 when-depth edits).
                # In those cases, keep status as 'new_invalid' so caller escalates.
                if old_path.startswith('/') and new_forward and old_abs_path.endswith('/' + new_forward):
                    self._debug(
                        "path_compare heuristic_equivalent "
                        f"old_path={old_path!r} new_path={new_path!r} "
                        f"old_abs={old_abs_path!r} new_forward={new_forward!r}"
                    )
                    return True, (
                        f"Path suffix match: old absolute '{old_abs_path}' ends with "
                        f"relative forward part '{new_forward}' — likely same node "
                        f"(absolute→relative refactoring in grouping context)"
                    ), 'both_valid'

                # ── Grouping-context fallback for new relative path ────────────
                # Symmetric to the old_invalid case: when the new path is relative
                # and fails to resolve from the grouping-level context_path, try
                # resolving it from each instantiation point and compare with the
                # old absolute path's resolved node.
                if (not context_override_used) and (not new_path.startswith('/')) and old_valid:
                    try:
                        new_inst_results = new_resolver.resolve_xpath_from_grouping_context(
                            self.context_path, new_path
                        )
                        if new_inst_results:
                            new_inst_paths = {
                                self._normalize_absolute_path(r.absolute_path)
                                for r in new_inst_results if r.success and r.absolute_path
                            }
                            old_abs_norm = self._normalize_absolute_path(old_resolved.absolute_path)
                            if new_inst_paths and old_abs_norm and old_abs_norm in new_inst_paths:
                                self._debug(
                                    "path_compare grouping_fallback_equivalent "
                                    f"old_path={old_path!r} new_path={new_path!r} "
                                    f"new_inst={new_inst_paths} old_abs={old_abs_norm!r}"
                                )
                                self._last_analysis_details['path_compare'].update({
                                    'resolution_status': 'both_valid',
                                    'same_target': True,
                                })
                                return True, (
                                    f"Both resolve to same node via grouping instantiation: "
                                    f"old absolute '{old_path}' → {old_abs_norm}, "
                                    f"new relative '{new_path}' → {new_inst_paths}"
                                ), 'both_valid'
                    except Exception:
                        pass

                explanation = (
                    f"New path invalid ('{new_path}': {new_resolved.error}), "
                    f"old path was valid: {old_resolved.absolute_path}. "
                    f"Constraint now broken (more permissive)."
                )
                return False, explanation, resolution_status
            
            # Both valid - compare resolved paths
            resolution_status = 'both_valid'
            old_abs = old_resolved.absolute_path
            new_abs = new_resolved.absolute_path
            same_target = old_abs == new_abs

            if not same_target:
                # Normalize: strip module-name prefix from first path component.
                # Absolute XPaths in YANG often omit the module name (e.g.
                # "/mpls/lsps/..." vs "/openconfig-mpls/mpls/lsps/...").
                # Both refer to the same node — only the module-name prefix differs.
                old_norm = self._normalize_absolute_path(old_abs)
                new_norm = self._normalize_absolute_path(new_abs)
                if old_norm == new_norm and old_norm:
                    same_target = True
                    explanation = (
                        f"Both resolve to same node (after module-name normalization): "
                        f"'{old_path}' → {old_abs}, '{new_path}' → {new_abs}"
                    )
                    return same_target, explanation, resolution_status

                # If both paths resolve to different strings but neither target_node
                # exists in the schema, the context is a grouping-level path that
                # doesn't correspond to any real instantiated node.  The path
                # arithmetic produces different strings but we cannot confirm which
                # (if either) is correct from the schema blueprint alone.
                # Downgrade to 'both_invalid' so the caller routes to
                # NEEDS_LLM_ANALYSIS rather than a false NARROWED verdict.
                if old_resolved.target_node is None and new_resolved.target_node is None:
                    resolution_status = 'both_invalid'
                    explanation = (
                        f"Both paths resolve to different strings but neither target node "
                        f"exists in the schema (grouping-context ambiguity): "
                        f"'{old_path}' → {old_abs}, '{new_path}' → {new_abs}"
                    )
                    return True, explanation, resolution_status

            if same_target:
                explanation = f"Both resolve to same node: {old_abs}"
            else:
                explanation = (
                    f"Different nodes: "
                    f"'{old_path}' → {old_abs}, "
                    f"'{new_path}' → {new_abs}"
                )
            
            return same_target, explanation, resolution_status
            
        except Exception as e:
            # Resolution error - fall back to string comparison
            return old_path == new_path, f"XPath resolution error: {str(e)}", None
    
    def _normalize_absolute_path(self, path: str) -> str:
        """
        Normalize an absolute schema path by stripping the module-name prefix
        from the first path component, but ONLY if the first component is a
        known YANG module name (from old_yang_file or new_yang_file).

        In YANG, absolute XPaths may or may not include the module name as the
        first component. For example:
          /openconfig-mpls/mpls/lsps/constrained-path/tunnel/config/signaling-protocol
          /mpls/lsps/constrained-path/tunnel/config/signaling-protocol

        Both refer to the same node. This method strips the first component only
        if it matches a known module name, to avoid incorrectly stripping
        container names that happen to contain hyphens.

        Returns the normalized path, or the original path if no stripping applies.
        """
        if not path or not path.startswith('/'):
            return path
        parts = [p for p in path.split('/') if p]
        if len(parts) < 2:
            return path

        # Build set of known module names from YANG file names
        known_module_names: set = set()
        from pathlib import Path
        for yang_file in [self.old_yang_file, self.new_yang_file]:
            if yang_file:
                stem = Path(yang_file).stem  # e.g. 'openconfig-mpls'
                known_module_names.add(stem)

        # Strip first component only if it's a known module name
        if parts[0] in known_module_names:
            parts = parts[1:]

        # Strip namespace prefixes from all remaining segments.
        # e.g. 'ocif:interfaces' → 'interfaces', 'oc-if:interface' → 'interface'
        # This ensures that prefix renames (ocif → oc-if) do not cause false
        # NBC classifications when the underlying schema node is the same.
        def _strip_ns(seg: str) -> str:
            return seg.split(':', 1)[1] if ':' in seg else seg

        return '/' + '/'.join(_strip_ns(p) for p in parts)

    def _conditions_equivalent(self, c1: ParsedCondition, c2: ParsedCondition) -> bool:
        """Check if two conditions are structurally equivalent."""
        if c1.type != c2.type:
            return False
        if c1.path != c2.path:
            return False
        if c1.operator != c2.operator:
            return False
        if c1.value != c2.value:
            return False
        return True

    def _collect_identity_enum_bit_names(self, yang_file: str) -> Optional[Dict[str, Set[str]]]:
        """Collect identity/enum/bit symbol names from a YANG module."""
        from pathlib import Path as _Path
        abs_yang = str(_Path(yang_file).resolve())

        if abs_yang in _identity_enum_bit_cache:
            return _identity_enum_bit_cache[abs_yang]

        try:
            try:
                from yang_rag.comparator.helper.pyang_utils import PyangContext
            except ImportError:
                from .pyang_utils import PyangContext

            search_dirs = self._prioritized_search_dirs(yang_file)
            ctx = PyangContext(search_dirs=search_dirs)
            mod = ctx.load_module(yang_file)
            if mod is None:
                _identity_enum_bit_cache[abs_yang] = None
                return None

            by_kind: Dict[str, Set[str]] = {
                'identity': set(),
                'enum': set(),
                'bit': set(),
            }

            def _collect(stmt):
                if stmt.keyword == 'identity':
                    by_kind['identity'].add(stmt.arg)
                elif stmt.keyword == 'enum':
                    by_kind['enum'].add(stmt.arg)
                elif stmt.keyword == 'bit':
                    by_kind['bit'].add(stmt.arg)
                for sub in stmt.substmts:
                    _collect(sub)

            _collect(mod)
            _identity_enum_bit_cache[abs_yang] = by_kind
            return by_kind
        except Exception:
            _identity_enum_bit_cache[abs_yang] = None
            return None

    def _collect_identity_enum_names(self, yang_file: str) -> Optional[set]:
        """
        Collect all identity and enum value names defined in a YANG module (and its imports).

        Uses pyang to load the module and extract:
          - identity statement args (e.g. 'ROUTER_INFORMATION_LSA')
          - enum value names inside typedef/leaf type enumerations

        Returns a set of local name strings, or None if loading fails.

        Results are cached in the process-level ``_identity_enum_cache`` so that
        the same YANG file is parsed at most once per process.
        """
        from pathlib import Path as _Path
        abs_yang = str(_Path(yang_file).resolve())

        if abs_yang in _identity_enum_cache:
            return _identity_enum_cache[abs_yang]

        try:
            names_by_kind = self._collect_identity_enum_bit_names(yang_file)
            if names_by_kind is None:
                _identity_enum_cache[abs_yang] = None
                return None

            names = set(names_by_kind.get('identity', set()))
            names.update(names_by_kind.get('enum', set()))
            names.update(names_by_kind.get('bit', set()))
            _identity_enum_cache[abs_yang] = names
            return names
        except Exception:
            _identity_enum_cache[abs_yang] = None
            return None

    def _check_value_is_rename(
        self,
        old_val_clean: str,
        new_val_clean: str,
    ) -> Tuple[Optional[bool], str]:
        """
        Determine if a when/must equality value change is an identity/enum rename.

        Strategy:
          1. Parse prefix:localname from old and new values.
          2. Use prefix maps to find the defining module file for each.
          3. Load the OLD module and collect all identity/enum names.
          4. Load the NEW module and collect all identity/enum names.
          5. Decision:
             - old_local NOT in new_names AND new_local IN new_names
               → old value was removed and new value added → RENAME → True
             - old_local IN new_names
               → old value still exists in new module → genuinely different values → False
             - Cannot determine (no YANG files, pyang error, no prefix)
               → None

        Returns:
            (True, explanation)  → rename detected → EQUIVALENT
            (False, explanation) → not a rename → NBC
            (None, explanation)  → cannot determine → NEEDS_LLM_ANALYSIS
        """
        if not self.old_yang_file or not self.new_yang_file:
            return None, "No YANG files available for rename check"

        # Parse prefix:localname from values
        def _parse_prefixed(val: str):
            if ':' in val:
                parts = val.split(':', 1)
                return parts[0], parts[1]
            return None, val  # no prefix → local name only

        old_prefix, old_local = _parse_prefixed(old_val_clean)
        new_prefix, new_local = _parse_prefixed(new_val_clean)

        # Resolve prefix to module file using prefix maps
        old_prefix_map, new_prefix_map = self._get_prefix_maps()

        def _find_module_file(prefix, prefix_map, yang_file):
            """Find the YANG file for the module referenced by prefix."""
            if prefix is None:
                # No prefix → value is in the same module
                return yang_file
            module_name = prefix_map.get(prefix)
            if not module_name:
                return None
            import os
            search_dirs = self._prioritized_search_dirs(yang_file)
            for d in search_dirs:
                candidate = os.path.join(d, f'{module_name}.yang')
                if os.path.exists(candidate):
                    return candidate
            return None

        old_module_file = _find_module_file(old_prefix, old_prefix_map, self.old_yang_file)
        new_module_file = _find_module_file(new_prefix, new_prefix_map, self.new_yang_file)

        if not old_module_file or not new_module_file:
            return None, (
                f"Could not locate module file for prefix "
                f"'{old_prefix}' (old) or '{new_prefix}' (new)"
            )

        # Collect identity/enum names from old and new modules
        old_names = self._collect_identity_enum_names(old_module_file)
        new_names = self._collect_identity_enum_names(new_module_file)

        if old_names is None or new_names is None:
            return None, "Could not load module for identity/enum name collection"

        old_in_old = old_local in old_names
        old_in_new = old_local in new_names
        new_in_new = new_local in new_names

        if not old_in_old:
            # Old value not found in the module pointed to by the old prefix.
            # Treat as broken (always-FALSE): the identity/enum doesn't exist in the
            # referenced module, so the condition never matches.
            # Return None to signal "cannot determine rename" — the caller will then
            # apply the validity framework (old broken → check if new is valid → BC/NBC).
            return None, (
                f"Old value '{old_local}' not found in old module '{old_module_file}' — "
                f"old condition is broken (always-FALSE)"
            )

        if not old_in_new and new_in_new:
            # Old value gone from new module, new value present → rename
            return True, (
                f"Old identity/enum '{old_local}' absent from new module, "
                f"new identity/enum '{new_local}' present → rename detected"
            )
        elif old_in_new:
            # Old value still exists in new module → genuinely different values
            return False, (
                f"Old identity/enum '{old_local}' still present in new module → "
                f"genuinely different constraint values (not a rename)"
            )
        else:
            # Neither old nor new found in new module — unusual
            return None, (
                f"Neither '{old_local}' nor '{new_local}' found in new module — "
                f"cannot determine rename"
            )

    def _analyze_presence(self, old: ParsedCondition, new: ParsedCondition) -> Tuple[ChangeDirection, str]:
        """Analyze change in presence checks."""
        if old.path == new.path:
            return ChangeDirection.EQUIVALENT, f"Same presence check: {old.path}"

        # Different paths — attempt resolution before escalating to LLM.
        #
        # Step 1: prefix-map normalisation (e.g. 'oc-opt-term:index' vs relative
        #         '../../index' — strip namespace prefixes to canonical module:local form
        #         so that a pure prefix-rename is detected as EQUIVALENT).
        if self.old_yang_file and self.new_yang_file:
            old_prefix_map, new_prefix_map = self._get_prefix_maps()
            if old_prefix_map or new_prefix_map:
                old_canonical = self._resolve_path_prefix_to_canonical(old.path, old_prefix_map)
                new_canonical = self._resolve_path_prefix_to_canonical(new.path, new_prefix_map)
                if old_canonical == new_canonical:
                    return ChangeDirection.EQUIVALENT, (
                        f"Presence check path prefix renamed: '{old.path}' → '{new.path}' "
                        f"(both resolve to '{old_canonical}') — semantically equivalent"
                    )

        # Step 2: XPath resolution — resolve both relative/absolute paths from the
        #         context node and compare the resulting absolute schema paths.
        same_target, explanation, resolution_status = self._resolve_and_compare_paths(
            old.path, new.path, old_condition_raw=old.raw, new_condition_raw=new.raw
        )

        # Apply the A–E validity matrix for presence-check paths:
        #   D. old invalid, new valid  → RELAXED  (BC — was broken, now enforces)
        #   C. old valid, new invalid  → NARROWED (NBC — was valid, now broken)
        #   E. both invalid            → EQUIVALENT (both always-FALSE)
        #   inconclusive / edge cases  → escalate to LLM
        if resolution_status == 'old_invalid':
            return ChangeDirection.RELAXED, (
                f"Presence check path changed: '{old.path}' → '{new.path}' — "
                f"old path was broken, new path is valid (constraint now enforces): {explanation}"
            )

        if resolution_status == 'new_invalid':
            return ChangeDirection.NARROWED, (
                f"Presence check path changed: '{old.path}' → '{new.path}' — "
                f"old path was valid, new path is broken (constraint now broken): {explanation}"
            )

        if resolution_status in ('both_invalid', 'edge_unresolved', None):
            # Resolution was not conclusive — escalate to LLM.
            return ChangeDirection.NEEDS_LLM_ANALYSIS, (
                f"Presence check path changed: '{old.path}' → '{new.path}' — "
                f"XPath resolution inconclusive ({resolution_status}): {explanation}"
            )

        if same_target:
            return ChangeDirection.EQUIVALENT, (
                f"Presence check path changed but resolves to same schema node "
                f"(relative↔absolute rewrite): {explanation}"
            )

        # Paths resolve to genuinely different nodes — this is a semantic change.
        # A presence check on a different node is INCOMPARABLE (we cannot tell
        # whether the new condition is stricter or looser without domain knowledge).
        return ChangeDirection.INCOMPARABLE, (
            f"Presence check path changed to a different schema node: "
            f"'{old.path}' → '{new.path}'. {explanation}"
        )
    
    def _resolve_path_prefix_to_canonical(self, path: str, prefix_map: Dict[str, str]) -> str:
        """
        Resolve namespace prefix on a path component to its canonical module name.

        e.g. 'ocif:type' with prefix_map {'ocif': 'openconfig-interfaces'}
             → 'openconfig-interfaces:type'

        If the path has no prefix or the prefix is not in the map, returns the path unchanged.
        """
        import re
        # Match prefix:localname pattern at the start or after /
        def replace_prefix(m):
            prefix = m.group(1)
            local = m.group(2)
            if prefix in prefix_map:
                return f'{prefix_map[prefix]}:{local}'
            return m.group(0)
        return re.sub(r'\b([a-zA-Z][a-zA-Z0-9_-]*):([a-zA-Z][a-zA-Z0-9_-]*)', replace_prefix, path)

    def _analyze_equality(self, old: ParsedCondition, new: ParsedCondition) -> Tuple[ChangeDirection, str]:
        """Analyze change in equality checks."""
        # First check if paths are different and try XPath resolution
        if old.path != new.path:
            if not self._is_decomposed_subcondition_analysis(old.raw, new.raw):
                pyang_transition = self._classify_pyang_broken_transition(old.raw, new.raw)
                if pyang_transition is not None:
                    return pyang_transition

            # Before XPath resolution, check if the path difference is only a namespace
            # prefix rename on path components (e.g. 'ocif:type' → 'oc-if:type').
            # Use the pyang prefix maps to resolve both to canonical module:localname form.
            if self.old_yang_file and self.new_yang_file:
                old_prefix_map, new_prefix_map = self._get_prefix_maps()
                if old_prefix_map or new_prefix_map:
                    old_canonical_path = self._resolve_path_prefix_to_canonical(old.path, old_prefix_map)
                    new_canonical_path = self._resolve_path_prefix_to_canonical(new.path, new_prefix_map)
                    if old_canonical_path == new_canonical_path:
                        # Paths resolve to the same canonical node — prefix rename only
                        # Now check the value
                        if old.value == new.value:
                            return ChangeDirection.EQUIVALENT, (
                                f"Path prefix renamed: '{old.path}' → '{new.path}' "
                                f"(both resolve to '{old_canonical_path}'), same value"
                            )
                        # Same path, different value — fall through to value comparison below
                        # by temporarily treating paths as equal
                        old = ParsedCondition(
                            type=old.type, path=new.path, operator=old.operator,
                            value=old.value, raw=old.raw, subconditions=old.subconditions
                        )

            # Try XPath resolution to see if they point to the same node
            same_target, explanation, resolution_status = self._resolve_and_compare_paths(
                old.path, new.path, old_condition_raw=old.raw, new_condition_raw=new.raw
            )
            
            # Handle invalid path cases deterministically
            if resolution_status == 'both_invalid':
                # Both paths failed to resolve (e.g. context path is a grouping definition
                # that doesn't exist in the instantiated schema tree).  We cannot confirm
                # equivalence, so treat conservatively: flag for deeper analysis rather than
                # silently marking as INCOMPARABLE (which falls through to untagged → BC).
                return ChangeDirection.NEEDS_LLM_ANALYSIS, (
                    f"XPath path component changed ('{old.path}' → '{new.path}') "
                    f"but resolution unavailable — requires deeper analysis: {explanation}"
                )
            elif resolution_status == 'edge_unresolved':
                return ChangeDirection.NEEDS_LLM_ANALYSIS, (
                    f"XPath path component changed ('{old.path}' → '{new.path}') "
                    f"and hit resolver edge case — requires LLM assistance: {explanation}"
                )
            elif resolution_status == 'old_invalid':
                # Old path invalid, new path valid -> bug fix / less restrictive.
                # With resolver/data-context fallback now in place, this transition is
                # authoritative enough to classify directly.
                return ChangeDirection.RELAXED, (
                    f"XPath path component changed ('{old.path}' → '{new.path}') — "
                    f"old path invalid, new path valid: {explanation} "
                    f"→ RELAXED (BC)."
                )
            elif resolution_status == 'new_invalid':
                # New path invalid, old path valid -> constraint became broken.
                return ChangeDirection.NARROWED, (
                    f"XPath path component changed ('{old.path}' → '{new.path}') — "
                    f"new path invalid, old path valid: {explanation} "
                    f"→ NARROWED (NBC)."
                )
            elif resolution_status is None:
                # Resolver was not available (no YANG files / context path missing).
                # Paths are textually different — we cannot determine if they are
                # semantically equivalent without resolution.  Use conservative path:
                # flag for deeper analysis rather than silently treating as INCOMPARABLE
                # (which falls through to untagged → BC in the downstream pipeline).
                return ChangeDirection.NEEDS_LLM_ANALYSIS, (
                    f"XPath path component changed ('{old.path}' → '{new.path}') "
                    f"with same value '{old.value}' — resolution unavailable, "
                    f"requires deeper analysis: {explanation}"
                )
            
            if same_target:
                # Paths resolve to same node - check value
                if old.value == new.value:
                    return ChangeDirection.EQUIVALENT, f"Same node, same value (XPath resolved): {explanation}"
                else:
                    # Same node, different value - analyze value change
                    pass  # Fall through to value comparison below
            else:
                # Paths resolve to DIFFERENT nodes from the given context.
                # This could be:
                #   A) A genuine semantic change (different nodes → NBC)
                #   B) A grouping context depth mismatch (the context path is a grouping
                #      path, not an instantiated path, so relative XPaths resolve
                #      incorrectly from the grouping root)
                #
                # Try resolving from instantiated contexts to confirm.
                try:
                    old_resolver, new_resolver = self._get_xpath_resolvers()
                    if old_resolver and new_resolver:
                        old_inst = old_resolver.resolve_xpath_from_grouping_context(
                            self.context_path, old.path
                        )
                        new_inst = new_resolver.resolve_xpath_from_grouping_context(
                            self.context_path, new.path
                        )
                        if old_inst and new_inst:
                            old_inst_paths = {r.absolute_path for r in old_inst if r.success}
                            new_inst_paths = {r.absolute_path for r in new_inst if r.success}
                            if old_inst_paths and new_inst_paths:
                                if old_inst_paths == new_inst_paths:
                                    # Instantiated contexts agree: same node → EQUIVALENT
                                    # (grouping context depth mismatch was the issue)
                                    if old.value == new.value:
                                        return ChangeDirection.EQUIVALENT, (
                                            f"Same node via grouping instantiation "
                                            f"(context depth mismatch resolved): "
                                            f"{old_inst_paths.pop()}"
                                        )
                                    # Same node, different value → fall through to value check
                                else:
                                    # Instantiated contexts confirm: different nodes → NBC
                                    return ChangeDirection.NARROWED, (
                                        f"Different target nodes confirmed via grouping "
                                        f"instantiation: old={old_inst_paths}, "
                                        f"new={new_inst_paths}. "
                                        f"Semantic change = NARROWED (NBC)."
                                    )
                except Exception:
                    pass
                # Could not confirm via instantiation — conservative NBC
                return ChangeDirection.NARROWED, (
                    f"Different target nodes: {explanation}. "
                    f"Semantic change = NARROWED (NBC)."
                )
        
        # Same path (or resolved to same node), check value
        if old.value != new.value:
            # Check if this is just a prefix addition (e.g., 'FOO' -> 'prefix:FOO')
            # This is a common pattern when adding module prefixes to identity/enum refs
            old_val_clean = old.value.strip('\'"')
            new_val_clean = new.value.strip('\'"')

            def _classify_prefix_variant(change_label: str) -> Tuple[ChangeDirection, str]:
                """Classify prefix-only value rewrites using RHS validity, not text alone."""
                old_pm, new_pm = self._get_prefix_maps()
                old_resolver, new_resolver = self._get_xpath_resolvers()
                old_rhs_valid, old_rhs_reason = self._validate_xpath_rhs(
                    f"'{old.value}'",
                    self.old_yang_file,
                    old_pm,
                    old_resolver,
                    lhs_path=old.path,
                    context_path=self.context_path,
                )
                new_rhs_valid, new_rhs_reason = self._validate_xpath_rhs(
                    f"'{new.value}'",
                    self.new_yang_file,
                    new_pm,
                    new_resolver,
                    lhs_path=new.path,
                    context_path=self.context_path,
                )

                # Important for enum/bit handling: prefixed enum literals are often
                # invalid, while unprefixed labels are valid. That is RELAXED, not
                # EQUIVALENT.
                if old_rhs_valid is False and new_rhs_valid is True:
                    return ChangeDirection.RELAXED, (
                        f"{change_label}: old value was broken ({old_rhs_reason}), "
                        f"new value is valid ({new_rhs_reason}). "
                        f"→ RELAXED (BC)."
                    )
                if old_rhs_valid is True and new_rhs_valid is False:
                    return ChangeDirection.NARROWED, (
                        f"{change_label}: old value was valid ({old_rhs_reason}), "
                        f"new value is broken ({new_rhs_reason}). "
                        f"→ NARROWED (NBC)."
                    )
                if old_rhs_valid is False and new_rhs_valid is False:
                    return ChangeDirection.EQUIVALENT, (
                        f"{change_label}: both values broken (always-FALSE): "
                        f"old ({old_rhs_reason}), new ({new_rhs_reason}). → EQUIVALENT."
                    )
                if old_rhs_valid is True and new_rhs_valid is True:
                    return ChangeDirection.EQUIVALENT, (
                        f"{change_label}: both values valid and resolve to the same local token "
                        f"('{new_val_clean}') → EQUIVALENT."
                    )

                # Uncertain validity on one or both sides.
                return ChangeDirection.NEEDS_LLM_ANALYSIS, (
                    f"{change_label}: could not determine RHS validity conclusively "
                    f"(old={old_rhs_valid}, new={new_rhs_valid})."
                )
            
            # Check if new value ends with old value after a colon (prefix addition)
            if ':' in new_val_clean and new_val_clean.split(':', 1)[1] == old_val_clean:
                return _classify_prefix_variant(
                    f"Prefix added to value: '{old.value}' → '{new.value}'"
                )
            
            # Check if old value has a prefix and new doesn't (prefix removal)
            if ':' in old_val_clean and old_val_clean.split(':', 1)[1] == new_val_clean:
                return _classify_prefix_variant(
                    f"Prefix removed from value: '{old.value}' → '{new.value}'"
                )
            
            # Check if both have prefixes but only prefix changed (module rename)
            if ':' in old_val_clean and ':' in new_val_clean:
                old_prefix, old_local = old_val_clean.split(':', 1)
                new_prefix, new_local = new_val_clean.split(':', 1)
                if old_local == new_local and old_prefix != new_prefix:
                    return _classify_prefix_variant(
                        f"Prefix changed: '{old_prefix}' → '{new_prefix}' (local name '{old_local}' unchanged)"
                    )

            # Check if the value change is actually an identity/enum rename in the
            # referenced module. Use pyang to inspect the old and new module definitions:
            #   - If old local name is ABSENT from new module AND new local name is PRESENT
            #     → the identity/enum was renamed → EQUIVALENT
            #   - If old local name is STILL PRESENT in new module
            #     → genuinely different values → NBC (NARROWED)
            is_rename, rename_explanation = self._check_value_is_rename(
                old_val_clean, new_val_clean
            )
            if is_rename is True:
                return ChangeDirection.EQUIVALENT, (
                    f"Identity/enum value renamed: '{old.value}' → '{new.value}'. "
                    f"{rename_explanation}"
                )
            elif is_rename is False:
                # Old value still exists in new module — genuinely different values.
                # New rejects data where path = old_value → NBC.
                return ChangeDirection.NARROWED, (
                    f"Equality value changed: '{old.value}' → '{new.value}'. "
                    f"Old value still exists in new module — different constraint target. "
                    f"Data matching old value now rejected = NARROWED (NBC). "
                    f"{rename_explanation}"
                )
            # is_rename is None → could not determine (no YANG files / pyang error,
            # or old value not found in its referenced module).
            # Apply the validity framework: check if old value is broken (always-FALSE)
            # and new value is valid. If old broken and new valid → RELAXED (BC).
            # If old valid and new broken → NARROWED (NBC). Both broken → EQUIVALENT.
            # This handles cases like prefix changes where the old prefix was wrong
            # (e.g. using the module's own prefix to reference an imported identity).
            # NOTE: old.value and new.value are already stripped of quotes by the parser.
            # Re-add quotes so _validate_xpath_rhs correctly identifies them as quoted
            # string literals (not unquoted location paths).
            old_pm, new_pm = self._get_prefix_maps()
            old_resolver, new_resolver = self._get_xpath_resolvers()
            old_rhs_valid, old_rhs_reason = self._validate_xpath_rhs(
                f"'{old.value}'",
                self.old_yang_file,
                old_pm,
                old_resolver,
                lhs_path=old.path,
                context_path=self.context_path,
            )
            new_rhs_valid, new_rhs_reason = self._validate_xpath_rhs(
                f"'{new.value}'",
                self.new_yang_file,
                new_pm,
                new_resolver,
                lhs_path=new.path,
                context_path=self.context_path,
            )

            if old_rhs_valid is False and new_rhs_valid is True:
                return ChangeDirection.RELAXED, (
                    f"Value fixed: old '{old.value}' was broken ({old_rhs_reason}), "
                    f"new '{new.value}' is valid ({new_rhs_reason}). "
                    f"→ RELAXED (BC): new version correctly enforces the constraint."
                )
            elif old_rhs_valid is True and new_rhs_valid is False:
                return ChangeDirection.NARROWED, (
                    f"Value broken: old '{old.value}' was valid ({old_rhs_reason}), "
                    f"new '{new.value}' is broken ({new_rhs_reason}). "
                    f"→ NARROWED (NBC): new version rejects data that old accepted."
                )
            elif old_rhs_valid is False and new_rhs_valid is False:
                return ChangeDirection.EQUIVALENT, (
                    f"Both values broken (always-FALSE): "
                    f"old '{old.value}' ({old_rhs_reason}), "
                    f"new '{new.value}' ({new_rhs_reason}). → EQUIVALENT."
                )

            # Both valid (or both uncertain) — apply additional deterministic checks:
            # If both are valid but different values, the new condition rejects data
            # that matched the old value → NARROWED (NBC), unless we can confirm
            # it's a rename or relaxation (e.g. OR clause expansion).
            # Since we already checked for rename above (is_rename is None = uncertain),
            # conservatively classify as NARROWED when both are valid.
            if old_rhs_valid is True and new_rhs_valid is True:
                return ChangeDirection.NARROWED, (
                    f"Both values valid but different: '{old.value}' → '{new.value}'. "
                    f"New condition targets a different identity/enum — data matching "
                    f"old value is now rejected → NARROWED (NBC)."
                )
            # Cannot determine validity of either side — escalate to LLM
            return ChangeDirection.NEEDS_LLM_ANALYSIS, f"Different values for same path: '{old.value}' → '{new.value}'"
        
        return ChangeDirection.EQUIVALENT, f"Same equality check: {old.path} = {old.value}"
    
    def _analyze_inequality(self, old: ParsedCondition, new: ParsedCondition) -> Tuple[ChangeDirection, str]:
        """Analyze change in inequality checks."""
        if old.path == new.path:
            # Same path, check value changes
            old_vals = self._parse_value_list(old.value)
            new_vals = self._parse_value_list(new.value)
            
            if old_vals == new_vals:
                return ChangeDirection.EQUIVALENT, f"Same inequality: {old.path} != {old.value}"
            
            # Check if the difference is only a namespace prefix change on the excluded value
            # e.g. != 'DEFAULT_INSTANCE' → != 'oc-ni-types:DEFAULT_INSTANCE'
            # This is equivalent: the same identity is being excluded, just with explicit prefix.
            old_val_clean = old.value.strip('\'"')
            new_val_clean = new.value.strip('\'"')
            if ':' in new_val_clean and new_val_clean.split(':', 1)[1] == old_val_clean:
                return ChangeDirection.EQUIVALENT, (
                    f"Prefix added to excluded value: '{old.value}' → '{new.value}' (semantic equivalent)"
                )
            if ':' in old_val_clean and old_val_clean.split(':', 1)[1] == new_val_clean:
                return ChangeDirection.EQUIVALENT, (
                    f"Prefix removed from excluded value: '{old.value}' → '{new.value}' (semantic equivalent)"
                )
            if ':' in old_val_clean and ':' in new_val_clean:
                old_prefix, old_local = old_val_clean.split(':', 1)
                new_prefix, new_local = new_val_clean.split(':', 1)
                if old_local == new_local and old_prefix != new_prefix:
                    return ChangeDirection.EQUIVALENT, (
                        f"Prefix changed on excluded value: '{old_prefix}' → '{new_prefix}' "
                        f"(local name '{old_local}' unchanged)"
                    )

            # More values excluded = more restrictive
            if len(new_vals) > len(old_vals):
                return ChangeDirection.NARROWED, f"More values excluded: {len(old_vals)} → {len(new_vals)}"
            elif len(new_vals) < len(old_vals):
                return ChangeDirection.RELAXED, f"Fewer values excluded: {len(old_vals)} → {len(new_vals)}"
            
            # Same number of exclusions but different values — requires analysis
            return ChangeDirection.NEEDS_LLM_ANALYSIS, (
                f"Different excluded values with same count: '{old.value}' → '{new.value}'"
            )
        
        # Path changed — conservative: flag for deeper analysis
        return ChangeDirection.NEEDS_LLM_ANALYSIS, (
            f"Inequality path changed: '{old.path}' → '{new.path}' — requires deeper analysis"
        )
    
    def _analyze_comparison(self, old: ParsedCondition, new: ParsedCondition) -> Tuple[ChangeDirection, str]:
        """Analyze change in comparison operators."""
        if old.path != new.path:
            return ChangeDirection.INCOMPARABLE, f"Different comparison paths: '{old.path}' → '{new.path}'"
        
        # Try to parse as numbers
        try:
            old_num = float(old.value)
            new_num = float(new.value)
            
            # Analyze based on operator
            if old.operator == new.operator:
                if old.operator in ('>', '>='):
                    # Threshold decreased = less restrictive
                    if new_num < old_num:
                        return ChangeDirection.RELAXED, f"Lower threshold: {old.operator} {old_num} → {new_num}"
                    elif new_num > old_num:
                        return ChangeDirection.NARROWED, f"Higher threshold: {old.operator} {old_num} → {new_num}"
                elif old.operator in ('<', '<='):
                    # Threshold increased = less restrictive
                    if new_num > old_num:
                        return ChangeDirection.RELAXED, f"Higher threshold: {old.operator} {old_num} → {new_num}"
                    elif new_num < old_num:
                        return ChangeDirection.NARROWED, f"Lower threshold: {old.operator} {old_num} → {new_num}"
                
                return ChangeDirection.EQUIVALENT, f"Same comparison: {old.path} {old.operator} {old.value}"
            else:
                # Operator changed — determine direction deterministically.
                # Represent each operator as a half-open interval on the number line
                # and compare which is a subset of the other.
                #
                # For a threshold T and value V:
                #   >  T  → V ∈ (T, ∞)
                #   >= T  → V ∈ [T, ∞)
                #   <  T  → V ∈ (-∞, T)
                #   <= T  → V ∈ (-∞, T]
                #
                # Subset relationships (same threshold, operator change only):
                #   >=  →  >  : NARROWED  (strict excludes T, inclusive includes T)
                #   >   →  >= : RELAXED
                #   <=  →  <  : NARROWED
                #   <   →  <= : RELAXED
                #   >   →  <  : NEEDS_LLM_ANALYSIS (direction inversion)
                #   >=  →  <= : NEEDS_LLM_ANALYSIS
                #   <   →  >  : NEEDS_LLM_ANALYSIS
                #   <=  →  >= : NEEDS_LLM_ANALYSIS
                _op_old = old.operator
                _op_new = new.operator
                _narrowing_pairs = {('>=', '>'), ('<=', '<')}
                _relaxing_pairs  = {('>', '>='), ('<', '<=')}
                if (_op_old, _op_new) in _narrowing_pairs:
                    return ChangeDirection.NARROWED, (
                        f"Operator tightened: '{_op_old} {old_num}' → '{_op_new} {new_num}'. "
                        f"Strict operator excludes the boundary value {old_num} → NARROWED (NBC)."
                    )
                elif (_op_old, _op_new) in _relaxing_pairs:
                    return ChangeDirection.RELAXED, (
                        f"Operator relaxed: '{_op_old} {old_num}' → '{_op_new} {new_num}'. "
                        f"Inclusive operator adds boundary value {old_num} → RELAXED (BC)."
                    )
                else:
                    # Direction inversion (> → <, >= → <=, etc.) — semantically complex
                    return ChangeDirection.NEEDS_LLM_ANALYSIS, (
                        f"Comparison direction inverted: '{_op_old} {old_num}' → '{_op_new} {new_num}'. "
                        f"Requires deeper analysis."
                    )
        except ValueError:
            # Not numeric - can't analyze
            return ChangeDirection.INCOMPARABLE, f"Non-numeric comparison: '{old.value}' → '{new.value}'"
    
    def _analyze_subconditions_semantically(
        self,
        old_subs: list,
        new_subs: list,
        clause_type: str = 'or',
    ) -> Optional[Tuple[ChangeDirection, str]]:
        """
        Semantically compare paired subconditions using _analyze_parsed.

        When the number of subconditions is the same, pair them positionally and
        analyze each pair. Combine results based on clause type:

          OR clause:
            All EQUIVALENT → EQUIVALENT
            Any RELAXED, no NARROWED → RELAXED
            Any NARROWED → NARROWED (conservative)

          AND clause:
            All EQUIVALENT → EQUIVALENT
            Any NARROWED, no RELAXED → NARROWED
            Any RELAXED → RELAXED (conservative for AND: relaxing one requirement
                          makes the conjunction less restrictive)
            Mixed NARROWED + RELAXED → NEEDS_LLM_ANALYSIS (cannot determine)

        Returns None if any pair is non-deterministic (NEEDS_LLM_ANALYSIS or INCOMPARABLE).
        """
        if len(old_subs) != len(new_subs):
            return None

        pair_results = []
        for old_sub, new_sub in zip(old_subs, new_subs):
            sub_result = self._analyze_parsed(old_sub, new_sub)
            if sub_result[0] in (ChangeDirection.NEEDS_LLM_ANALYSIS,
                                  ChangeDirection.INCOMPARABLE):
                return None  # Cannot determine — fall through to LLM
            pair_results.append(sub_result[0])

        if not pair_results:
            return None

        unique = set(pair_results)

        if unique == {ChangeDirection.EQUIVALENT}:
            return ChangeDirection.EQUIVALENT, (
                f"All {len(pair_results)} subcondition pair(s) are semantically equivalent."
            )

        if clause_type == 'or':
            # OR: any RELAXED (no NARROWED) → RELAXED; any NARROWED → NARROWED
            if ChangeDirection.NARROWED in unique:
                return ChangeDirection.NARROWED, (
                    f"Subcondition results: {[d.value for d in pair_results]}. "
                    f"At least one NARROWED → OR clause NARROWED (NBC)."
                )
            if ChangeDirection.RELAXED in unique:
                return ChangeDirection.RELAXED, (
                    f"Subcondition results: {[d.value for d in pair_results]}. "
                    f"At least one RELAXED, none NARROWED → OR clause RELAXED (BC)."
                )

        elif clause_type == 'and':
            # AND: any NARROWED (no RELAXED) → NARROWED; any RELAXED → RELAXED
            # Mixed NARROWED + RELAXED → cannot determine
            has_narrowed = ChangeDirection.NARROWED in unique
            has_relaxed = ChangeDirection.RELAXED in unique
            if has_narrowed and has_relaxed:
                return None  # Mixed — cannot determine without deeper analysis
            if has_narrowed:
                return ChangeDirection.NARROWED, (
                    f"Subcondition results: {[d.value for d in pair_results]}. "
                    f"At least one NARROWED → AND clause NARROWED (NBC)."
                )
            if has_relaxed:
                return ChangeDirection.RELAXED, (
                    f"Subcondition results: {[d.value for d in pair_results]}. "
                    f"At least one RELAXED → AND clause RELAXED (BC)."
                )

        return None

    def _analyze_or_clauses(self, old: ParsedCondition, new: ParsedCondition) -> Tuple[ChangeDirection, str]:
        """Analyze change in OR clauses (disjunction = more options = less restrictive)."""
        # Handle case where old is simple but new is OR (added alternatives)
        if not old.subconditions and new.subconditions:
            # Went from single option to multiple - check if old is in new set
            # Use prefix-aware matching: 'L2VSI' matches 'oc-ni-types:L2VSI'
            old_norm = self._normalize_subcondition(old)
            old_norm_stripped = self._strip_value_prefix(old_norm)
            new_set = set(self._normalize_subcondition(sc) for sc in new.subconditions)
            new_set_stripped = {self._strip_value_prefix(s) for s in new_set}
            if old_norm in new_set or old_norm_stripped in new_set_stripped:
                return ChangeDirection.RELAXED, f"OR: Expanded from 1 to {len(new_set)} alternatives (more options)"
            return ChangeDirection.NEEDS_LLM_ANALYSIS, "OR: Expanded to multiple conditions not including original"
        
        # Handle case where old is OR but new is simple (removed alternatives)
        if old.subconditions and not new.subconditions:
            # Went from multiple options to single - check if single is in old set
            # Use prefix-aware matching: 'oc-ni-types:L2VSI' matches 'L2VSI'
            new_norm = self._normalize_subcondition(new)
            new_norm_stripped = self._strip_value_prefix(new_norm)
            old_set = set(self._normalize_subcondition(sc) for sc in old.subconditions)
            old_set_stripped = {self._strip_value_prefix(s) for s in old_set}
            if new_norm in old_set or new_norm_stripped in old_set_stripped:
                return ChangeDirection.NARROWED, f"OR: Reduced from {len(old_set)} alternatives to 1 (fewer options)"
            return ChangeDirection.NEEDS_LLM_ANALYSIS, "OR: Reduced to single condition not in original set"
        
        old_set = set(self._normalize_subcondition(sc) for sc in old.subconditions)
        new_set = set(self._normalize_subcondition(sc) for sc in new.subconditions)
        
        if old_set == new_set:
            return ChangeDirection.EQUIVALENT, "Same OR conditions (possibly reordered)"
        
        added = new_set - old_set
        removed = old_set - new_set
        
        if added and not removed:
            return ChangeDirection.RELAXED, f"OR: Added {len(added)} alternative(s) (more options)"
        elif removed and not added:
            return ChangeDirection.NARROWED, f"OR: Removed {len(removed)} alternative(s) (fewer options)"
        elif added and removed:
            # Both added and removed — check if the difference is only namespace prefix
            # changes on comparison values (e.g. 'L2P2P' → 'oc-ni-types:L2P2P').
            # This is a common pattern when adding module prefixes to qualify identity refs.
            prefix_result = self._check_prefix_only_change(added, removed)
            if prefix_result == 'equivalent':
                return ChangeDirection.EQUIVALENT, (
                    f"OR: Namespace prefix qualification added/removed on {len(added)} "
                    f"condition(s) — same local identity names, semantically equivalent"
                )
            elif prefix_result == 'prefix_changed':
                # Both sides have prefixes but different ones — could be module rename
                # or genuinely different identity. The XPath resolver confirms the schema
                # path is the same; the identity equivalence requires import resolution.
                return ChangeDirection.NEEDS_LLM_ANALYSIS, (
                    f"OR: {len(added)} condition(s) have different namespace prefixes "
                    f"on same local identity names — may be module rename or different identity"
                )

            # ── Semantic subcondition comparison (OR clause) ──────────────────
            # Try positional semantic comparison when counts match.
            # For OR: overall direction = most permissive of all pairs.
            #   All EQUIVALENT → EQUIVALENT
            #   Any RELAXED (no NARROWED) → RELAXED
            #   Any NARROWED → NARROWED (conservative)
            if (old.subconditions and new.subconditions and
                    len(old.subconditions) == len(new.subconditions)):
                semantic_result = self._analyze_subconditions_semantically(
                    old.subconditions, new.subconditions, clause_type='or'
                )
                if semantic_result is not None:
                    direction, expl = semantic_result
                    return direction, f"OR: {expl}"

            # Genuinely different conditions (different local names or paths)
            return ChangeDirection.NEEDS_LLM_ANALYSIS, f"OR: {len(added)} added, {len(removed)} removed - complex change"
        
        return ChangeDirection.EQUIVALENT, "OR conditions unchanged"
    
    def _analyze_and_clauses(self, old: ParsedCondition, new: ParsedCondition) -> Tuple[ChangeDirection, str]:
        """Analyze change in AND clauses (conjunction = more requirements = more restrictive)."""
        old_set = set(self._normalize_subcondition(sc) for sc in old.subconditions)
        new_set = set(self._normalize_subcondition(sc) for sc in new.subconditions)
        
        if old_set == new_set:
            return ChangeDirection.EQUIVALENT, "Same AND conditions (possibly reordered)"
        
        added = new_set - old_set
        removed = old_set - new_set
        
        if added and not removed:
            return ChangeDirection.NARROWED, f"AND: Added {len(added)} requirement(s) (more restrictions)"
        elif removed and not added:
            return ChangeDirection.RELAXED, f"AND: Removed {len(removed)} requirement(s) (fewer restrictions)"
        elif added and removed:
            # Both added and removed — try semantic subcondition comparison first.
            # For AND: any NARROWED → NARROWED; any RELAXED → RELAXED; mixed → LLM.
            if (old.subconditions and new.subconditions and
                    len(old.subconditions) == len(new.subconditions)):
                semantic_result = self._analyze_subconditions_semantically(
                    old.subconditions, new.subconditions, clause_type='and'
                )
                if semantic_result is not None:
                    direction, expl = semantic_result
                    return direction, f"AND: {expl}"
            # Cannot determine — mark for LLM analysis
            return ChangeDirection.NEEDS_LLM_ANALYSIS, f"AND: {len(added)} added, {len(removed)} removed - complex change"
        
        return ChangeDirection.EQUIVALENT, "AND conditions unchanged"
    
    def _normalize_subcondition(self, cond: ParsedCondition) -> str:
        """Normalize a subcondition for comparison."""
        # Normalize whitespace: replace all whitespace sequences (including newlines) with single space
        # This handles cases where YANG files have multi-line string concatenation
        normalized = ' '.join(cond.raw.strip().split())
        return normalized.lower()

    @staticmethod
    def _strip_value_prefix(normalized_cond: str) -> str:
        """
        Strip namespace prefix from quoted comparison values in a normalized condition string.

        e.g. "../config/type = 'oc-ni-types:l2p2p'" → "../config/type = 'l2p2p'"

        Only strips the prefix from the VALUE side (inside quotes), not from path components.
        This is used for prefix-equivalence checking only — not for the primary normalization.
        """
        import re
        # Match quoted values that contain a colon (prefix:localname pattern)
        # After lowercasing, pattern is: quote + prefix + colon + localname + quote
        return re.sub(
            r"(['\"])([a-z][a-z0-9_-]*):([a-z][a-z0-9_.-]*)(['\"])",
            r'\1\3\4',
            normalized_cond
        )

    def _check_prefix_only_change(self, added: set, removed: set) -> str:
        """
        Check if the difference between two OR-clause sets is only namespace prefix
        changes on comparison values (e.g. 'L2P2P' → 'oc-ni-types:L2P2P').

        This handles three cases for identityref/enum value qualification:
          1. Unqualified → Qualified: 'FOO' → 'prefix:FOO'
             → EQUIVALENT (adding a prefix to an unqualified name is a clarification;
               the unqualified form already referred to the same identity in the module's
               namespace per RFC 7950 §9.10.5)
          2. Qualified → Unqualified: 'prefix:FOO' → 'FOO'
             → EQUIVALENT (same reasoning as above, reversed)
          3. Different prefixes, same local name: 'prefix-a:FOO' → 'prefix-b:FOO'
             → 'prefix_changed' (could be module rename or different identity;
               requires LLM or import resolution to determine)
          4. Different local names: 'FOO' → 'BAR'
             → 'different' (genuinely different values, not a prefix change)

        Args:
            added:   Set of normalized subcondition strings present in new but not old
            removed: Set of normalized subcondition strings present in old but not new

        Returns:
            'equivalent'     — all differences are unqualified↔qualified prefix changes
            'prefix_changed' — all differences have same local name but different prefixes
            'different'      — differences involve genuinely different local names or paths
        """
        if len(added) != len(removed):
            # Different cardinalities — cannot be a pure prefix change
            return 'different'

        # Strip prefixes from both sets and compare local-name-only versions
        added_stripped = {self._strip_value_prefix(s) for s in added}
        removed_stripped = {self._strip_value_prefix(s) for s in removed}

        if added_stripped == removed_stripped:
            # After stripping prefixes, the sets are identical.
            # Now determine if this is unqualified↔qualified or prefix-to-prefix.
            # Check if any item in added has a prefix that removed doesn't (or vice versa).
            has_unqualified_to_qualified = False
            has_prefix_to_prefix = False

            for a_item in added:
                # Find the corresponding removed item (same after stripping)
                a_stripped = self._strip_value_prefix(a_item)
                for r_item in removed:
                    r_stripped = self._strip_value_prefix(r_item)
                    if a_stripped == r_stripped and a_item != r_item:
                        # They differ — check the nature of the difference
                        import re
                        # Extract quoted values from both
                        a_vals = re.findall(r"['\"]([^'\"]+)['\"]", a_item)
                        r_vals = re.findall(r"['\"]([^'\"]+)['\"]", r_item)
                        for av, rv in zip(a_vals, r_vals):
                            a_has_prefix = ':' in av
                            r_has_prefix = ':' in rv
                            if a_has_prefix != r_has_prefix:
                                # One has prefix, other doesn't → unqualified↔qualified
                                has_unqualified_to_qualified = True
                            elif a_has_prefix and r_has_prefix:
                                # Both have prefixes but different → prefix-to-prefix
                                a_prefix = av.split(':', 1)[0]
                                r_prefix = rv.split(':', 1)[0]
                                if a_prefix != r_prefix:
                                    has_prefix_to_prefix = True

            if has_prefix_to_prefix and not has_unqualified_to_qualified:
                # Both sides have prefixes but different ones.
                # Try pyang-based identity resolution to determine if they refer to
                # the same defining module (e.g. mb:L2P2P via import chain vs mc:L2P2P directly).
                if self.old_yang_file and self.new_yang_file:
                    old_prefix_map, new_prefix_map = self._get_prefix_maps()
                    if old_prefix_map or new_prefix_map:
                        # Resolve each pair of (old_prefix:local, new_prefix:local)
                        # to their canonical 'defining_module:local' forms
                        all_resolved_equivalent = True
                        import re
                        for a_item in added:
                            a_stripped = self._strip_value_prefix(a_item)
                            for r_item in removed:
                                r_stripped = self._strip_value_prefix(r_item)
                                if a_stripped == r_stripped and a_item != r_item:
                                    a_vals = re.findall(r"['\"]([^'\"]+)['\"]", a_item)
                                    r_vals = re.findall(r"['\"]([^'\"]+)['\"]", r_item)
                                    for av, rv in zip(a_vals, r_vals):
                                        if ':' in av and ':' in rv:
                                            a_prefix, a_local = av.split(':', 1)
                                            r_prefix, r_local = rv.split(':', 1)
                                            if a_local == r_local and a_prefix != r_prefix:
                                                # Resolve both to canonical form
                                                a_canonical = self._resolve_identity_to_canonical(
                                                    a_prefix, a_local, new_prefix_map,
                                                    self.new_yang_file
                                                )
                                                r_canonical = self._resolve_identity_to_canonical(
                                                    r_prefix, r_local, old_prefix_map,
                                                    self.old_yang_file
                                                )
                                                if a_canonical != r_canonical:
                                                    all_resolved_equivalent = False
                        if all_resolved_equivalent:
                            return 'equivalent'
                return 'prefix_changed'
            return 'equivalent'

        # After stripping prefixes, sets still differ → genuinely different local names
        return 'different'

    def _parse_value_list(self, value: str) -> Set[str]:
        """Parse a value that might be a list (e.g., in complex inequality checks)."""
        # For now, treat as single value
        # Could be enhanced to parse "x and y and z" style lists
        return {value.strip()}
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get analysis statistics."""
        return dict(self.stats)


# Convenience functions for backward compatibility with existing code
def classify_condition_change(old: Optional[str], new: Optional[str]) -> str:
    """
    Classify a condition change (compatible with existing constraint_change API).
    
    Args:
        old: Old condition
        new: New condition
        
    Returns:
        'relaxed', 'narrowed', 'unchanged', 'needs_llm_analysis', or 'incomparable'
    """
    analyzer = ConditionAnalyzer()
    direction, _ = analyzer.analyze_change(old, new)
    
    if direction == ChangeDirection.RELAXED:
        return 'relaxed'
    elif direction == ChangeDirection.NARROWED:
        return 'narrowed'
    elif direction == ChangeDirection.EQUIVALENT:
        return 'unchanged'
    elif direction == ChangeDirection.INCOMPARABLE:
        # INCOMPARABLE should not silently fall through to BC; treat conservatively.
        return 'needs_llm_analysis'
    else:
        return 'needs_llm_analysis'
