import re
import ast
from typing import List, Optional, Set, Tuple

# Import condition semantic analyzer
try:
    from .condition_analyzer import ConditionAnalyzer, ChangeDirection
except ImportError:
    from yang_rag.comparator.helper.condition_analyzer import ConditionAnalyzer, ChangeDirection

# Import pyang broken-condition extractor.
# get_pyang_xpath_broken_lines has its own process-level cache internally, so
# no additional caching layer is needed here.
try:
    from .pyang_utils import get_pyang_xpath_broken_lines
except ImportError:
    try:
        from yang_rag.comparator.helper.pyang_utils import get_pyang_xpath_broken_lines
    except ImportError:
        def get_pyang_xpath_broken_lines(yang_file, search_dirs=None):  # type: ignore[misc]
            return set()


def _get_cached_pyang_broken_lines(yang_file: Optional[str], search_dirs: Optional[List[str]] = None) -> Set[int]:
    """Return pyang XPATH broken line numbers for *yang_file*.

    Delegates to ``get_pyang_xpath_broken_lines`` which maintains its own
    process-level cache, so repeated calls for the same file are free.
    """
    if not yang_file:
        return set()
    return get_pyang_xpath_broken_lines(yang_file, search_dirs)

# Import FSM-based regex analyzer
try:
    from .regex_fsm_analyzer import compare_regex_patterns, INTEREGULAR_AVAILABLE
except ImportError:
    try:
        from yang_rag.comparator.helper.regex_fsm_analyzer import compare_regex_patterns, INTEREGULAR_AVAILABLE
    except ImportError:
        INTEREGULAR_AVAILABLE = False
        def compare_regex_patterns(old: str, new: str) -> str:
            return 'error'


def _strip_tags(s: str) -> str:
    """Remove trailing compatibility tags like " <backward-compatible>" from a line."""
    return re.sub(r"\s*<.*?>\s*$", "", s).strip()


def parse_old_new_from_line(line: str, lines: Optional[List[str]] = None, line_index: Optional[int] = None) -> Tuple[Optional[str], Optional[str]]:
    """Parse a report line to extract (old, new) raw value strings.
    
    Handles multi-line values by collecting continuation lines.

    Returns a tuple (old, new) where either may be None.
    Examples:
      "... -> false" -> (None, 'false')
      "... -> new (was old)" -> ('old', 'new')
    
    Args:
        line: The current line being parsed
        lines: Optional list of all lines (for collecting continuation lines)
        line_index: Optional index of current line in lines list
    """
    # If we have context, collect continuation lines for multi-line values
    if lines is not None and line_index is not None:
        # Determine the indentation level of the current line
        current_indent = len(line) - len(line.lstrip())
        
        # Collect this line and any continuation lines (more indented than current)
        full_text = line
        i = line_index + 1
        item_line_re = re.compile(r'^\s*\d+\.\d+\s+')
        while i < len(lines):
            next_line = lines[i]
            next_indent = len(next_line) - len(next_line.lstrip())
            # Continuation lines should be more indented AND not start a new item (e.g., "2.3")
            if next_indent > current_indent and not item_line_re.match(next_line):
                full_text += " " + next_line.strip()
                i += 1
            else:
                break
        
        line = full_text
    
    line = _strip_tags(line)
    m = re.search(r"->\s*(.+?)(?:\s*\(was\s*(.+?)\))?$", line, re.DOTALL)
    if not m:
        return None, None
    new = m.group(1).strip()
    old = m.group(2).strip() if m.group(2) else None
    return old, new


