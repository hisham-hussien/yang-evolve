"""
LLM Verification and Analysis Layer for Compatibility Rules
------------------------------------------------------------
Unified DSPy-based module for:
1. Verifying compatibility decisions (assistance="true" rules)
2. Analyzing complex condition changes (condition analyzer integration)
3. Batch processing of reports with markers

Signatures and Modules are defined in their canonical files:
  - yang_rag/dspy/signatures.py  → VerifyCompatibilityDecision, AnalyzeConditionChange
  - yang_rag/dspy/modules.py     → CompatibilityVerifier, ConditionAnalyzer

This module consolidates the higher-level functions that use those classes.
"""

import dspy
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .signatures import (
    VerifyCompatibilityDecision, AnalyzeConditionChange,
    JudgeExtensionCompatibility, JudgeMappingQuality, JudgeRuleCloneReadiness,
)
from .modules import CompatibilityVerifier, ConditionAnalyzer

console = Console()


def needs_llm_verification(rule: Dict[str, Any], element_name: Optional[str] = None) -> bool:
    """Check if a rule requires LLM verification.
    
    Args:
        rule: The rule dictionary from parse_rules()
        element_name: Optional specific element name to check (keyword, attribute, or constraint)
        
    Returns:
        True if the rule (or specific element) has assistance="true"
    """
    if not rule:
        return False
    
    # Check keywords for assistance="true"
    for keyword in rule.get('keywords', []):
        if isinstance(keyword, dict):
            # If checking specific element, filter by name
            if element_name and keyword.get('name') != element_name:
                continue
            
            # Check if assistance="true"
            if keyword.get('assistance') == 'true':
                return True
    
    # Check attributes for assistance="true"
    for attribute in rule.get('attributes', []):
        if isinstance(attribute, dict):
            # If checking specific element, filter by name
            if element_name and attribute.get('name') != element_name:
                continue
            
            # Check if assistance="true"
            if attribute.get('assistance') == 'true':
                return True
    
    # Check constraints for assistance="true"
    for constraint in rule.get('constraints', []):
        if isinstance(constraint, dict):
            # If checking specific element, filter by name
            if element_name and constraint.get('name') != element_name:
                continue
            
            # Check if assistance="true"
            if constraint.get('assistance') == 'true':
                return True
    
    return False


def verify_compatibility_change(
    change_info: Dict[str, Any],
    rule: Dict[str, Any],
    llm_model: str = "gpt-4o-mini"
) -> Optional[Dict[str, Any]]:
    """Verify a compatibility change using LLM.
    
    Args:
        change_info: Dictionary with change details:
            - rule_id: str
            - keyword_or_constraint: str
            - action: str
            - original_decision: str
            - old_value: str (optional)
            - new_value: str (optional)
            - yang_path: str (optional)
            - parent_context: str (optional)
        rule: The rule dictionary from parse_rules()
        llm_model: LLM model to use for verification
        
    Returns:
        Dictionary with verification results, or None if LLM verification not needed
    """
    
    # Check if this rule needs LLM verification
    element_name = change_info.get('keyword_or_constraint')
    if not needs_llm_verification(rule, element_name):
        return None
    
    console.print(f"\n[yellow]🤖 LLM Verification:[/yellow] Checking {element_name}...")
    
    try:
        # Configure DSPy with the specified model
        # Try relative import first (when running as module)
        try:
            from .api_config import configure_dspy, FUELIX_API_BASE
        except ImportError:
            # Fall back to absolute import (when running from root)
            from yang_rag.dspy.api_config import configure_dspy, FUELIX_API_BASE
        
        if not configure_dspy(model=llm_model, api_base=FUELIX_API_BASE):
            console.print("[red]❌ Failed to configure DSPy for LLM verification[/red]")
            return None
        
        # Extract element type and relaxed flag from rule
        # Check keywords, attributes, and constraints
        element_type = "N/A"
        is_relaxed = False
        
        # Check keywords
        for keyword in rule.get('keywords', []):
            if isinstance(keyword, dict) and keyword.get('name') == element_name:
                element_type = 'keyword'
                is_relaxed = keyword.get('relaxed', 'false').lower() == 'true'
                break
        
        # Check attributes
        if element_type == "N/A":
            for attribute in rule.get('attributes', []):
                if isinstance(attribute, dict) and attribute.get('name') == element_name:
                    element_type = attribute.get('type', 'attribute')
                    is_relaxed = attribute.get('relaxed', 'false').lower() == 'true'
                    break
        
        # Check constraints
        if element_type == "N/A":
            for constraint in rule.get('constraints', []):
                if isinstance(constraint, dict) and constraint.get('name') == element_name:
                    element_type = constraint.get('type', 'N/A')
                    is_relaxed = constraint.get('relaxed', 'false').lower() == 'true'
                    break
        
        # Create verifier and run verification
        verifier = CompatibilityVerifier()
        
        result = verifier(
            rule_id=change_info.get('rule_id', ''),
            keyword_or_constraint=element_name or '',
            action=change_info.get('action', ''),
            original_decision=change_info.get('original_decision', ''),
            old_value=change_info.get('old_value', ''),
            new_value=change_info.get('new_value', ''),
            yang_path=change_info.get('yang_path', ''),
            parent_context=change_info.get('parent_context', ''),
            constraint_type=element_type,
            is_relaxed=is_relaxed
        )
        
        # Package results
        verification = {
            'verification_result': result.verification_result,
            'confidence': float(result.confidence),
            'verified_compatibility': result.verified_compatibility,
            'reasoning': result.reasoning,
            'suggested_action': result.suggested_action,
            'original_decision': change_info.get('original_decision', ''),
            'llm_model': llm_model
        }
        
        # Display results
        _display_verification_results(change_info, verification)
        
        return verification
        
    except Exception as e:
        console.print(f"[red]❌ LLM Verification failed: {e}[/red]")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
        return None


