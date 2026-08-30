"""
DSPy Modules for YANG Compatibility Analysis and Rule Generation
-----------------------------------------------------------------
All DSPy Module classes live here — one canonical location.

Sections:
  1. RuleGeneratorModule  — rule generation pipeline (used by pipeline.py)
  2. CompatibilityVerifier — verifies assistance="true" rule decisions
  3. ConditionAnalyzer    — analyzes when/must condition changes
"""

import dspy
from typing import Optional, Dict, Any
from .signatures import (
    GenerateCompatibilityRule,
    GenerateAllCompatibilityRules,
    ValidateGeneratedRule,
    ExplainCompatibilityRule,
    VerifyCompatibilityDecision,
    AnalyzeConditionChange,
)


class RuleGeneratorModule(dspy.Module):
    """DSPy module that generates compatibility rules using chain of thought."""
    
    def __init__(self):
        super().__init__()
        self.generate = dspy.ChainOfThought(GenerateCompatibilityRule)
        self.generate_all = dspy.ChainOfThought(GenerateAllCompatibilityRules)
        self.validate = dspy.ChainOfThought(ValidateGeneratedRule)
        self.explain = dspy.ChainOfThought(ExplainCompatibilityRule)
    
    def forward(self, unknown_keyword: str, similar_keyword: str, similarity_score: float,
                category: str, existing_rules: str, yang_context: str, generate_all: bool = True):
        """Generate, validate, and explain compatibility rule(s).
        
        Args:
            unknown_keyword: The unknown YANG keyword to generate rules for
            similar_keyword: Similar keyword from RAG search
            similarity_score: Semantic similarity (0.0-1.0)
            category: YANG category (structural, constraint, attribute)
            existing_rules: Formatted XML rules for similar keyword
            yang_context: Example YANG syntax with unknown keyword
            generate_all: If True, generate rules for all existing patterns. If False, generate single rule.
            
        Returns:
            dspy.Prediction with generated_xml, confidence, rationale, is_valid, 
            validation_issues, explanation, rules_count
        """
        
        if generate_all:
            # Generate ALL rules based on all existing patterns
            generation = self.generate_all(
                unknown_keyword=unknown_keyword,
                similar_keyword=similar_keyword,
                similarity_score=similarity_score,
                category=category,
                existing_rules=existing_rules,
                yang_context=yang_context
            )
            
            # Use the all_generated_rules output
            generated_xml = generation.all_generated_rules
            confidence = generation.overall_confidence
            rationale = generation.rationale
            rules_count = generation.rules_count
            
        else:
            # Generate single rule (original behavior)
            generation = self.generate(
                unknown_keyword=unknown_keyword,
                similar_keyword=similar_keyword,
                similarity_score=similarity_score,
                category=category,
                existing_rules=existing_rules,
                yang_context=yang_context
            )
            generated_xml = generation.generated_xml
            confidence = generation.confidence
            rationale = generation.rationale
            rules_count = 1
        
        # Step 2: Validate
        validation = self.validate(
            generated_xml=generated_xml,
            schema_requirements=(
                "Rules must follow compatibility_rules.xml schema. "
                "Each <rule> must have exactly ONE of: "
                "<structurals> (for structural/block keywords), "
                "<constraints> (for constraint keywords), or "
                "<attributes> (for attribute keywords). "
                "Use <structural> child elements inside <structurals>, NOT <keyword> or <keywords>."
            ),
            original_keyword=unknown_keyword
        )
        
        # Quick sanity check: Does XML have at least one valid rule structure?
        xml_lower = generated_xml.lower()
        has_structurals = '<structurals>' in xml_lower
        has_keywords = '<keywords>' in xml_lower   # legacy fallback
        has_constraints = '<constraints>' in xml_lower
        has_attributes = '<attributes>' in xml_lower
        has_rule = '<rule>' in xml_lower
        
        if has_rule and (has_structurals or has_keywords or has_constraints or has_attributes):
            # Override validator if it's wrong - XML is structurally valid
            validation.is_valid = True
            validation.issues = "None"
        
        # Step 3: Explain (provide context about multiple rules if applicable)
        if generate_all and rules_count > 1:
            # For multiple rules, add context to help explanation
            explanation_context = f"""
These are {rules_count} different rules for the '{unknown_keyword}' keyword.
Each rule covers a different scenario (add, delete, change with different constraints).
Look at each <rule> element's <compatible> tag to determine if that specific action is backward-compatible or not.

Rules:
{generated_xml}
"""
            explanation = self.explain(
                xml_rule=explanation_context,
                keyword=unknown_keyword
            )
        else:
            # Single rule
            explanation = self.explain(
                xml_rule=generated_xml,
                keyword=unknown_keyword
            )
        
        return dspy.Prediction(
            generated_xml=generated_xml,
            confidence=confidence,
            rationale=rationale,
            is_valid=validation.is_valid,
            validation_issues=validation.issues,
            explanation=explanation.explanation,
            rules_count=rules_count
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. CompatibilityVerifier
# ─────────────────────────────────────────────────────────────────────────────

class CompatibilityVerifier(dspy.Module):
    """DSPy module that verifies compatibility decisions using LLM.

    Used for rules marked with assistance="true" in compatibility_rules.xml.
    Wraps the VerifyCompatibilityDecision signature with ChainOfThought reasoning.
    """

    def __init__(self):
        super().__init__()
        self.verify = dspy.ChainOfThought(VerifyCompatibilityDecision)

    def forward(
        self,
        rule_id: str,
        keyword_or_constraint: str,
        action: str,
        original_decision: str,
        old_value: str = "",
        new_value: str = "",
        yang_path: str = "",
        parent_context: str = "",
        constraint_type: str = "N/A",
        is_relaxed: bool = False
    ) -> dspy.Prediction:
        """Verify a compatibility decision.

        Args:
            rule_id: The rule ID being applied
            keyword_or_constraint: The YANG element being evaluated
            action: added/changed/deleted
            original_decision: Original compatibility from rule
            old_value: Original value (if any)
            new_value: New value (if any)
            yang_path: Full path in YANG tree
            parent_context: Parent node context
            constraint_type: Type of constraint
            is_relaxed: Whether this is a relaxed change

        Returns:
            dspy.Prediction with verification_result, confidence, verified_compatibility,
            reasoning, suggested_action
        """
        return self.verify(
            rule_id=rule_id,
            keyword_or_constraint=keyword_or_constraint,
            action=action,
            original_decision=original_decision,
            old_value=old_value,
            new_value=new_value,
            yang_path=yang_path,
            parent_context=parent_context,
            constraint_type=constraint_type,
            is_relaxed=is_relaxed
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. ConditionAnalyzer
# ─────────────────────────────────────────────────────────────────────────────

class ConditionAnalyzer(dspy.Module):
    """DSPy module for analyzing complex `when`/`must` condition changes.

    Handles cases marked with NEEDS_LLM_ANALYSIS by the rule-based condition
    analyzer when semantic analysis is insufficient.
    Wraps the AnalyzeConditionChange signature with ChainOfThought reasoning.
    """

    def __init__(self):
        super().__init__()
        self.analyze = dspy.ChainOfThought(AnalyzeConditionChange)

    def forward(
        self,
        old_condition: str,
        new_condition: str,
        context: Optional[Dict[str, Any]] = None
    ) -> dspy.Prediction:
        """Analyze a complex condition change.

        Args:
            old_condition: Original condition string
            new_condition: New condition string
            context: Optional dict with keys like yang_path, constraint, action

        Returns:
            dspy.Prediction with classification, explanation, confidence
        """
        # Build a structured context string
        context_str = ""
        static_findings = ""
        if context:
            static_findings = str(context.get("static_findings", "") or "")
            context_items = {
                k: v for k, v in context.items()
                if k not in {"static_findings"}
            }
            context_str = " | ".join(f"{k}: {v}" for k, v in context_items.items())

        # Auto-annotate XPath depth differences to guide the LLM
        import re as _re
        old_depth = len(_re.findall(r'\.\./+', old_condition))
        new_depth = len(_re.findall(r'\.\./+', new_condition))
        if old_depth != new_depth:
            context_str += (
                f" | NOTE: XPath ancestor depth changed from {old_depth} to {new_depth} levels up — "
                "this navigates to a different schema ancestor; use INCOMPARABLE unless you can confirm "
                "both paths resolve to the same node in this schema tree."
            )

        context_str += (
            " | RFC7950_WRAPPER_3_RULES: "
            "1) Resolve relative paths from the effective attachment context of when/must "
            "(uses/choice/case/augment/refine can change this context). "
            "2) If old/new differ only by wrapper-relative rewrite and resolve to the same absolute node, classify EQUIVALENT. "
            "3) If wrapper-aware resolution is inconclusive or targets differ, do not infer from text-only path shape; "
            "prefer INCOMPARABLE unless static findings prove a deterministic broken↔valid transition."
        )

        if static_findings:
            context_str += (
                " | NOTE: Static findings are provided separately; you must double-check "
                "your decision against those findings before final classification."
            )

        return self.analyze(
            old_condition=old_condition,
            new_condition=new_condition,
            context=context_str,
            static_findings=static_findings
        )
