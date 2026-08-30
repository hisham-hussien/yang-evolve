"""
Integration Hook for LLM Verification in YANG Comparator
---------------------------------------------------------
This module provides hooks to integrate LLM verification into the
existing YANG comparison workflow.
"""

import sys
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from rich.console import Console
from rich.panel import Panel

# Add project root to path
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

# Try relative import first (when running as module)
try:
    from .llm_verification import (
        verify_compatibility_change,
        batch_verify_changes,
        needs_llm_verification
    )
    from yang_rag.comparator.helper.regex_fsm_analyzer import compare_regex_patterns
except ImportError:
    # Fall back to absolute import (when running from root)
    from yang_rag.dspy.llm_verification import (
        verify_compatibility_change,
        batch_verify_changes,
        needs_llm_verification
    )
    from yang_rag.comparator.helper.regex_fsm_analyzer import compare_regex_patterns

console = Console()


def _resolve_verification_outcome(
    *,
    classification: str,
    static_class: str,
    semantic_outcome: str,
    orig_decision: str,
    llm_compat: str,
) -> str:
    """Resolve final confirmed/conflict outcome for one LLM-verified item.

    Rules:
    - If LLM call failed (classification=llm_error), always conflict.
    - If static semantic class is known (narrowed/relaxed/equivalent/incomparable),
      compare semantic classes directly.
    - Otherwise (for needs_llm_analysis/unknown), compatibility agreement is
      authoritative: if llm_compat differs from orig_decision, mark conflict.
    """
    semantic_classes = {'narrowed', 'relaxed', 'equivalent', 'incomparable'}
    norm_class = (classification or '').strip().lower()
    norm_static = (static_class or '').strip().lower()
    norm_orig = (orig_decision or '').strip().lower()
    norm_llm = (llm_compat or '').strip().lower()
    norm_semantic_outcome = (semantic_outcome or '').strip().lower()

    if norm_class == 'llm_error':
        return 'conflict'

    if norm_static in semantic_classes:
        return 'confirmed' if norm_class == norm_static else 'conflict'

    if norm_llm != norm_orig:
        return 'conflict'

    if norm_semantic_outcome in {'confirmed', 'unconfirmed'}:
        return 'confirmed' if norm_semantic_outcome == 'confirmed' else 'conflict'

    return 'confirmed'


def _apply_static_policy_override(
    *,
    llm_classification: str,
    static_class: str,
    llm_explanation: str,
) -> tuple[str, str, bool]:
    """Force LLM semantic class to static class when static class is authoritative.

    The comparator's static engine implements repository-specific policy
    (including ``incomparable`` handling). When static classification is one of
    the semantic classes, we treat it as authoritative and normalize the LLM
    class to it for compatibility/outcome reconciliation.

    Returns:
        (effective_classification, effective_explanation, overridden)
    """
    semantic_classes = {'narrowed', 'relaxed', 'equivalent', 'incomparable'}
    norm_static = (static_class or '').strip().lower()
    norm_llm = (llm_classification or '').strip().lower()

    if norm_static in semantic_classes and norm_llm != norm_static:
        note = (
            f"Policy override applied: static semantic class '{norm_static}' "
            f"is authoritative over LLM class '{norm_llm or 'unknown'}'."
        )
        combined = f"{llm_explanation} {note}".strip() if llm_explanation else note
        return norm_static, combined, True

    return norm_llm, llm_explanation, False


def _is_report_item_line(line: str) -> bool:
    """Return True for numbered report item lines (e.g. 10.1 or 10.1.1)."""
    return bool(re.match(r'^\s*\d+(?:\.\d+)+\s+', line or ''))


def _strip_followup_change_tail(text: str) -> str:
    """Drop appended sibling-change fragments accidentally joined into one line.

    Example tail to remove:
      " 10.1.1 attribute changed: ['name'] -> string"
    """
    if not text:
        return text
    return re.sub(
        r"\s+\d+(?:\.\d+)+\s+(?:attribute|constraint|keyword)\s+(?:added|changed|deleted)\b.*$",
        '',
        text,
        flags=re.IGNORECASE,
    ).strip()


def _extract_changed_old_new(rhs: str) -> tuple[str, str]:
    """Extract NEW and OLD values from RHS text in "NEW (was OLD)" format."""
    rhs = _strip_followup_change_tail((rhs or '').strip())
    was_idx = rhs.rfind(' (was ')
    if was_idx == -1:
        return rhs, ''

    new_value = rhs[:was_idx].strip()
    old_part = rhs[was_idx + len(' (was '):].strip()

    # Normal canonical form ends with ')'. Keep conservative fallback if not.
    if old_part.endswith(')'):
        old_value = old_part[:-1].strip()
    else:
        last_paren = old_part.rfind(')')
        old_value = old_part[:last_paren].strip() if last_paren != -1 else old_part

    return new_value, old_value


def _compute_regex_static_class(constraint_name: str, old_value: str, new_value: str) -> Optional[str]:
    """Compute deterministic regex semantic class using dialect-aware FSM policy.

    Returns one of semantic classes (narrowed/relaxed/equivalent/incomparable)
    or None when this helper is not applicable.
    """
    c = (constraint_name or '').strip().lower()
    if c not in {'pattern', 'posix-pattern'}:
        return None
    if not (old_value and new_value):
        return None

    dialect = 'posix' if c == 'posix-pattern' else 'xsd'
    result = compare_regex_patterns(old_value, new_value, dialect=dialect)
    if result in {'narrowed', 'relaxed', 'equivalent', 'incomparable'}:
        return result
    return None