def _display_verification_results(change_info: Dict[str, Any], verification: Dict[str, Any]):
    """Display verification results in a nice format."""
    
    # Color based on result
    result = verification['verification_result']
    if result == 'CONFIRMED':
        color = 'green'
        icon = '✓'
    elif result == 'OVERRIDDEN':
        color = 'red'
        icon = '⚠'
    else:  # UNCERTAIN
        color = 'yellow'
        icon = '?'
    
    # Create results table
    table = Table(title=f"{icon} LLM Verification Results", show_header=True, header_style="bold magenta")
    table.add_column("Field", style="cyan")
    table.add_column("Value", style=color)
    
    table.add_row("Constraint", change_info.get('keyword_or_constraint', 'N/A'))
    table.add_row("Action", change_info.get('action', 'N/A'))
    table.add_row("Original Decision", verification['original_decision'])
    table.add_row("Verification Result", verification['verification_result'])
    table.add_row("Verified Compatibility", verification['verified_compatibility'])
    table.add_row("Confidence", f"{verification['confidence']:.1%}")
    table.add_row("Suggested Action", verification['suggested_action'])
    
    console.print(table)
    
    # Display reasoning in a panel
    console.print(Panel.fit(
        f"[bold]Reasoning:[/bold]\n\n{verification['reasoning']}",
        border_style=color,
        title="LLM Analysis"
    ))
    
    # Warning if overridden
    if result == 'OVERRIDDEN':
        console.print(Panel.fit(
            f"[bold red]⚠ WARNING:[/bold red]\n\n"
            f"LLM disagrees with original decision!\n"
            f"Original: {verification['original_decision']}\n"
            f"LLM Says: {verification['verified_compatibility']}\n\n"
            f"Suggested action: {verification['suggested_action']}",
            border_style="red"
        ))


def batch_verify_changes(
    changes: List[Dict[str, Any]],
    rules: List[Dict[str, Any]],
    llm_model: str = "gpt-4o-mini"
) -> List[Dict[str, Any]]:
    """Verify multiple changes in batch.
    
    Args:
        changes: List of change dictionaries (same format as verify_compatibility_change)
        rules: List of all rules from parse_rules()
        llm_model: LLM model to use
        
    Returns:
        List of verification results (only for changes that needed verification)
    """
    
    verifications = []
    
    console.print(Panel.fit(
        f"[bold cyan]LLM Batch Verification[/bold cyan]\n\n"
        f"Checking {len(changes)} changes for assistance='true' rules...",
        border_style="cyan"
    ))
    
    for i, change in enumerate(changes, 1):
        # Prefer explicit rule_id if present
        rule_id = change.get('rule_id')
        matching_rule = next((r for r in rules if r.get('rule_id') == rule_id), None) if rule_id else None

        # If rule_id is not available or not found, infer a suitable rule based on element name and decision
        if not matching_rule:
            element_name = change.get('keyword_or_constraint')
            decision = change.get('original_decision')
            action = change.get('action')

            # Candidate rules that include the element (keyword/attribute/constraint) with assistance="true"
            candidates = []
            for r in rules:
                # Check all element types (keywords, attributes, constraints)
                has_element_with_assistance = False
                
                # Check keywords
                for kw in (r.get('keywords') or []):
                    if isinstance(kw, dict) and kw.get('name') == element_name and kw.get('assistance') == 'true':
                        has_element_with_assistance = True
                        break
                
                # Check attributes
                if not has_element_with_assistance:
                    for attr in (r.get('attributes') or []):
                        if isinstance(attr, dict) and attr.get('name') == element_name and attr.get('assistance') == 'true':
                            has_element_with_assistance = True
                            break
                
                # Check constraints
                if not has_element_with_assistance:
                    for cons in (r.get('constraints') or []):
                        if isinstance(cons, dict) and cons.get('name') == element_name and cons.get('assistance') == 'true':
                            has_element_with_assistance = True
                            break
                
                if has_element_with_assistance:
                    # Match compatible class if possible
                    comp = r.get('compatible')
                    if comp == decision:
                        candidates.append(r)
            
            # Fallback: any rule containing element name with assistance="true"
            if not candidates:
                for r in rules:
                    for kw in (r.get('keywords') or []):
                        if isinstance(kw, dict) and kw.get('name') == element_name and kw.get('assistance') == 'true':
                            candidates.append(r)
                            break
                    for attr in (r.get('attributes') or []):
                        if isinstance(attr, dict) and attr.get('name') == element_name and attr.get('assistance') == 'true':
                            candidates.append(r)
                            break
                    for cons in (r.get('constraints') or []):
                        if isinstance(cons, dict) and cons.get('name') == element_name and cons.get('assistance') == 'true':
                            candidates.append(r)
                            break

            matching_rule = candidates[0] if candidates else None

        if not matching_rule:
            # No applicable assistance-enabled rule found; skip
            continue

        # Verify if needed (verify_compatibility_change will skip if assistance != true)
        result = verify_compatibility_change(change, matching_rule, llm_model)

        if result:
            verifications.append({
                'change': change,
                'verification': result
            })
    
    # Summary
    console.print(f"\n[green]✓[/green] Verified {len(verifications)} changes requiring LLM assistance")
    
    return verifications


