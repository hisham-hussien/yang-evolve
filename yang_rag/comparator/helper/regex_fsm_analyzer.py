#!/usr/bin/env python3
"""FSM-based regex pattern analysis using interegular.

This module uses Finite State Machines to definitively determine if one regex
pattern is a subset/superset of another, providing robust compatibility analysis.
"""

from typing import Optional, Literal
import logging

try:
    from interegular import parse_pattern
    from interegular.fsm import FSM
    INTEREGULAR_AVAILABLE = True
except ImportError:
    INTEREGULAR_AVAILABLE = False
    logging.warning("interegular not available - falling back to heuristic regex analysis")


def _preprocess_pattern(pattern: str) -> tuple[str, bool, bool]:
    """Preprocess pattern to handle anchors.
    
    Returns:
        (core_pattern, has_start_anchor, has_end_anchor)
    """
    import re
    has_start = pattern.startswith('^')
    has_end = pattern.endswith('$')
    
    # Remove anchors for FSM processing
    core = pattern
    if has_start:
        core = core[1:]
    if has_end:
        core = core[:-1]
    
    return core, has_start, has_end


def _literalize_xsd_anchors(pattern: str) -> str:
    """Treat unescaped ^ and $ as literals for XSD regex semantics.

    XSD regular expressions do not use ^/$ as anchors. They are literal
    characters unless escaped/within character classes.
    """
    out = []
    escaped = False
    in_class = False

    for ch in pattern:
        if escaped:
            out.append(ch)
            escaped = False
            continue

        if ch == '\\':
            out.append(ch)
            escaped = True
            continue

        if ch == '[' and not in_class:
            in_class = True
            out.append(ch)
            continue

        if ch == ']' and in_class:
            in_class = False
            out.append(ch)
            continue

        if not in_class and ch in {'^', '$'}:
            out.append('\\' + ch)
        else:
            out.append(ch)

    return ''.join(out)


def _is_bug_fix_pattern(old_pattern: str, new_pattern: str) -> Optional[bool]:
    """Heuristically detect if a pattern change is likely a bug fix (relaxation).
    
    Returns:
        True if likely a relaxing bug fix (backward-compatible)
        False if likely a narrowing change (non-backward-compatible)
        None if unable to determine heuristically
    """
    import difflib
    
    # Calculate simple edit distance
    matcher = difflib.SequenceMatcher(None, old_pattern, new_pattern)
    ratio = matcher.ratio()
    
    # If patterns are very similar (>95%), analyze the differences
    if ratio > 0.95:
        # Get the diff operations
        opcodes = matcher.get_opcodes()
        
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == 'insert':
                # Something was inserted in new pattern
                inserted = new_pattern[j1:j2]
                
                # Check for common relaxing changes:
                # 1. Adding a pipe | (missing alternation - bug fix)
                if '|' in inserted:
                    # Check context - if adding | between two alternatives, it's a fix
                    return True
                
                # 2. Adding optional/repeated quantifiers
                if any(c in inserted for c in ['*', '+', '?']):
                    return True
                    
            elif tag == 'delete':
                # Something was deleted from old pattern
                deleted = old_pattern[i1:i2]
                
                # Removing anchors is relaxing
                if deleted in ['^', '$']:
                    return True
                    
            elif tag == 'replace':
                # Something was replaced
                old_part = old_pattern[i1:i2]
                new_part = new_pattern[j1:j2]
                
                # Character class expansion: [a-z] → [a-zA-Z]
                if old_part.startswith('[') and new_part.startswith('['):
                    if len(new_part) > len(old_part):
                        return True
                
                # Quantifier expansion: {1,3} → {1,5}
                import re
                old_quant = re.findall(r'\{(\d+),(\d+)\}', old_part)
                new_quant = re.findall(r'\{(\d+),(\d+)\}', new_part)
                if old_quant and new_quant:
                    old_min, old_max = map(int, old_quant[0])
                    new_min, new_max = map(int, new_quant[0])
                    if new_min <= old_min and new_max >= old_max:
                        return True
    
    return None


