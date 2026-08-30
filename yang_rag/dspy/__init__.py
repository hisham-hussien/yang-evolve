"""
DSPy Module
===========
DSPy-based LLM pipelines for generating XML compatibility rules.

Feature Modes:
- LITE MODE (default): Comparator + LLM Verification only
- FULL MODE: Comparator + LLM Verification + RAG + Rule Generation Pipeline

Set YANG_COMPARATOR_MODE environment variable to "full" to enable all features.
Default is "lite" mode.

This module provides modular components for rule generation:
- signatures: DSPy signature definitions for LLM prompts
- modules: RuleGeneratorModule with ChainOfThought reasoning
- xml_utils: XML extraction and formatting utilities
- api_config: FuelIX API configuration
- pipeline: Main rule generation workflow (FULL MODE only - requires yang_rag.rag)
- display: Rich-formatted result display
- cli: Command-line interface (main entry point)
- llm_verification: LLM-based verification for compatibility decisions (LITE + FULL)
"""

import os

# Feature flag: Check if full mode is enabled
COMPARATOR_MODE = os.environ.get('YANG_COMPARATOR_MODE', 'lite').lower()
IS_FULL_MODE = COMPARATOR_MODE == 'full'
IS_LITE_MODE = not IS_FULL_MODE

# LLM Verification and Condition Analysis - AVAILABLE IN BOTH MODES
from .llm_verification import (
    verify_compatibility_change,
    batch_verify_changes,
    needs_llm_verification,
    analyze_condition_change,
    process_report_with_dspy_markers,
)
# Signatures (canonical location: signatures.py)
from .signatures import (
    VerifyCompatibilityDecision,
    AnalyzeConditionChange,
)
# Modules (canonical location: modules.py)
from .modules import (
    CompatibilityVerifier,
    ConditionAnalyzer,
)

__all__ = [
    # Feature flags
    'COMPARATOR_MODE',
    'IS_FULL_MODE',
    'IS_LITE_MODE',
    # Verification and Analysis - Available in both modes
    'verify_compatibility_change',
    'batch_verify_changes',
    'needs_llm_verification',
    'analyze_condition_change',
    'process_report_with_dspy_markers',
    'VerifyCompatibilityDecision',
    'AnalyzeConditionChange',
    'CompatibilityVerifier',
    'ConditionAnalyzer',
]

# FULL MODE: Import additional pipeline and RAG features
if IS_FULL_MODE:
    try:
        from .pipeline import (
            generate_rule_for_unknown_keyword,
            RuleGenerationResult
        )
        from .modules import RuleGeneratorModule
        from .signatures import (
            GenerateCompatibilityRule,
            GenerateAllCompatibilityRules,
            ExplainCompatibilityRule,
            ValidateGeneratedRule
        )
        from .display import display_generation_result
        from .api_config import configure_dspy, get_api_key, FUELIX_API_BASE, FUELIX_DEFAULT_MODEL
        from .xml_utils import extract_rules_for_keyword, format_rules_as_text
        
        # Add full mode exports
        __all__.extend([
            'generate_rule_for_unknown_keyword',
            'RuleGenerationResult',
            'RuleGeneratorModule',
            'GenerateCompatibilityRule',
            'GenerateAllCompatibilityRules',
            'ExplainCompatibilityRule',
            'ValidateGeneratedRule',
            'display_generation_result',
            'configure_dspy',
            'get_api_key',
            'extract_rules_for_keyword',
            'format_rules_as_text',
            'FUELIX_API_BASE',
            'FUELIX_DEFAULT_MODEL',
        ])
    except ImportError as e:
        print(f"[WARNING] Full mode requested but dependencies not available: {e}")
        print("[WARNING] Falling back to lite mode (comparator + LLM verification only)")
        IS_FULL_MODE = False
        IS_LITE_MODE = True