def analyze_condition_change(
    old_condition: str,
    new_condition: str,
    context: Optional[Dict[str, Any]] = None,
    dspy_model: Optional[str] = None,
    force_llm: bool = False,
) -> Tuple[str, str, str]:
    """Analyze a complex condition change using DSPy.
    
    This is the main entry point for condition analysis when rule-based
    analysis returns NEEDS_LLM_ANALYSIS.
    
    Args:
        old_condition: Original condition
        new_condition: New condition
        context: Optional context information
        dspy_model: DSPy model to use (if None, uses configured default)
        force_llm: When True, always call the LLM even if the static analyzer
                   resolves the case deterministically.  Use this when the caller
                   wants the LLM as a mandatory second-layer verification (e.g.
                   when --llm-verify is active).  The static analyzer's findings
                   are still passed to the LLM via static_findings so the LLM
                   can cross-check the deterministic conclusion.
        
    Returns:
        Tuple of (classification, explanation, confidence)
        where classification is one of: 'narrowed', 'relaxed', 'equivalent', 'incomparable'
    """
    try:
        def _format_static_findings(details: Dict[str, Any], direction: Any, explanation: str) -> str:
            """Serialize static analyzer evidence into a concise, LLM-friendly text block."""
            if not details:
                details = {}

            lines: List[str] = []
            lines.append("STATIC_ANALYZER_DOUBLE_CHECK")
            lines.append(f"preliminary_direction={getattr(direction, 'value', str(direction))}")
            lines.append(f"preliminary_explanation={explanation}")

            mixed = details.get('mixed_equality') or {}
            if mixed:
                lines.append(
                    "lhs_rhs="
                    f"old_lhs={mixed.get('old_lhs')} ; old_rhs={mixed.get('old_rhs')} ; "
                    f"new_lhs={mixed.get('new_lhs')} ; new_rhs={mixed.get('new_rhs')}"
                )
                lines.append(
                    "lhs_rhs_validity="
                    f"old_lhs_valid={mixed.get('old_lhs_valid')} ; old_rhs_valid={mixed.get('old_rhs_valid')} ; "
                    f"new_lhs_valid={mixed.get('new_lhs_valid')} ; new_rhs_valid={mixed.get('new_rhs_valid')} ; "
                    f"old_overall={mixed.get('old_overall_valid')} ; new_overall={mixed.get('new_overall_valid')}"
                )
                lines.append(
                    "lhs_rhs_reasons="
                    f"old_rhs_reason={mixed.get('old_rhs_reason')} ; "
                    f"new_rhs_reason={mixed.get('new_rhs_reason')}"
                )

            path_cmp = details.get('path_compare') or {}
            if path_cmp:
                lines.append(
                    "path_resolution="
                    f"old_lhs={path_cmp.get('old_path')} ; old_success={path_cmp.get('old_success')} ; old_abs={path_cmp.get('old_abs')} ; old_error={path_cmp.get('old_error')} ; "
                    f"new_lhs={path_cmp.get('new_path')} ; new_success={path_cmp.get('new_success')} ; new_abs={path_cmp.get('new_abs')} ; new_error={path_cmp.get('new_error')}"
                )
                lines.append(
                    "path_resolution_context="
                    f"old_context={path_cmp.get('old_context_used')} ; old_fallback={path_cmp.get('old_fallback_used')} ; "
                    f"new_context={path_cmp.get('new_context_used')} ; new_fallback={path_cmp.get('new_fallback_used')}"
                )
                if 'resolution_status' in path_cmp:
                    lines.append(
                        f"path_resolution_status={path_cmp.get('resolution_status')} ; same_target={path_cmp.get('same_target')}"
                    )

            lines.append(
                "wrapper_rules=Apply RFC7950 when/must wrapper rules: "
                "(1) resolve from effective attachment context, "
                "(2) treat wrapper-only relative rewrites as equivalent when absolute targets match, "
                "(3) if wrapper-aware resolution is inconclusive/different targets and no deterministic validity transition exists, prefer incomparable"
            )

            lines.append(
                "instruction=Double-check whether your final classification agrees with the static evidence above. "
                "If you disagree, explicitly explain why."
            )
            return "\n".join(lines)

        # ── Pre-LLM: try static ConditionAnalyzer with YANG files ──────────────
        # If the context contains YANG file paths, run the static analyzer first.
        # This avoids unnecessary LLM calls for cases the static engine can resolve
        # deterministically (e.g. namespace prefix renames, import chain shortcuts).
        ctx = context or {}
        old_yang_file = ctx.get('old_yang_file')
        new_yang_file = ctx.get('new_yang_file')
        context_path = ctx.get('yang_path') or ctx.get('context_path')
        search_dirs = ctx.get('search_dirs')

        if old_yang_file and new_yang_file:
            try:
                try:
                    from yang_rag.comparator.helper.condition_analyzer import (
                        ConditionAnalyzer as StaticConditionAnalyzer,
                        ChangeDirection
                    )
                except ImportError:
                    from ..comparator.helper.condition_analyzer import (
                        ConditionAnalyzer as StaticConditionAnalyzer,
                        ChangeDirection
                    )
                # Load pyang broken-line data so the static analyzer has the same
                # pyang override information as the main comparison pipeline.
                # Without this, the pyang override (which sets new_lhs_valid=True
                # when pyang found no XPATH error for the new condition) is skipped,
                # causing the static findings passed to the LLM to have wrong
                # validity data (e.g. new_lhs_valid=False instead of True).
                old_pyang_broken = None
                new_pyang_broken = None
                try:
                    from yang_rag.comparator.helper.pyang_utils import get_pyang_xpath_broken_lines
                    old_pyang_broken = get_pyang_xpath_broken_lines(
                        old_yang_file, search_dirs
                    )
                    new_pyang_broken = get_pyang_xpath_broken_lines(
                        new_yang_file, search_dirs
                    )
                except Exception:
                    pass  # pyang not available — proceed without broken-line data

                static_analyzer = StaticConditionAnalyzer(
                    enable_xpath_resolution=True,
                    old_yang_file=old_yang_file,
                    new_yang_file=new_yang_file,
                    context_path=context_path,
                    search_dirs=search_dirs,
                    old_pyang_broken_lines=old_pyang_broken,
                    new_pyang_broken_lines=new_pyang_broken,
                )
                direction, explanation = static_analyzer.analyze_change(
                    old_condition, new_condition
                )
                static_details = static_analyzer.get_last_analysis_details()
                ctx['static_findings'] = _format_static_findings(
                    static_details,
                    direction,
                    explanation,
                )
                # If the static engine resolved it definitively AND force_llm is False,
                # return without calling the LLM.  The static result is trusted.
                # When force_llm=True, fall through so the LLM validates the static result.
                if direction != ChangeDirection.NEEDS_LLM_ANALYSIS and not force_llm:
                    direction_map = {
                        ChangeDirection.NARROWED: 'narrowed',
                        ChangeDirection.RELAXED: 'relaxed',
                        ChangeDirection.EQUIVALENT: 'equivalent',
                        ChangeDirection.INCOMPARABLE: 'incomparable',
                    }
                    cls = direction_map.get(direction, 'incomparable')
                    # No LLM call — outcome is implicitly 'confirmed' (static engine resolved)
                    return cls, explanation, 'high', 'confirmed'
            except Exception:
                pass  # Fall through to LLM if static analysis fails

        # Configure DSPy if model specified
        if dspy_model:
            from .api_config import configure_dspy, FUELIX_API_BASE
            if not configure_dspy(model=dspy_model):
                return _fallback_condition_analysis(old_condition, new_condition)
        
        # Create analyzer and run.
        # The LLM receives static_findings (via ctx) and is asked to:
        #   1. Classify the condition change independently
        #   2. Validate the static tool's preliminary_direction
        #   3. Output 'outcome': confirmed | unconfirmed
        analyzer = ConditionAnalyzer()
        result = analyzer(
            old_condition=old_condition,
            new_condition=new_condition,
            context=ctx
        )
        
        # Normalize classification
        classification = result.classification.lower().strip()
        if classification not in ['narrowed', 'relaxed', 'equivalent', 'incomparable']:
            # Try to extract from response
            if 'narrow' in classification:
                classification = 'narrowed'
            elif 'relax' in classification:
                classification = 'relaxed'
            elif 'equiv' in classification:
                classification = 'equivalent'
            else:
                classification = 'incomparable'

        # Extract outcome (confirmed / unconfirmed)
        raw_outcome = getattr(result, 'outcome', '') or ''
        outcome = raw_outcome.lower().strip()
        if outcome not in ('confirmed', 'unconfirmed'):
            # Derive from whether LLM classification matches static preliminary direction
            static_dir = ''
            if ctx.get('static_findings'):
                import re as _re_out
                m = _re_out.search(r'preliminary_direction=(\S+)', ctx['static_findings'])
                if m:
                    static_dir = m.group(1).lower()
            llm_cls_map = {
                'narrowed': 'narrowed',
                'relaxed': 'relaxed',
                'equivalent': 'equivalent',
                'incomparable': 'incomparable',
            }
            if static_dir and static_dir in llm_cls_map:
                outcome = 'confirmed' if classification == static_dir else 'unconfirmed'
            else:
                outcome = 'confirmed'  # No static direction to compare against

        return (
            classification,
            result.explanation,
            result.confidence.lower().strip(),
            outcome,
        )
        
    except Exception as e:
        console.print(f"[yellow]⚠️  DSPy condition analysis error: {e}[/yellow]")
        return _fallback_condition_analysis(old_condition, new_condition)