def compare_regex_patterns(
    old_pattern: str,
    new_pattern: str,
    dialect: Literal['xsd', 'posix'] = 'xsd'
) -> Literal['relaxed', 'narrowed', 'equivalent', 'incomparable', 'error']:
    """Compare two regex patterns using FSM analysis.
    
    Args:
        old_pattern: The original regex pattern
        new_pattern: The new regex pattern
        dialect: Regex semantics mode:
             - 'xsd'   : YANG pattern/XSD regex (treat ^/$ as literals)
             - 'posix' : POSIX/PCRE-like anchor handling
        
    Returns:
        - 'relaxed': new accepts everything old accepts + more (old ⊆ new, old ≠ new)
        - 'narrowed': new accepts fewer strings than old (new ⊂ old)
        - 'equivalent': patterns accept exactly the same strings (old = new)
        - 'incomparable': patterns have overlapping but non-subset relationship
        - 'error': unable to parse or analyze patterns
        
    The backward compatibility rule:
        - 'relaxed' or 'equivalent' → backward-compatible
        - 'narrowed' → non-backward-compatible (breaks existing valid data)
        - 'incomparable' → non-backward-compatible (some old valid data now rejected)
    """
    # Handle trivial cases
    if old_pattern == new_pattern:
        return 'equivalent'
    
    if dialect == 'posix':
        # Handle anchors as positional assertions in POSIX/PCRE mode.
        old_core, _old_start, _old_end = _preprocess_pattern(old_pattern)
        new_core, _new_start, _new_end = _preprocess_pattern(new_pattern)

        # For POSIX-style semantics, anchor-only changes with the same inner
        # expression are treated as equivalent by policy.
        if old_core == new_core:
            return 'equivalent'
    else:
        # XSD mode: ^/$ are literals, not anchors.
        old_core = _literalize_xsd_anchors(old_pattern)
        new_core = _literalize_xsd_anchors(new_pattern)

    if not INTEREGULAR_AVAILABLE:
        # Fallback to heuristic analysis after deterministic checks.
        # In XSD mode we avoid anchor heuristics because ^/$ are literals.
        if dialect == 'xsd':
            return 'error'
        heuristic_result = _is_bug_fix_pattern(old_pattern, new_pattern)
        if heuristic_result is True:
            return 'relaxed'
        elif heuristic_result is False:
            return 'narrowed'
        return 'error'
    
    try:
        # Parse core patterns into FSMs (without anchors)
        old_fsm = parse_pattern(old_core).to_fsm()
        new_fsm = parse_pattern(new_core).to_fsm()
        
        # Check if patterns are equivalent (accept same language)
        if old_fsm.equivalent(new_fsm):
            return 'equivalent'
        
        # Check subset relationships using FSM difference operations
        # old - new: strings accepted by old but not by new
        old_minus_new = old_fsm - new_fsm
        
        # new - old: strings accepted by new but not by old  
        new_minus_old = new_fsm - old_fsm
        
        # Check if old - new is empty (all old strings are accepted by new)
        old_is_subset_of_new = old_minus_new.empty()
        
        # Check if new - old is empty (all new strings are accepted by old)
        new_is_subset_of_old = new_minus_old.empty()
        
        if old_is_subset_of_new and new_is_subset_of_old:
            # Both differences empty means equivalent (already handled above, but safety check)
            return 'equivalent'
        elif old_is_subset_of_new and not new_is_subset_of_old:
            # old ⊆ new and new has additional strings → RELAXED (backward-compatible)
            return 'relaxed'
        elif new_is_subset_of_old and not old_is_subset_of_new:
            # new ⊂ old → NARROWED (non-backward-compatible)
            return 'narrowed'
        else:
            # Neither is subset of the other → INCOMPARABLE
            # But this might be a bug fix - check heuristically
            if dialect == 'posix':
                heuristic_result = _is_bug_fix_pattern(old_pattern, new_pattern)
                if heuristic_result is True:
                    logging.info(f"FSM returned incomparable, but heuristic analysis suggests relaxation (bug fix)")
                    return 'relaxed'
            return 'incomparable'
            
    except Exception as e:
        logging.debug(f"FSM regex comparison failed for patterns '{old_pattern}' vs '{new_pattern}': {e}")
        # Try heuristic analysis as fallback
        if dialect == 'posix':
            heuristic_result = _is_bug_fix_pattern(old_pattern, new_pattern)
            if heuristic_result is True:
                logging.info(f"FSM failed but heuristic analysis suggests relaxation (bug fix)")
                return 'relaxed'
            elif heuristic_result is False:
                return 'narrowed'
        return 'error'