def _parse_numbers_spec(s: Optional[str]) -> Optional[List[Tuple[int, Optional[int]]]]:
    """Parse numbers spec like '1..14 | 17 | 21' into merged intervals.

    Returns list of (low, high) where high=None represents +inf, or None on parse failure.
    """
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    parts = [p.strip() for p in s.split('|') if p.strip()]
    intervals: List[Tuple[int, Optional[int]]] = []
    for p in parts:
        # range
        m = re.match(r"^([0-9]+)\s*\.\.\s*([0-9]+|max)$", p)
        if m:
            low = int(m.group(1))
            high = None if m.group(2) == 'max' else int(m.group(2))
            intervals.append((low, high))
            continue
        # single number
        m2 = re.match(r"^([0-9]+)$", p)
        if m2:
            v = int(m2.group(1))
            intervals.append((v, v))
            continue
        return None

    # merge overlapping/adjacent intervals
    intervals.sort(key=lambda x: x[0])
    merged: List[Tuple[int, Optional[int]]] = []
    for low, high in intervals:
        if not merged:
            merged.append((low, high))
            continue
        last_low, last_high = merged[-1]
        # last_high None means +inf, it covers everything
        if last_high is None:
            continue
        # if current overlaps or adjacent
        if low <= (last_high + 1):
            # extend
            if high is None:
                merged[-1] = (last_low, None)
            else:
                merged[-1] = (last_low, max(last_high, high))
        else:
            merged.append((low, high))
    return merged


def _intervals_contains(a: List[Tuple[int, Optional[int]]], b: List[Tuple[int, Optional[int]]]) -> bool:
    """Return True if every interval in a is covered by union of intervals in b."""
    for al, ah in a:
        covered = False
        for bl, bh in b:
            # bh None means +inf
            if bl <= al and (bh is None or (ah is not None and bh >= ah)):
                covered = True
                break
        if not covered:
            return False
    return True


def classify_xpath_attribute_change(
    attr_entry: dict,
    old: Optional[str],
    new: Optional[str],
    old_yang_file: Optional[str] = None,
    new_yang_file: Optional[str] = None,
    context_path: Optional[str] = None,
    search_dirs: Optional[List[str]] = None,
) -> str:
    """Classify an attribute change whose value is an XPath path expression.

    This is used for attributes declared with ``type="XPath"`` in the XML rules
    (e.g. leafref ``path``, augment/deviation ``name``).  The attribute value is
    treated as an XPath path expression and compared semantically:

    - If both XPaths resolve to the same schema node → ``'unchanged'``
      (semantics-preserving, backward-compatible).
    - If they resolve to different nodes → ``'not_equivalent'``
      (not semantics-preserving, non-backward-compatible).
    - If resolution is unavailable or ambiguous → ``'needs_llm_analysis'``
      (requires deeper analysis, sent to LLM).

    Note: XPath path expressions do not have a "narrowed/relaxed" concept like
    numeric ranges or regex patterns.  A path either points to the same schema
    node (equivalent) or a different one (not equivalent).  There is no partial
    overlap.  The ``'not_equivalent'`` result maps to the NBC rule
    (``semantics-preserving="false"``).

    Prefix stripping is intentionally NOT used as a fallback because it is
    semantically incorrect: ``oc-inv:components`` (openconfig-inventory) and
    ``oc-platform:components`` (openconfig-platform) have the same local name
    but refer to different modules.  When the XPath resolver is unavailable,
    we conservatively return ``'needs_llm_analysis'`` rather than guess.

    Args:
        attr_entry: Attribute rule entry dict (from XML ``attribute_defs``).
        old: Old attribute value (XPath string).
        new: New attribute value (XPath string).
        old_yang_file: Path to old YANG file (for XPath resolution).
        new_yang_file: Path to new YANG file (for XPath resolution).
        context_path: Schema path where the attribute is attached.
        search_dirs: Directories to search for imported modules.

    Returns one of: ``'unchanged'``, ``'not_equivalent'``, ``'needs_llm_analysis'``, ``'unknown'``
    """
    if not old or not new:
        return 'unknown'

    # ── Primary: semantic XPath comparison via _are_paths_equivalent ─────────
    # Import here to avoid circular imports at module level.
    try:
        from yang_rag.comparator.check_compatibility import _are_paths_equivalent
    except ImportError:
        try:
            from ..check_compatibility import _are_paths_equivalent
        except ImportError:
            _are_paths_equivalent = None  # type: ignore[assignment]

    if _are_paths_equivalent is not None and context_path:
        try:
            equivalent = _are_paths_equivalent(
                str(old), str(new), context_path,
                old_yang_file=old_yang_file,
                new_yang_file=new_yang_file,
                search_dirs=search_dirs,
            )
            if equivalent:
                return 'unchanged'
            else:
                # Paths resolve to different schema nodes → not semantics-preserving.
                # Do NOT fall through to prefix-stripping: the resolver has given us
                # an authoritative answer.
                return 'not_equivalent'
        except Exception:
            pass  # Resolver raised an exception (including inconclusive ValueError) → fall through to LLM analysis

    # ── No resolver available or resolver raised an exception ─────────────────
    # Prefix stripping is NOT used here because it is semantically incorrect:
    # different modules can share the same local node names (e.g. oc-inv vs
    # oc-platform both have /components/component/name but refer to different
    # schema trees).  Conservatively request LLM analysis.
    return 'needs_llm_analysis'