def _fallback_condition_analysis(
    old_condition: str,
    new_condition: str
) -> Tuple[str, str, str, str]:
    """Fallback analysis when DSPy fails.

    Returns conservative classification to avoid false positives.
    Returns a 4-tuple: (classification, explanation, confidence, outcome).
    """
    return (
        'incomparable',
        f"Complex condition change requires manual review: '{old_condition}' → '{new_condition}'",
        'low',
        'unconfirmed',
    )


def judge_mapping_quality(
    extension_keyword: str,
    extension_snippet: str,
    matched_keyword: str,
    matched_snippet: str,
    model: str = "claude-sonnet-4-6",
    api_base: Optional[str] = None,
) -> Dict[str, Any]:
    """Use the LLM to independently judge whether the RAG-selected keyword is a good
    structural and semantic match for the unknown vendor extension keyword.

    Args:
        extension_keyword: The unknown vendor extension keyword name.
        extension_snippet: Example YANG usage snippet for the extension.
        matched_keyword: The known YANG keyword selected by the RAG system.
        matched_snippet: Example usage snippet of the matched keyword from the corpus.
        model: LLM model to use.
        api_base: Optional API base URL override.

    Returns:
        Dictionary with keys:
            verdict   : 'agree' | 'disagree'
            reasoning : str explanation
            llm_map_judge : 1 if 'agree', 0 if 'disagree'
    """
    try:
        try:
            from .api_config import configure_dspy, FUELIX_API_BASE
        except ImportError:
            from yang_rag.dspy.api_config import configure_dspy, FUELIX_API_BASE

        target_base = api_base or FUELIX_API_BASE
        if not configure_dspy(model=model, api_base=target_base):
            console.print("[yellow]⚠️  LLM mapping judge unavailable — API key not set[/yellow]")
            return {"verdict": "disagree", "reasoning": "LLM unavailable.", "llm_map_judge": 0}

        predictor = dspy.Predict(JudgeMappingQuality)
        result = predictor(
            extension_keyword=extension_keyword,
            extension_snippet=extension_snippet,
            matched_keyword=matched_keyword,
            matched_snippet=matched_snippet,
        )

        raw = (result.verdict or "").lower().strip()
        # Check "disagree" before "agree": "disagree" contains "agree" as substring
        if "disagree" in raw or "not agree" in raw or "don't agree" in raw:
            verdict = "disagree"
        elif "agree" in raw:
            verdict = "agree"
        else:
            verdict = "disagree"  # conservative default
        llm_map_judge = 1 if verdict == "agree" else 0

        color = "green" if verdict == "agree" else "red"
        icon = "✓" if verdict == "agree" else "✗"
        console.print(Panel.fit(
            f"[bold]LLM Mapping Judge:[/bold] [{color}]{icon} {verdict.upper()}[/{color}]\n\n"
            f"{result.reasoning}",
            border_style=color,
            title=f"[bold]Mapping: {extension_keyword} → {matched_keyword}[/bold]",
        ))

        return {"verdict": verdict, "reasoning": result.reasoning, "llm_map_judge": llm_map_judge}

    except Exception as e:
        console.print(f"[yellow]⚠️  LLM mapping judge failed: {e}[/yellow]")
        return {"verdict": "disagree", "reasoning": str(e), "llm_map_judge": 0}