def _default_rules_xml_path() -> str:
    """Return bundled compatibility rules XML path."""
    return str(Path(__file__).resolve().parent.parent / 'comparator' / 'compatibility_rules.xml')


def _load_constraint_type_map(xml_rules_path: Optional[str]) -> Dict[str, str]:
    """Load constraint-name -> type mapping from compatibility_rules.xml.

    If a constraint appears multiple times, the first explicit type wins.
    """
    path = xml_rules_path or _default_rules_xml_path()
    p = Path(path)
    if not p.exists():
        return {}

    out: Dict[str, str] = {}
    try:
        root = ET.parse(str(p)).getroot()
        for c in root.findall('.//constraint'):
            name = (c.text or '').strip().lower()
            ctype = (c.attrib.get('type') or '').strip().lower()
            if not name or not ctype:
                continue
            if name not in out:
                out[name] = ctype
    except Exception:
        return {}
    return out


def _is_regex_constraint(
    constraint_name: str,
    constraint_type_map: Optional[Dict[str, str]] = None,
) -> bool:
    """Determine whether a constraint should be treated as regex from XML rules."""
    c = (constraint_name or '').strip().lower()
    if not c:
        return False
    ctype = (constraint_type_map or {}).get(c, '')
    return ctype == 'regex'


def _regex_dialect_for_constraint(
    constraint_name: str,
    constraint_type_map: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Resolve regex dialect for a regex-typed constraint.

    XML currently encodes type=regex, not dialect. We keep YANG defaults:
    - pattern -> xsd
    - posix-pattern -> posix
    - other regex-typed constraints -> xsd
    """
    if not _is_regex_constraint(constraint_name, constraint_type_map):
        return None
    c = (constraint_name or '').strip().lower()
    if c == 'posix-pattern':
        return 'posix'
    return 'xsd'


def _compute_regex_static_class_from_rules(
    constraint_name: str,
    old_value: str,
    new_value: str,
    constraint_type_map: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Compute deterministic regex class/note for XML-typed regex constraints.

    Returns:
      (semantic_class, policy_note)
      - semantic_class: narrowed/relaxed/equivalent/incomparable when available
      - policy_note: explanatory note for static_tool_reason, including unparsed case
    """
    if not (old_value and new_value):
        return None, None

    dialect = _regex_dialect_for_constraint(constraint_name, constraint_type_map)
    if not dialect:
        return None, None

    result = compare_regex_patterns(old_value, new_value, dialect=dialect)
    if result in {'narrowed', 'relaxed', 'equivalent', 'incomparable'}:
        return result, f"Deterministic regex static policy ({constraint_name}) -> {result}"
    if result == 'error':
        return None, (
            f"Deterministic regex static policy ({constraint_name}) -> "
            "unparsed (parser could not analyze pattern pair)"
        )
    return None, None


def enrich_compatibility_with_llm(
    report_lines: List[str],
    rules: List[Dict[str, Any]],
    enriched_lines: List[str],
    llm_model: str = "gpt-4o-mini",
    enable_llm: bool = True
) -> List[str]:
    """Enhance compatibility report with LLM verification.
    
    This function scans the enriched compatibility report and performs
    LLM verification for any rules marked with assistance="true".
    
    Args:
        report_lines: Original comparison report lines
        rules: Parsed rules from compatibility_rules.xml
        enriched_lines: Lines enriched with compatibility decisions
        llm_model: LLM model to use for verification
        enable_llm: If False, skip LLM verification
        
    Returns:
        Enhanced report lines with LLM verification notes
    """
    
    if not enable_llm:
        return enriched_lines
    
    console.print("\n[bold cyan]🤖 Starting LLM Verification Layer...[/bold cyan]")
    
    # Parse enriched lines to extract changes
    changes = _extract_changes_from_report(enriched_lines, rules)
    
    if not changes:
        console.print("[dim]No changes requiring LLM verification found[/dim]")
        return enriched_lines
    
    # Verify changes
    verifications = batch_verify_changes(changes, rules, llm_model)
    
    if not verifications:
        console.print("[dim]No rules with assistance='true' found[/dim]")
        return enriched_lines
    
    # Inject verification results into report
    enhanced_lines = _inject_verification_results(enriched_lines, verifications)
    
    console.print(f"[green]✓[/green] LLM verification complete: {len(verifications)} changes verified")
    
    return enhanced_lines


def _extract_changes_from_report(
    enriched_lines: List[str],
    rules: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Extract change information from enriched report lines.
    
    **IMPORTANT**: This function should ONLY extract changes marked with <needs-dspy-analysis>.
    LLM verification is expensive and should only run for complex cases that the rule-based
    analyzer cannot handle.
    
    Args:
        enriched_lines: Report lines with compatibility annotations
        rules: Parsed rules (not used - kept for compatibility)
        
    Returns:
        List of change dictionaries suitable for verification
    """
    
    changes = []
    current_path = ""

    console.print(f"[dim]Scanning for <needs-deep-analysis> markers in report...[/dim]")

    for idx, line in enumerate(enriched_lines):
        stripped = line.rstrip("\n")

        # Track current section path: lines without leading spaces that look like a path
        if stripped and not stripped.startswith(' '):
            # Example: example-old/user/username/string
            if '/' in stripped:
                current_path = stripped.strip()

        # **KEY REQUIREMENT**: Only process lines with <needs-deep-analysis> marker
        # This marker indicates complex cases that the rule-based analyzer couldn't handle
        if '<needs-deep-analysis>' not in stripped.lower():
            continue

        console.print(f"[cyan]Found marker at line {idx + 1}[/cyan]: {stripped[:100]}...")

        # Parse decision tag
        decision = None
        if '<needs-deep-analysis>' in stripped.lower():
            decision = 'needs-deep-analysis'

        # Detect change lines for constraints/attributes/keywords
        # Examples:
        #   "3.2 constraint changed: ['must'] -> (../priority >= 5 and . != 'be') or ../priority < 5 (was ...) <needs-dspy-analysis>"
        lower = stripped.lower()
        if 'constraint ' in lower and ("['" in stripped or '["' in stripped):
            # Extract action and name
            action = 'changed' if 'changed' in lower else 'added' if 'added' in lower else 'deleted' if 'deleted' in lower else None
            import re
            m = re.search(r"\['([^']+)']", stripped)
            if not m:
                m = re.search(r'\["([^"]+)"\]', stripped)
            constraint_name = m.group(1) if m else None

            if action and constraint_name:
                # Extract old/new values and compatibility tags from the same line
                # Formats:
                # CHANGED: "constraint changed: ['pattern'] -> new_value (was old_value) <non-backward-compatible> <needs-deep-analysis>"
                old_value = ""
                new_value = ""
                original_decision = "needs-deep-analysis"
                
                # Extract the tag IMMEDIATELY BEFORE <needs-deep-analysis>
                # Pattern: <backward-compatible|non-backward-compatible|conditional-backward-compatible> <needs-deep-analysis>
                # Look for the rightmost compatibility tag before needs-deep-analysis
                tag_before_needs_deep = re.search(
                    r'<(backward-compatible|non-backward-compatible|conditional-backward-compatible)>\s*<needs-deep-analysis>',
                    stripped,
                    re.IGNORECASE
                )
                if tag_before_needs_deep:
                    original_decision = tag_before_needs_deep.group(1).strip().lower()
                
                if action == "changed" and " -> " in stripped and " (was " in stripped:
                    # Format: "constraint changed: ['must'] -> NEW (was OLD) <tags>"
                    new_part = stripped.split(" -> ", 1)[1]
                    if " (was " in new_part:
                        new_value = new_part.split(" (was ", 1)[0].strip()
                        old_part = new_part.split(" (was ", 1)[1]
                        # Remove trailing markers
                        if " <" in old_part:
                            old_part = old_part.split(" <", 1)[0]
                        # Remove trailing ) and strip
                        old_value = old_part.rstrip(")").strip()
                
                elif action == "added" and " -> " in stripped:
                    # Format: "constraint added: ['pattern'] -> VALUE <tags>"
                    parts = stripped.split(" -> ", 1)
                    if len(parts) == 2:
                        value_part = parts[1].strip()
                        # Remove trailing markers
                        if " <" in value_part:
                            value_part = value_part.split(" <", 1)[0].strip()
                        new_value = value_part
                        old_value = ""
                
                change_info = {
                    'rule_id': None,
                    'keyword_or_constraint': constraint_name,
                    'action': action,
                    'original_decision': original_decision,
                    'old_value': old_value,
                    'new_value': new_value,
                    'yang_path': current_path,
                    'parent_context': '',
                    'line_number': idx + 1
                }

                changes.append(change_info)
                console.print(f"[green]✓[/green] Extracted change: {constraint_name} {action}")

    console.print(f"[dim]Found {len(changes)} changes requiring LLM verification[/dim]")
    return changes


def _extract_change_details(
    lines: List[str],
    compatibility_line_idx: int,
    rule_id: Optional[str],
    decision: Optional[str],
    current_path: str
) -> Optional[Dict[str, Any]]:
    """Extract change details from lines before the compatibility annotation.
    
    Args:
        lines: All report lines
        compatibility_line_idx: Index of the compatibility annotation line
        rule_id: Rule ID extracted from annotation
        decision: Compatibility decision
        current_path: Current YANG path
        
    Returns:
        Dictionary with change details, or None if parsing fails
    """
    
    # Look backwards up to 10 lines to find the change
    search_start = max(0, compatibility_line_idx - 10)
    
    keyword_or_constraint = None
    action = None
    old_value = ""
    new_value = ""
    
    # Format patterns from enriched_report.txt:
    # ADDED:   "attribute added: ['name'] -> value <backward-compatible>"
    # CHANGED: "constraint changed: ['pattern'] -> new_value (was old_value) <non-backward-compatible>"
    # DELETED: "container deleted) <non-backward-compatible>" (no values)
    
    import re
    
    for i in range(compatibility_line_idx - 1, search_start - 1, -1):
        line = lines[i]
        
        # Look for patterns like:
        # "constraint changed: ['pattern'] -> new (was old)"
        # "attribute added: ['description'] -> value"
        # "(leaf added)"
        
        if "constraint " in line.lower() and ("['" in line or '["' in line):
            # Extract constraint name and action
            if "changed" in line.lower():
                action = "changed"
            elif "added" in line.lower():
                action = "added"
            elif "deleted" in line.lower():
                action = "deleted"
            
            # Extract constraint name from ['...']
            match = re.search(r"\['([^']+)'\]", line)
            if match:
                keyword_or_constraint = match.group(1)
                
                # Extract values from the SAME line
                if action == "changed" and " -> " in line and " (was " in line:
                    # Format: "constraint changed: ['pattern'] -> NEW (was OLD) <...>"
                    new_part = line.split(" -> ", 1)[1]
                    if " (was " in new_part:
                        new_value = new_part.split(" (was ", 1)[0].strip()
                        old_part = new_part.split(" (was ", 1)[1]
                        old_value = old_part.rstrip(")").strip()
                        # Remove trailing compatibility markers
                        if " <" in old_value:
                            old_value = old_value.split(" <", 1)[0].strip()
                
                elif action == "added" and " -> " in line:
                    # Format: "constraint added: ['pattern'] -> VALUE <...>"
                    parts = line.split(" -> ", 1)
                    if len(parts) == 2:
                        value_part = parts[1].strip()
                        # Remove trailing compatibility markers
                        if " <" in value_part:
                            value_part = value_part.split(" <", 1)[0].strip()
                        new_value = value_part
                        old_value = None
                
                break
        
        elif "attribute " in line.lower() and ("['" in line or '["' in line):
            # Similar for attributes
            if "changed" in line.lower():
                action = "changed"
            elif "added" in line.lower():
                action = "added"
            elif "deleted" in line.lower():
                action = "deleted"
            
            match = re.search(r"\['([^']+)'\]", line)
            if match:
                keyword_or_constraint = match.group(1)
                
                # Extract values from the SAME line
                if action == "changed" and " -> " in line and " (was " in line:
                    new_part = line.split(" -> ", 1)[1]
                    if " (was " in new_part:
                        new_value = new_part.split(" (was ", 1)[0].strip()
                        old_part = new_part.split(" (was ", 1)[1]
                        old_value = old_part.rstrip(")").strip()
                        if " <" in old_value:
                            old_value = old_value.split(" <", 1)[0].strip()
                
                elif action == "added" and " -> " in line:
                    parts = line.split(" -> ", 1)
                    if len(parts) == 2:
                        value_part = parts[1].strip()
                        if " <" in value_part:
                            value_part = value_part.split(" <", 1)[0].strip()
                        new_value = value_part
                        old_value = None
                
                break
        
        elif "(" in line and ")" in line and any(a in line.lower() for a in ["added", "deleted", "changed"]):
            # Keyword changes like "(leaf added)" or "(container deleted)"
            match = re.search(r"\((\w+)\s+(added|deleted|changed)\)", line.lower())
            if match:
                keyword_or_constraint = match.group(1)
                action = match.group(2)
                # Keywords typically don't have values in the format we're looking for
                break
    
    if not keyword_or_constraint or not action:
        return None
    
    return {
        'rule_id': rule_id or '',
        'keyword_or_constraint': keyword_or_constraint,
        'action': action,
        'original_decision': decision or '',
        'old_value': old_value,
        'new_value': new_value,
        'yang_path': current_path,
        'parent_context': ''  # Could be enhanced to extract parent info
    }


def _inject_verification_results(
    enriched_lines: List[str],
    verifications: List[Dict[str, Any]]
) -> List[str]:
    """Inject LLM verification results into the enriched report (in-place modification).
    
    New marker logic:
    - If LLM confirms non-BC: Replace "<non-backward-compatible> <needs-deep-analysis>" 
      with "<non-backward-compatible> <confirmed>"
    - If LLM overrides to BC: Replace "<non-backward-compatible> <needs-deep-analysis>" 
      with "<backward-compatible> <overridden>"
    - If no LLM result: Keep "<non-backward-compatible> <needs-deep-analysis>" as-is
    
    Args:
        enriched_lines: Original enriched report lines
        verifications: List of verification results
        
    Returns:
        Enhanced lines with LLM verification results replacing markers
    """
    
    # Create a map from line number to verification result
    line_verification_map = {}
    for v in verifications:
        change = v.get('change', {})
        line_num = change.get('line_number')
        if line_num:
            line_verification_map[line_num] = v['verification']
    
    enhanced = []
    for line_idx, line in enumerate(enriched_lines, start=1):
        # Check if this line has <needs-deep-analysis> marker
        if '<needs-deep-analysis>' in line.lower():
            # Check if we have a verification result for this line
            if line_idx in line_verification_map:
                verification = line_verification_map[line_idx]
                verified_compatibility = verification.get('verified_compatibility', '').lower()
                original_decision = verification.get('original_decision', '').lower()
                
                # IMPORTANT: Only replace <needs-deep-analysis>, keep original decision intact
                # Compare LLM's decision against the original compatibility decision
                if verified_compatibility == original_decision:
                    # LLM CONFIRMED the original decision
                    # Replace: <original-decision> <needs-deep-analysis> → <original-decision> <confirmed>
                    updated_line = re.sub(
                        r'<needs-deep-analysis>',
                        '<confirmed>',
                        line,
                        flags=re.IGNORECASE
                    )
                else:
                    # LLM DISAGREED with original decision
                    # Replace: <original-decision> <needs-deep-analysis> → <original-decision> <conflicted>
                    updated_line = re.sub(
                        r'<needs-deep-analysis>',
                        '<conflicted>',
                        line,
                        flags=re.IGNORECASE
                    )
                
                enhanced.append(updated_line)
            else:
                # No verification found, keep marker as-is (no LLM key case)
                enhanced.append(line)
        else:
            # No marker, keep line as-is
            enhanced.append(line)
    
    return enhanced


def run_comparator_with_llm_verification(
    old_yang_path: str,
    new_yang_path: str,
    rules_xml_path: str = "yang_rag/comparator/compatibility_rules.xml",
    output_path: Optional[str] = None,
    llm_model: str = "gpt-4o-mini",
    enable_llm: bool = True
) -> str:
    """Run YANG comparator with LLM verification enabled.
    
    This is a wrapper that runs the standard YANG comparison but adds
    LLM verification for rules marked with assistance="true".
    
    Args:
        old_yang_path: Path to old YANG file
        new_yang_path: Path to new YANG file
        rules_xml_path: Path to compatibility_rules.xml
        output_path: Optional path to save enhanced report
        llm_model: LLM model to use
        enable_llm: If False, skip LLM verification
        
    Returns:
        Enhanced report text
    """
    
    console.print(Panel.fit(
        "[bold cyan]YANG Comparator with LLM Verification[/bold cyan]\n\n"
        f"Old: {old_yang_path}\n"
        f"New: {new_yang_path}\n"
        f"LLM: {llm_model if enable_llm else 'Disabled'}",
        border_style="cyan"
    ))
    
    # Import comparator modules
    try:
        from yang_comparator.check_compatibility import parse_rules
        from yang_comparator.compare_yang import compare_yang_files
    except ImportError:
        console.print("[red]❌ Failed to import YANG comparator modules[/red]")
        return ""
    
    # Load rules
    rules = parse_rules(rules_xml_path)
    console.print(f"[green]✓[/green] Loaded {len(rules)} compatibility rules")
    
    # Run comparison (this would call the existing comparator)
    # NOTE: This is a simplified example - actual integration would hook into
    # the existing comparison workflow
    console.print("\n[yellow]Running YANG comparison...[/yellow]")
    
    # For now, return a placeholder
    # In actual implementation, this would:
    # 1. Run the comparator
    # 2. Get the enriched report
    # 3. Call enrich_compatibility_with_llm()
    # 4. Return/save the enhanced report
    
    console.print("[yellow]⚠ This is a template - integrate with actual comparator flow[/yellow]")
    
    return ""


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python llm_verification_hook.py <old_yang> <new_yang> [--llm-model MODEL] [--no-llm]")
        sys.exit(1)
    
    old_path = sys.argv[1]
    new_path = sys.argv[2]
    
    # Parse options
    llm_model = "gpt-4o-mini"
    enable_llm = True
    
    for i, arg in enumerate(sys.argv[3:], 3):
        if arg == "--llm-model" and i + 1 < len(sys.argv):
            llm_model = sys.argv[i + 1]
        elif arg == "--no-llm":
            enable_llm = False
    
    # Run with verification
    result = run_comparator_with_llm_verification(
        old_path,
        new_path,
        llm_model=llm_model,
        enable_llm=enable_llm
    )


def verify_enriched_report(
    enriched_report_path: str,
    output_dir: str,
    model_name: str = "gpt-4o-mini",
    xml_rules_path: Optional[str] = None,
    old_yang_file: Optional[str] = None,
    new_yang_file: Optional[str] = None,
    search_dirs: Optional[list] = None,
) -> Dict[str, Any]:
    """
    Verify an enriched compatibility report using LLM.

    For every line tagged ``<needs-deep-analysis>`` the function:
    - First tries the static ConditionAnalyzer with YANG files (if provided)
      to resolve deterministic cases without LLM.
    - Falls back to ``analyze_condition_change()`` (LLM) for complex cases.
        - Maps the classification to a compatibility decision.
        - Replaces the ``<needs-deep-analysis>`` marker with
            ``<confirmed>`` (LLM agrees) or ``<conflict>`` (LLM disagrees).

    Args:
        enriched_report_path: Path to the enriched report file produced by
            ``check_compatibility.py``.
        output_dir: Directory to save the verified report.
        model_name: LLM model to use for verification (e.g. ``claude-sonnet-4-6``).
        xml_rules_path: Optional path to a custom compatibility_rules.xml.
            When ``None`` the bundled default rules are used (this parameter is
            reserved for future rule-aware verification; currently unused by the
            LLM condition analyser but stored in the return stats for traceability).
        old_yang_file: Optional path to the old YANG file. When provided, the
            static ConditionAnalyzer uses pyang to resolve namespace prefixes
            and import chains before falling back to the LLM.
        new_yang_file: Optional path to the new YANG file.
        search_dirs: Optional list of directories to search for YANG imports.

    Returns:
        Dict with keys ``success``, ``verified_report_path``, ``stats``,
        and optionally ``error`` / ``traceback``.
    """
    import re
    from pathlib import Path

    try:
        with open(enriched_report_path, 'r', encoding='utf-8') as f:
            enriched_lines = f.readlines()

        # Collect lines that carry the marker
        changes_to_verify = [
            (i, line) for i, line in enumerate(enriched_lines)
            if '<needs-deep-analysis>' in line
        ]

        if not changes_to_verify:
            return {
                'success': True,
                'verified_report_path': enriched_report_path,
                'stats': {'total': 0, 'verified': 0},
                'message': 'No changes requiring LLM verification',
            }

        # Configure DSPy once for the whole batch
        try:
            from .api_config import configure_dspy
            if not configure_dspy(model=model_name):
                raise RuntimeError("configure_dspy() returned False — check FUELIX_API_KEY")
        except Exception as cfg_err:
            console.print(f"[red]❌ DSPy configuration failed: {cfg_err}[/red]")
            # Graceful degradation: mark items as <unverified>
            verified_lines = [
                line.replace('<needs-deep-analysis>', '<unverified>')
                for line in enriched_lines
            ]
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            fallback_path = str(
                Path(output_dir) / Path(enriched_report_path).name.replace('.txt', '_verified.txt')
            )
            with open(fallback_path, 'w', encoding='utf-8') as f:
                f.writelines(verified_lines)

            # Avoid stale LLM artifacts from previous runs.
            # When verification cannot execute, publish an explicit empty report.
            llm_json_path = str(Path(output_dir) / 'llm_verification_report.json')
            with open(llm_json_path, 'w', encoding='utf-8') as f:
                import json as _json
                _json.dump({'items': []}, f, indent=2)

            return {
                'success': False,
                'verified_report_path': fallback_path,
                'error': f'DSPy configuration failed: {cfg_err}',
                'stats': {'total': len(changes_to_verify), 'verified': 0},
            }

        # Import the real condition analyzer (configured above)
        from .llm_verification import analyze_condition_change

        verified_count = 0
        confirmed_count = 0
        conflict_count = 0
        verification_details: List[Dict[str, Any]] = []

        # Build a constraint type index from compatibility_rules.xml once per run.
        constraint_type_map = _load_constraint_type_map(xml_rules_path)

        verified_lines = list(enriched_lines)  # mutable copy

        # Track the last non-indented path line seen above any marker
        def _find_path_context(lines: List[str], before_idx: int) -> str:
            for j in range(before_idx - 1, max(0, before_idx - 30), -1):
                check = lines[j].rstrip()
                if check and not check[0].isspace():
                    return check
            return 'unknown'

        console.print(Panel.fit(
            f"[bold cyan]LLM Verification — {len(changes_to_verify)} items[/bold cyan]\n"
            f"Model: {model_name}",
            border_style="cyan",
        ))

        for idx, raw_line in changes_to_verify:
            stripped = raw_line.rstrip('\n')
            current_path = _find_path_context(enriched_lines, idx)

            # Rebuild a logical constraint line by appending continuation lines
            # that belong to the same indented report item. Some reports wrap
            # long XPath expressions across multiple physical lines.
            logical_line = stripped
            next_idx = idx + 1
            while next_idx < len(enriched_lines):
                cont = enriched_lines[next_idx].rstrip('\n')
                if not cont.strip():
                    break
                if not cont[0].isspace():
                    break
                # Stop at the next numbered report item (attribute/constraint/etc.)
                # so we do not accidentally append sibling lines like:
                #   7.1 attribute changed ...
                #   7.2 attribute changed ...
                if _is_report_item_line(cont):
                    break
                logical_line += ' ' + cont.strip()
                next_idx += 1

            # Remove compatibility/status tags before extracting values.
            parse_line = re.sub(
                r'\s*<(?:backward-compatible|non-backward-compatible|conditional-backward-compatible|needs-deep-analysis|confirmed|conflict|conflicted|unconfirmed|llm_error|unverified)>',
                '',
                logical_line,
                flags=re.IGNORECASE,
            )

            # ── Extract old/new values from the constraint-changed line ─────────
            # Canonical format (may be wrapped across two display lines, but stored
            # on ONE logical line in the file):
            #   "     4.1 constraint changed: ['when'] -> NEW (was OLD) <tag> <needs-deep-analysis>"
            old_value = ''
            new_value = ''
            constraint_name = 'when'  # default
            action = 'changed'

            m_name = re.search(r"\['([^']+)'\]", parse_line)
            if m_name:
                constraint_name = m_name.group(1)

            if 'changed' in parse_line.lower():
                action = 'changed'
                # Robust extraction for regex-heavy values:
                # split at first '->' and the LAST ' (was ' to avoid truncation
                # when patterns themselves contain nested parentheses.
                if '->' in parse_line:
                    rhs = parse_line.split('->', 1)[1].strip()
                    new_value, old_value = _extract_changed_old_new(rhs)

            elif 'added' in parse_line.lower():
                action = 'added'
                if '->' in parse_line:
                    new_value = _strip_followup_change_tail(parse_line.split('->', 1)[1].strip())

            elif 'deleted' in parse_line.lower():
                action = 'deleted'
                if '->' in parse_line:
                    old_value = _strip_followup_change_tail(parse_line.split('->', 1)[1].strip())

            # ── Determine the original compatibility decision on this line ───────
            orig_decision = 'non-backward-compatible'
            tag_m = re.search(
                r'<(backward-compatible|non-backward-compatible|conditional-backward-compatible)>',
                stripped, re.IGNORECASE
            )
            if tag_m:
                orig_decision = tag_m.group(1).lower()

            # ── Call the real LLM ────────────────────────────────────────────────
            console.print(
                f"[cyan][{idx+1}][/cyan] Analysing {constraint_name} {action} "
                f"at {current_path[:60]}..."
            )

            static_class = ''
            static_reason = ''
            analysis_context = {
                'yang_path': current_path,
                'constraint': constraint_name,
                'action': action,
                # Pass YANG file paths so the static ConditionAnalyzer
                # can use pyang for prefix/import resolution before LLM.
                # The static findings (lhs_valid, rhs_valid, preliminary
                # direction) are automatically included in static_findings
                # and passed to the LLM for cross-validation.
                'old_yang_file': old_yang_file,
                'new_yang_file': new_yang_file,
                'search_dirs': search_dirs,
            }

            try:
                # analyze_condition_change returns a 4-tuple:
                #   (classification, explanation, confidence, outcome)
                # where outcome is the LLM's own verdict on whether it
                # CONFIRMS or DISAGREES with the static tool's findings.
                classification, explanation, confidence, semantic_outcome = analyze_condition_change(
                    old_condition=old_value or '(none)',
                    new_condition=new_value or '(none)',
                    context=analysis_context,
                    dspy_model=None,   # already configured globally above
                    # force_llm=True: always call the LLM as a second-layer check.
                    # The static ConditionAnalyzer runs first and its findings are
                    # passed to the LLM via static_findings so the LLM can
                    # cross-check the deterministic conclusion.  This is the
                    # intended behaviour when --llm-verify is active.
                    force_llm=True,
                )

                static_findings = analysis_context.get('static_findings', '') or ''
                static_m = re.search(r'preliminary_direction=(\S+)', static_findings)
                if static_m:
                    static_class = static_m.group(1).strip().lower()
                reason_m = re.search(r'preliminary_explanation=(.+)', static_findings)
                if reason_m:
                    static_reason = reason_m.group(1).strip()

                # For regex constraints, use deterministic FSM comparison as
                # authoritative static semantic class (dialect-aware).
                regex_static_class, regex_policy_note = _compute_regex_static_class_from_rules(
                    constraint_name=constraint_name,
                    old_value=old_value,
                    new_value=new_value,
                    constraint_type_map=constraint_type_map,
                )
                if regex_static_class:
                    static_class = regex_static_class
                if regex_policy_note:
                    static_reason = (
                        f"{static_reason}; {regex_policy_note}"
                        if static_reason else regex_policy_note
                    )
            except Exception as llm_err:
                # The LLM call itself failed (e.g. auth error, network error).
                # We must NOT silently fall through to 'incomparable → confirmed'
                # because that would produce false confirmations in the report.
                # Instead mark as 'llm_error' so callers can distinguish this
                # from a genuine LLM-confirmed result.
                console.print(
                    f"[red]⚠️  LLM call FAILED for item {idx+1} — not a confirmation![/red]\n"
                    f"   Error: {llm_err}"
                )
                classification = 'llm_error'
                explanation = str(llm_err)
                confidence = 'low'
                semantic_outcome = 'unconfirmed'

            # Static policy is authoritative for known semantic classes.
            classification, explanation, static_overrode_llm = _apply_static_policy_override(
                llm_classification=classification,
                static_class=static_class,
                llm_explanation=explanation,
            )

            # Map LLM classification → compatibility decision
            # narrowed     → non-backward-compatible (more restrictive = breaking)
            # relaxed      → backward-compatible     (less restrictive = safe)
            # equivalent   → backward-compatible
            # incomparable → keep original (conservative)
            # llm_error    → keep original (conservative), outcome = conflict
            if classification == 'llm_error':
                llm_compat = orig_decision   # conservative fallback
            else:
                llm_compat = {
                    'narrowed':     'non-backward-compatible',
                    'relaxed':      'backward-compatible',
                    'equivalent':   'backward-compatible',
                    'incomparable': orig_decision,   # conservative: keep original
                }.get(classification, orig_decision)

                # Conservative policy for regex/pattern constraints:
                # if semantic relation is incomparable, treat it as breaking.
                if _is_regex_constraint(constraint_name, constraint_type_map) and classification == 'incomparable':
                    llm_compat = 'non-backward-compatible'

            outcome = _resolve_verification_outcome(
                classification=classification,
                static_class=static_class,
                semantic_outcome=semantic_outcome,
                orig_decision=orig_decision,
                llm_compat=llm_compat,
            )

            console.print(
                f"  → static_tool_decision={static_class or 'n/a'} "
                f"reason={static_reason or 'n/a'}"
            )

            console.print(
                f"  → classification=[bold]{classification}[/bold]  "
                f"static_class={static_class or 'n/a'}  "
                f"static_compat={orig_decision}  "
                f"llm_compat={llm_compat}  outcome=[bold]{outcome}[/bold]  "
                f"confidence={confidence}"
            )

            # ── Patch the line in place ──────────────────────────────────────────
            # Step 1: replace <needs-deep-analysis> with the verification outcome tag
            # (e.g. <confirmed> or <conflict>).
            new_line = re.sub(
                r'<needs-deep-analysis>',
                f'<{outcome}>',
                raw_line,
                flags=re.IGNORECASE,
            )
            # Step 2: when the LLM/static tool reclassified the item (llm_compat
            # differs from orig_decision), also update the compatibility tag on the
            # line so that downstream scripts (generate_non_compatibility_list.py,
            # generate_compatibility_list.py) route the item to the correct bucket.
            # Example: orig_decision='non-backward-compatible', llm_compat='backward-compatible'
            # → replace <non-backward-compatible> with <backward-compatible> on the line.
            # Retag primary compatibility when verification confirmed, OR when
            # the static analyzer deterministically flagged the regex pair as
            # unparsed/failed to analyze. Treat parser failures as NBC by
            # default to avoid conservative BC fallbacks for unparsed regexes.
            if (outcome == 'confirmed' or (static_reason and 'unparsed' in static_reason.lower())) and llm_compat != orig_decision:
                # Map llm_compat to the canonical tag string used in the report
                _compat_tag_map = {
                    'backward-compatible': 'backward-compatible',
                    'non-backward-compatible': 'non-backward-compatible',
                    'conditional-backward-compatible': 'conditional-backward-compatible',
                }
                new_tag = _compat_tag_map.get(llm_compat)
                old_tag = _compat_tag_map.get(orig_decision)
                if new_tag and old_tag and new_tag != old_tag:
                    new_line = re.sub(
                        re.escape(f'<{old_tag}>'),
                        f'<{new_tag}>',
                        new_line,
                        flags=re.IGNORECASE,
                    )
            verified_lines[idx] = new_line
            verified_count += 1

            if outcome == 'confirmed':
                confirmed_count += 1
            else:
                conflict_count += 1

            verification_details.append({
                'line_number': idx + 1,
                'path': current_path,
                'constraint': constraint_name,
                'action': action,
                'old_value': old_value,
                'new_value': new_value,
                'orig_decision': orig_decision,
                'static_tool_decision': static_class or None,
                'static_tool_reason': static_reason or None,
                'llm_classification': classification,
                'llm_compat': llm_compat,
                'static_overrode_llm': static_overrode_llm,
                'outcome': outcome,
                'confidence': confidence,
                'explanation': explanation,
            })

        # ── Write output ─────────────────────────────────────────────────────────
        import json as _json
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        verified_report_path = str(
            Path(output_dir) / Path(enriched_report_path).name.replace('.txt', '_verified.txt')
        )
        with open(verified_report_path, 'w', encoding='utf-8') as f:
            f.writelines(verified_lines)

        # Write llm_verification_report.json so generate_non_compatibility_list.py
        # can populate the llm_assistance_decision field in final_report.json.
        # Format: { "items": [ { "change": {...}, "verification": {...} }, ... ] }
        llm_report_items = []
        for d in verification_details:
            llm_report_items.append({
                'change': {
                    'yang_path': d['path'],
                    'keyword_or_constraint': d['constraint'],
                    'action': d['action'],
                    'old_value': d.get('old_value'),
                    'new_value': d.get('new_value'),
                },
                'verification': {
                    'verified_compatibility': d['outcome'],   # 'confirmed' or 'conflict'
                    'static_tool_decision': d.get('static_tool_decision'),
                    'static_tool_reason': d.get('static_tool_reason'),
                    'llm_classification': d.get('llm_classification'),
                    'static_overrode_llm': d.get('static_overrode_llm'),
                    'confidence': d.get('confidence'),
                    'explanation': d.get('explanation'),
                },
            })
        llm_json_path = str(Path(output_dir) / 'llm_verification_report.json')
        with open(llm_json_path, 'w', encoding='utf-8') as f:
            _json.dump({'items': llm_report_items}, f, indent=2)

        error_count = sum(1 for d in verification_details if d['llm_classification'] == 'llm_error')

        panel_color = "green" if error_count == 0 else "yellow"
        panel_icon = "✅" if error_count == 0 else "⚠️ "
        console.print(Panel(
            f"[{panel_color}]{panel_icon} LLM Verification Complete[/{panel_color}]\n\n"
            f"Total items analysed:          {verified_count}\n"
            f"Confirmed (LLM agrees):        {confirmed_count}\n"
            f"Conflict (LLM disagrees):      {conflict_count}\n"
            f"LLM errors (call failed):      {error_count}",
            title="🤖 LLM Verification Results",
            border_style=panel_color,
        ))

        return {
            'success': True,
            'verified_report_path': verified_report_path,
            'xml_rules_path': xml_rules_path,
            'stats': {
                'total': len(changes_to_verify),
                'verified': verified_count,
                'confirmed': confirmed_count,
                'conflicts': conflict_count,
                'llm_errors': error_count,
            },
            'verification_details': verification_details,
        }

    except Exception as e:
        import traceback as _tb
        return {
            'success': False,
            'error': str(e),
            'traceback': _tb.format_exc(),
        }