def classify_constraint_change(
    constraint_entry: dict,
    old: Optional[str],
    new: Optional[str],
    old_yang_file: Optional[str] = None,
    new_yang_file: Optional[str] = None,
    context_path: Optional[str] = None,
    search_dirs: Optional[List[str]] = None
) -> str:
    """Classify constraint change.

    Args:
        constraint_entry: Constraint rule entry from XML
        old: Old constraint value
        new: New constraint value
        old_yang_file: Path to old YANG file (for XPath resolution)
        new_yang_file: Path to new YANG file (for XPath resolution)
        context_path: Schema path where constraint is attached (for XPath resolution)
        search_dirs: Directories to search for imported modules

    Returns one of: 'relaxed','narrowed','undetermined','unchanged','unknown'
    """
    name = constraint_entry.get('name', '')
    ctype = (constraint_entry.get('type') or '').lower()

    # ── type="XPath": attribute value is an XPath path expression ────────────
    # Delegate to the dedicated XPath attribute classifier.
    if ctype == 'xpath':
        return classify_xpath_attribute_change(
            constraint_entry, old, new,
            old_yang_file=old_yang_file,
            new_yang_file=new_yang_file,
            context_path=context_path,
            search_dirs=search_dirs,
        )

    # Generic handling for condition type constraints (when/must) - use semantic analyzer
    if ctype == 'condition':
        # Collect pyang-validated broken XPath lines for both YANG files.
        # These are used by ConditionAnalyzer to authoritatively determine whether
        # a must/when condition is broken (always-FALSE) in the old or new schema,
        # overriding the XPath resolver which may fail on downward paths from grouping
        # contexts (e.g. "config/leaf" when the context is a grouping, not a data node).
        old_broken = _get_cached_pyang_broken_lines(old_yang_file, search_dirs)
        new_broken = _get_cached_pyang_broken_lines(new_yang_file, search_dirs)

        analyzer = ConditionAnalyzer(
            enable_xpath_resolution=True,
            old_yang_file=old_yang_file,
            new_yang_file=new_yang_file,
            context_path=context_path,
            search_dirs=search_dirs,
            old_pyang_broken_lines=old_broken,
            new_pyang_broken_lines=new_broken,
        )
        direction, explanation = analyzer.analyze_change(old, new)
        
        # Check if DSPy assistance is enabled for complex cases
        assistance = constraint_entry.get('assistance', '').lower() == 'true'
        
        # Map the direction to the expected return values
        if direction == ChangeDirection.NARROWED:
            return 'narrowed'
        elif direction == ChangeDirection.RELAXED:
            return 'relaxed'
        elif direction == ChangeDirection.EQUIVALENT:
            return 'unchanged'
        elif direction == ChangeDirection.NEEDS_LLM_ANALYSIS:
            # Complex case that needs deeper analysis.
            # Always mark for LLM/deep analysis — returning 'unknown' here causes the
            # rule loop to 'continue' and the line gets no tag, which means it falls
            # through to generate_compatibility_list.py (untagged → BC). That is a
            # false-negative. Conservative default: NBC with needs-deep-analysis tag.
            return 'needs_llm_analysis'
        elif direction == ChangeDirection.INCOMPARABLE:
            # Incomparable cases might be complex transformations (e.g., De Morgan's Law).
            # Same reasoning as NEEDS_LLM_ANALYSIS: do not silently treat as 'unknown'
            # (which becomes BC). Mark for deep analysis instead.
            return 'needs_llm_analysis'
        else:
            return 'unknown'
    
    # If type not provided in the rule, infer it from the observed old/new values
    if not ctype:
        def _looks_like_numbers(x: Optional[str]) -> bool:
            try:
                return _parse_numbers_spec(x) is not None
            except Exception:
                return False

        def _looks_like_boolean(x: Optional[str]) -> bool:
            if x is None:
                return False
            s = str(x).strip().lower()
            return s in {'true', 'false'}

        def _looks_like_number(x: Optional[str]) -> bool:
            if x is None:
                return False
            s = str(x).strip()
            try:
                float(s)
                return True
            except Exception:
                return False

        if _looks_like_numbers(old) or _looks_like_numbers(new):
            ctype = 'numbers'
        elif _looks_like_boolean(old) or _looks_like_boolean(new):
            ctype = 'boolean'
        elif _looks_like_number(old) or _looks_like_number(new):
            ctype = 'number'
        else:
            ctype = ''

    if old is None and new is None:
        return 'unknown'
    if old == new:
        return 'unchanged'

    # normalizers
    def norm_bool(v: Optional[str]) -> Optional[bool]:
        if v is None:
            return None
        s = str(v).strip().lower()
        if s in {'true', 'false'}:
            return s == 'true'
        return None

    def norm_number(v: Optional[str]) -> Optional[float]:
        if v is None:
            return None
        s = str(v).strip()
        try:
            if '.' in s:
                return float(s)
            return int(s)
        except Exception:
            return None

    def norm_list(v: Optional[str]):
        if v is None:
            return None
        s = str(v).strip()
        if s.startswith('[') and s.endswith(']'):
            try:
                return ast.literal_eval(s)
            except Exception:
                return None
        return None

    # handle boolean
    if ctype == 'boolean':
        no = norm_bool(old)
        nn = norm_bool(new)
        if no is None and nn is None:
            return 'unknown'
        if no == nn:
            return 'unchanged'
        if no is False and nn is True:
            return 'relaxed'
        if no is True and nn is False:
            return 'narrowed'
        # Cases like old=None, new=True -> relaxed (added allowing)
        if no is None and nn is True:
            return 'relaxed'
        if no is None and nn is False:
            return 'narrowed'
        return 'unknown'

    # handle single numeric constraints
    if ctype == 'number':
        no = norm_number(old)
        nn = norm_number(new)
        if no is None or nn is None:
            return 'unknown'
        # heuristics based on name
        lname = name.lower()
        if 'min' in lname:
            if nn < no:
                return 'relaxed'
            if nn > no:
                return 'narrowed'
            return 'unchanged'
        if 'max' in lname:
            if nn > no:
                return 'relaxed'
            if nn < no:
                return 'narrowed'
            return 'unchanged'
        # otherwise compare absolute
        if nn > no:
            return 'narrowed'
        if nn < no:
            return 'relaxed'
        return 'unchanged'

    # handle ranges/unions
    if ctype == 'numbers':
        old_ints = _parse_numbers_spec(old)
        new_ints = _parse_numbers_spec(new)
        if old_ints is None or new_ints is None:
            classification = 'unknown'
        elif old_ints == new_ints:
            classification = 'unchanged'
        elif _intervals_contains(old_ints, new_ints):
            classification = 'relaxed'
        elif _intervals_contains(new_ints, old_ints):
            classification = 'narrowed'
        else:
            # partial overlap or disjoint: use heuristics comparing overall min/max
            def overall_min_max(intervals: List[Tuple[int, Optional[int]]]):
                mins = [i[0] for i in intervals]
                maxs = [i[1] for i in intervals]
                overall_min = min(mins) if mins else None
                # treat None as +inf when computing overall max
                if any(m is None for m in maxs):
                    overall_max = None
                else:
                    overall_max = max(maxs) if maxs else None
                return overall_min, overall_max

            old_min, old_max = overall_min_max(old_ints)
            new_min, new_max = overall_min_max(new_ints)

            # If the old max was unbounded/infinite and the new max is bounded, it's narrowed
            if old_max is None and new_max is not None:
                classification = 'narrowed'
            # If both bounded and new max is smaller -> narrowed
            elif old_max is not None and new_max is not None and new_max < old_max:
                classification = 'narrowed'
            # If new min is smaller (extends lower bound) while max did not decrease -> relaxed
            elif new_min is not None and old_min is not None and new_min < old_min and (old_max is None or new_max is None or (new_max >= old_max)):
                classification = 'relaxed'
            else:
                classification = 'redefined'

        # If we have a 'redefined' classification for numeric ranges, refine it:
        # Mixed boundary movement both adds and removes values. From a compatibility
        # standpoint any removal (narrowing) should dominate because clients relying
        # on removed values may break. So conservatively downgrade 'redefined' to
        # 'narrowed' so that narrowed rules (typically non-backward-compatible)
        # are applied instead of producing no match (and thus no tag).
        if classification == 'redefined':
            classification = 'narrowed'

        return classification

    # handle regex
    if ctype == 'regex':
        # Use FSM-based analysis for robust regex comparison
        oldp = (old or '').strip()
        newp = (new or '').strip()
        if not oldp and not newp:
            return 'unknown'
        if oldp == newp:
            return 'unchanged'

        # Try FSM-based comparison first (most robust)
        if INTEREGULAR_AVAILABLE:
            fsm_result = compare_regex_patterns(oldp, newp)
            if fsm_result == 'equivalent':
                return 'unchanged'
            elif fsm_result == 'relaxed':
                return 'relaxed'
            elif fsm_result == 'narrowed':
                return 'narrowed'
            elif fsm_result == 'incomparable':
                # Incomparable means overlapping but non-subset — some old-valid
                # strings may be rejected.  If the rule has assistance=true (i.e.
                # an LLM can adjudicate), flag it for LLM analysis; otherwise fall
                # back to the conservative 'narrowed' classification.
                assistance = constraint_entry.get('assistance', '').lower() == 'true'
                if assistance:
                    return 'needs_llm_analysis'
                return 'narrowed'
            # If fsm_result == 'error', fall through to heuristic analysis
        
        # Fallback heuristic analysis if FSM fails or unavailable
        def _strip_anchors(p: str) -> str:
            return re.sub(r'^\^?', '', re.sub(r'\$?$', '', p)).strip()

        def _has_start_anchor(p: str) -> bool:
            return bool(re.match(r'^\s*\^', p))

        def _has_end_anchor(p: str) -> bool:
            return bool(re.search(r'\$\s*$', p))

        # quick obvious relaxed cases: new is a full wildcard
        if newp in {'.*', '^.*$', '.*$'}:
            return 'relaxed'

        # Anchor-only change with identical inner pattern.
        # NOTE: In YANG (RFC 7950 §9.4.5), pattern statements are XSD regular
        # expressions that ALWAYS match the full string — ^ and $ are implicit.
        # Therefore adding or removing explicit ^ / $ when the core is unchanged
        # is SEMANTICALLY EQUIVALENT and must not be reported as narrowed/relaxed.
        so = _strip_anchors(oldp)
        sn = _strip_anchors(newp)
        if so == sn and so:
            return 'unchanged'

        # Unable to confidently classify regex relation with heuristics
        # Check if LLM assistance is enabled for complex patterns
        assistance = constraint_entry.get('assistance', '').lower() == 'true'
        if assistance:
            # Complex regex pattern that needs LLM verification
            return 'needs_llm_analysis'
        else:
            # Conservative fallback without LLM: treat as narrowed 
            # (changed semantics likely remove some previously valid values)
            return 'narrowed'

    # if values look like lists (e.g., membership-style constraints)
    lold = norm_list(old)
    lnew = norm_list(new)
    if lold is not None or lnew is not None:
        if lold == lnew:
            return 'unchanged'
        return 'redefined'

    # fallback string equality
    if old is not None and new is not None and str(old).strip() == str(new).strip():
        return 'unchanged'

    return 'unknown'