def _fetch_xml_rule_for_keyword(matched_keyword: str, xml_path: Optional[str] = None) -> str:
    """Fetch the actual XML rule snippet for a matched keyword from compatibility_rules.xml.

    Returns a compact XML string showing the rule(s) that apply to the matched keyword,
    including all attributes (type, relaxed, separator, parent, symbolic, etc.).
    Falls back to a generic description if the XML cannot be parsed.
    """
    try:
        import xml.etree.ElementTree as ET
        from pathlib import Path as _Path
        if xml_path:
            xml_file = _Path(xml_path)
        else:
            # Try relative paths
            for candidate in [
                'yang_rag/comparator/compatibility_rules.xml',
                '../yang_rag/comparator/compatibility_rules.xml',
            ]:
                p = _Path(candidate)
                if p.exists():
                    xml_file = p
                    break
            else:
                return f"(XML rules file not found; matched keyword: {matched_keyword})"

        tree = ET.parse(str(xml_file))
        root = tree.getroot()
        matching_rules = []
        for rule in root.findall('rule'):
            # Check all element types for the matched keyword
            found = False
            for tag in ('structural', 'attribute', 'constraint'):
                for elem in rule.iter(tag):
                    if elem.text and elem.text.strip() == matched_keyword:
                        found = True
                        break
                if found:
                    break
            if found:
                matching_rules.append(ET.tostring(rule, encoding='unicode'))

        if matching_rules:
            return '\n'.join(matching_rules)  # return ALL matching rules (no limit)
        return f"(No rule found for '{matched_keyword}' in XML rules)"
    except Exception as e:
        return f"(Could not fetch XML rule: {e})"