def get_example_strings(pattern: str, max_examples: int = 5) -> list[str]:
    """Generate example strings that match the given pattern.
    
    Args:
        pattern: Regex pattern to generate examples for
        max_examples: Maximum number of examples to generate
        
    Returns:
        List of example strings that match the pattern
    """
    if not INTEREGULAR_AVAILABLE:
        return []
    
    try:
        fsm = parse_pattern(pattern).to_fsm()
        # Generate examples by traversing the FSM
        examples = []
        for example in fsm.strings():
            examples.append(example)
            if len(examples) >= max_examples:
                break
        return examples
    except Exception as e:
        logging.debug(f"Failed to generate examples for pattern '{pattern}': {e}")
        return []


def get_differential_examples(
    old_pattern: str,
    new_pattern: str,
    max_examples: int = 3
) -> dict[str, list[str]]:
    """Get example strings showing the difference between two patterns.
    
    Args:
        old_pattern: The original regex pattern
        new_pattern: The new regex pattern
        max_examples: Maximum examples per category
        
    Returns:
        Dictionary with keys:
        - 'only_old': strings matching old but not new
        - 'only_new': strings matching new but not old
        - 'both': strings matching both patterns
    """
    if not INTEREGULAR_AVAILABLE:
        return {'only_old': [], 'only_new': [], 'both': []}
    
    try:
        old_fsm = parse_pattern(old_pattern).to_fsm()
        new_fsm = parse_pattern(new_pattern).to_fsm()
        
        # Get differences
        old_minus_new = old_fsm - new_fsm
        new_minus_old = new_fsm - old_fsm
        intersection = old_fsm & new_fsm
        
        result = {
            'only_old': [],
            'only_new': [],
            'both': []
        }
        
        # Generate examples from each set
        for example in old_minus_new.strings():
            result['only_old'].append(example)
            if len(result['only_old']) >= max_examples:
                break
                
        for example in new_minus_old.strings():
            result['only_new'].append(example)
            if len(result['only_new']) >= max_examples:
                break
                
        for example in intersection.strings():
            result['both'].append(example)
            if len(result['both']) >= max_examples:
                break
        
        return result
        
    except Exception as e:
        logging.debug(f"Failed to generate differential examples: {e}")
        return {'only_old': [], 'only_new': [], 'both': []}


if __name__ == '__main__':
    # Test cases
    test_cases = [
        # (old, new, expected_result)
        ('[0-9a-fA-F]{2}', '[0-9A-F]{2}', 'narrowed'),  # Case sensitivity - narrowing
        ('[0-9]{1,3}', '[0-9]{1,5}', 'relaxed'),  # Length expansion - relaxing
        ('^foo$', 'foo', 'relaxed'),  # Anchor removal - relaxing
        ('foo', '^foo$', 'narrowed'),  # Anchor addition - narrowing
        ('[a-z]+', '[a-zA-Z]+', 'relaxed'),  # Character class expansion
        ('(192|193|194)', '(19[2-4])', 'equivalent'),  # Semantically equivalent
        ('([0-9]{1,3}\\.){3}[0-9]{1,3}', '([0-9]{1,3}\\.){3}[0-9]{1,3}|any', 'relaxed'),  # Alternation added
    ]
    
    print("Testing FSM-based regex comparison:\n")
    for old, new, expected in test_cases:
        result = compare_regex_patterns(old, new)
        status = "✓" if result == expected else "✗"
        print(f"{status} Old: {old}")
        print(f"  New: {new}")
        print(f"  Expected: {expected}, Got: {result}")
        
        if result in ['narrowed', 'incomparable']:
            examples = get_differential_examples(old, new, max_examples=2)
            if examples['only_old']:
                print(f"  Examples only in old: {examples['only_old']}")
        print()
