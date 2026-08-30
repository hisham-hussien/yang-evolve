"""
YANG Compatibility Self-Improving Pipeline
-------------------------------------------
Orchestrates end-to-end workflow for automatically improving
YANG compatibility rule generation.

Main Components:
- YangCompatibilityPipeline: Main orchestrator class
- optimize_prompts: DSPy prompt optimization (run after collecting data)
"""

from .yang_compatibility_pipeline import (
    YangCompatibilityPipeline,
    UnmarkedStatement,
    RuleGenerationAttempt
)

__all__ = [
    'YangCompatibilityPipeline',
    'UnmarkedStatement',
    'RuleGenerationAttempt',
]