def judge_rule_clone_readiness(
    extension_keyword: str,
    extension_snippet: str,
    matched_keyword: str,
    matched_rules_summary: str,
    user_confidence: int,
    model: str = "claude-sonnet-4-6",
    api_base: Optional[str] = None,
    xml_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Use the LLM to independently judge whether the XML compatibility rule of the matched
    keyword can be cloned and applied to the extension keyword by name substitution only,
    with no other changes to the rule's XML attributes.

    Args:
        extension_keyword: The unknown vendor extension keyword name.
        extension_snippet: Example YANG usage snippet for the extension.
        matched_keyword: The known YANG keyword whose rule is being cloned.
        matched_rules_summary: Brief summary (used as fallback if XML fetch fails).
        user_confidence: 1 = user believes rule can be cloned as-is; 0 = needs modification.
        model: LLM model to use.
        api_base: Optional API base URL override.
        xml_path: Optional path to compatibility_rules.xml.

    Returns:
        Dictionary with keys:
            verdict          : 'agree' | 'disagree'
            reasoning        : str explanation
            user_conf        : int (echo of user_confidence)
            llm_clone_judge  : 1 if 'agree' (clone as-is), 0 if 'disagree' (needs modification)
            user_llm_agree   : 1 if user and LLM agree, 0 if they disagree
    """
    # Fetch the actual XML rule for the matched keyword
    matched_xml_rule = _fetch_xml_rule_for_keyword(matched_keyword, xml_path)
    if "(No rule found" in matched_xml_rule or "(Could not fetch" in matched_xml_rule:
        # Fall back to summary if XML fetch failed
        matched_xml_rule = matched_rules_summary

    try:
        try:
            from .api_config import configure_dspy, FUELIX_API_BASE
        except ImportError:
            from yang_rag.dspy.api_config import configure_dspy, FUELIX_API_BASE

        target_base = api_base or FUELIX_API_BASE
        if not configure_dspy(model=model, api_base=target_base):
            console.print("[yellow]⚠️  LLM rule-clone judge unavailable — API key not set[/yellow]")
            return {
                "verdict": "disagree", "reasoning": "LLM unavailable.",
                "user_conf": user_confidence, "llm_clone_judge": 0, "user_llm_agree": 0,
            }

        predictor = dspy.Predict(JudgeRuleCloneReadiness)
        result = predictor(
            extension_keyword=extension_keyword,
            extension_snippet=extension_snippet,
            matched_keyword=matched_keyword,
            matched_xml_rule=matched_xml_rule,
            user_confidence=user_confidence,
        )

        raw = (result.verdict or "").lower().strip()
        # New vocabulary: 'clone-as-is' or 'needs-modification'
        # Also handle legacy 'agree'/'disagree' for backward compatibility
        if "needs-modification" in raw or "needs modification" in raw or "disagree" in raw:
            verdict = "needs-modification"
            llm_clone_judge = 0
        elif "clone-as-is" in raw or "clone as-is" in raw or "clone as is" in raw or "agree" in raw:
            verdict = "clone-as-is"
            llm_clone_judge = 1
        else:
            verdict = "needs-modification"  # conservative default
            llm_clone_judge = 0
        # user_conf=1 means "clone as-is" → maps to llm_clone_judge=1 for agreement
        user_llm_agree = 1 if (user_confidence == llm_clone_judge) else 0

        color = "green" if verdict == "clone-as-is" else "yellow"
        icon = "✓" if verdict == "clone-as-is" else "?"
        user_label = "clone as-is (1)" if user_confidence == 1 else "needs modification (0)"
        llm_label = "clone-as-is" if llm_clone_judge == 1 else "needs-modification"
        agree_label = "agrees" if user_llm_agree else "disagrees"
        console.print(Panel.fit(
            f"[bold]LLM Rule-Clone Judge:[/bold] [{color}]{icon} {verdict.upper()}[/{color}]\n"
            f"User said: {user_label} | LLM: {llm_label} | User↔LLM: {agree_label}\n\n"
            f"{result.reasoning}",
            border_style=color,
            title=f"[bold]Rule Clone: {extension_keyword} ← {matched_keyword}[/bold]",
        ))

        return {
            "verdict": verdict,
            "reasoning": result.reasoning,
            "user_conf": user_confidence,
            "llm_clone_judge": llm_clone_judge,
            "user_llm_agree": user_llm_agree,
        }

    except Exception as e:
        console.print(f"[yellow]⚠️  LLM rule-clone judge failed: {e}[/yellow]")
        return {
            "verdict": "disagree", "reasoning": str(e),
            "user_conf": user_confidence, "llm_clone_judge": 0, "user_llm_agree": 0,
        }


def judge_extension_compatibility(
    extension_keyword: str,
    extension_snippet: str,
    matched_keyword: str,
    matched_rules_summary: str,
    user_confidence: int,
    model: str = "claude-sonnet-4-6",
    api_base: Optional[str] = None,
) -> Dict[str, Any]:
    """Use the LLM to judge whether the compatibility decision transferred from a known
    YANG keyword to an unknown vendor extension keyword is appropriate.

    Args:
        extension_keyword: The unknown vendor extension keyword name.
        extension_snippet: Example YANG usage snippet for the extension.
        matched_keyword: The known YANG keyword selected as the best RAG match.
        matched_rules_summary: Human-readable summary of the inherited compatibility rules.
        user_confidence: 1 if the user is confident the rules apply, 0 if uncertain.
        model: LLM model to use.
        api_base: Optional API base URL override.

    Returns:
        Dictionary with keys:
            judgment      : 'aligned' | 'uncertain'
            reasoning     : str explanation
            user_conf     : int (echo of user_confidence)
            llm_judge     : 1 if 'aligned', 0 if 'uncertain'
    """
    try:
        try:
            from .api_config import configure_dspy, FUELIX_API_BASE
        except ImportError:
            from yang_rag.dspy.api_config import configure_dspy, FUELIX_API_BASE

        target_base = api_base or FUELIX_API_BASE
        if not configure_dspy(model=model, api_base=target_base):
            console.print("[yellow]⚠️  LLM judge unavailable — API key not set[/yellow]")
            return {
                "judgment": "uncertain",
                "reasoning": "LLM unavailable — could not configure DSPy.",
                "user_conf": user_confidence,
                "llm_judge": 0,
            }

        predictor = dspy.Predict(JudgeExtensionCompatibility)
        result = predictor(
            extension_keyword=extension_keyword,
            extension_snippet=extension_snippet,
            matched_keyword=matched_keyword,
            matched_rules_summary=matched_rules_summary,
            user_confidence=user_confidence,
        )

        raw_judgment = (result.judgment or "").lower().strip()
        # Check "unaligned"/"uncertain" before "aligned": "unaligned" contains "aligned"
        if "unaligned" in raw_judgment or "uncertain" in raw_judgment or "not aligned" in raw_judgment:
            judgment = "uncertain"
        elif "aligned" in raw_judgment:
            judgment = "aligned"
        else:
            judgment = "uncertain"  # conservative default
        llm_judge = 1 if judgment == "aligned" else 0

        # Display result
        color = "green" if judgment == "aligned" else "yellow"
        icon = "✓" if judgment == "aligned" else "?"
        console.print(Panel.fit(
            f"[bold]LLM Compatibility Judgment:[/bold] [{color}]{icon} {judgment.upper()}[/{color}]\n\n"
            f"{result.reasoning}",
            border_style=color,
            title=f"[bold]Extension: {extension_keyword} ← {matched_keyword}[/bold]",
        ))

        return {
            "judgment": judgment,
            "reasoning": result.reasoning,
            "user_conf": user_confidence,
            "llm_judge": llm_judge,
        }

    except Exception as e:
        console.print(f"[yellow]⚠️  LLM judgment failed: {e}[/yellow]")
        return {
            "judgment": "uncertain",
            "reasoning": f"LLM judgment failed: {e}",
            "user_conf": user_confidence,
            "llm_judge": 0,
        }


def process_report_with_deep_analysis_markers(
    report_file: str,
    output_file: str,
    llm_model: Optional[str] = None
) -> int:
    """Process a compatibility report, analyzing all <needs-deep-analysis> markers.
    
    Args:
        report_file: Path to enriched report with markers
        output_file: Path to write final report with LLM analysis
        llm_model: LLM model to use
        
    Returns:
        Number of markers processed
    """
    with open(report_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    processed_count = 0
    processed_lines = []
    
    for line in lines:
        if '<needs-deep-analysis>' in line.lower():
            # Extract condition info if possible
            # For now, mark as analyzed with conservative classification
            processed_line = line.replace(
                '<needs-deep-analysis>',
                '<llm-analyzed: incomparable (manual-review-recommended)>'
            ).replace(
                '<Needs-Deep-Analysis>',
                '<llm-analyzed: incomparable (manual-review-recommended)>'
            )
            processed_lines.append(processed_line)
            processed_count += 1
        else:
            processed_lines.append(line)
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.writelines(processed_lines)
    
    if processed_count > 0:
        console.print(f"[green]✓[/green] LLM deep analysis complete: {processed_count} markers processed")
        console.print(f"[dim]  Output: {output_file}[/dim]")
    
    return processed_count


# Backward compatibility alias
def process_report_with_dspy_markers(report_file: str, output_file: str, dspy_model: Optional[str] = None) -> int:
    """Deprecated: Use process_report_with_deep_analysis_markers instead."""
    return process_report_with_deep_analysis_markers(report_file, output_file, dspy_model)


if __name__ == "__main__":
    # Example usage - demonstrates correct analysis for keywords, attributes, and constraints
    from yang_rag.comparator.check_compatibility import parse_rules
    
    # Load rules
    rules = parse_rules("yang_rag/comparator/compatibility_rules.xml")
    
    console.print("\n[bold magenta]═══ CONSTRAINT TESTS ═══[/bold magenta]")
    
    # Test Case 1: ADDED constraint (should be NON-BC)
    console.print("\n[bold cyan]Test Case 1: ADDED pattern constraint[/bold cyan]")
    test_constraint_added = {
        'rule_id': 'constraint-relaxed-change-rule',
        'keyword_attribute_or_constraint': 'pattern',
        'action': 'added',
        'original_decision': 'backward-compatible',  # WRONG - adding constraint narrows
        'old_value': '',  # No constraint before
        'new_value': '^[0-9]+$',  # New constraint added
        'yang_path': '/test-module:config/test-leaf',
        'parent_context': 'leaf with type string'
    }
    
    # Test Case 2: DELETED constraint (should be BC)
    console.print("\n[bold cyan]Test Case 2: DELETED pattern constraint[/bold cyan]")
    test_constraint_deleted = {
        'rule_id': 'constraint-relaxed-change-rule',
        'keyword_attribute_or_constraint': 'pattern',
        'action': 'deleted',
        'original_decision': 'non-backward-compatible',  # WRONG - removing constraint relaxes
        'old_value': '^[0-9]+$',  # Had constraint before
        'new_value': '',  # Constraint removed
        'yang_path': '/test-module:config/test-leaf',
        'parent_context': 'leaf with type string'
    }
    
    # Test Case 3: CHANGED constraint - narrowed (should be non-BC)
    console.print("\n[bold cyan]Test Case 3: CHANGED pattern - narrowed[/bold cyan]")
    test_constraint_narrowed = {
        'rule_id': 'constraint-relaxed-change-rule',
        'keyword_attribute_or_constraint': 'pattern',
        'action': 'changed',
        'original_decision': 'backward-compatible',  # WRONG - narrowed is non-BC
        'old_value': '^[0-9]+$',  # Any digits
        'new_value': '^[0-9]{3}$',  # Only 3 digits (more restrictive)
        'yang_path': '/test-module:config/test-leaf',
        'parent_context': 'leaf with type string'
    }
    
    console.print("\n[bold magenta]═══ ATTRIBUTE TESTS ═══[/bold magenta]")
    
    # Test Case 4: ADDED mandatory attribute (should be NON-BC)
    console.print("\n[bold cyan]Test Case 4: ADDED mandatory attribute[/bold cyan]")
    test_attribute_added = {
        'rule_id': 'attribute-value-change-rule',
        'keyword_attribute_or_constraint': 'mandatory',
        'action': 'added',
        'original_decision': 'backward-compatible',  # WRONG - mandatory=true restricts
        'old_value': '',  # No mandatory before (implicitly false)
        'new_value': 'true',  # Now mandatory
        'yang_path': '/test-module:config/test-leaf',
        'parent_context': 'leaf'
    }
    
    # Test Case 5: DELETED mandatory attribute (should be BC)
    console.print("\n[bold cyan]Test Case 5: DELETED mandatory attribute[/bold cyan]")
    test_attribute_deleted = {
        'rule_id': 'attribute-value-change-rule',
        'keyword_attribute_or_constraint': 'mandatory',
        'action': 'deleted',
        'original_decision': 'non-backward-compatible',  # WRONG - removing mandatory relaxes
        'old_value': 'true',  # Was mandatory
        'new_value': '',  # No longer mandatory
        'yang_path': '/test-module:config/test-leaf',
        'parent_context': 'leaf'
    }
    
    console.print("\n[bold magenta]═══ KEYWORD TESTS ═══[/bold magenta]")
    
    # Test Case 6: DELETED keyword (usually NON-BC - removes functionality)
    console.print("\n[bold cyan]Test Case 6: DELETED leaf keyword[/bold cyan]")
    test_keyword_deleted = {
        'rule_id': 'keyword-semantic-change-rule',
        'keyword_attribute_or_constraint': 'leaf',
        'action': 'deleted',
        'original_decision': 'backward-compatible',  # WRONG - removing leaf breaks clients
        'old_value': 'test-leaf',  # Leaf existed
        'new_value': '',  # Leaf removed
        'yang_path': '/test-module:config',
        'parent_context': 'container'
    }
    
    # Run all test cases
    test_cases = [
        test_constraint_added, test_constraint_deleted, test_constraint_narrowed,
        test_attribute_added, test_attribute_deleted,
        test_keyword_deleted
    ]
    
    for test_case in test_cases:
        # Find the appropriate rule
        rule = next((r for r in rules if r.get('rule_id') == test_case['rule_id']), None)
        
        if rule:
            result = verify_compatibility_change(test_case, rule)
            if result:
                console.print(f"\n[bold green]✓ Verification complete for {test_case['action']} {test_case['keyword_or_constraint']}![/bold green]")
            else:
                console.print(f"\n[yellow]No verification needed for {test_case['keyword_or_constraint']} (assistance != 'true')[/yellow]")
        else:
            console.print(f"\n[red]Rule not found: {test_case['rule_id']}[/red]")
