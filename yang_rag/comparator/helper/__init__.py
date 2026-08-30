"""Helper package for comparator condition and XPath utilities."""

from .condition_analyzer import (
    ConditionAnalyzer,
    ChangeDirection,
    ConditionType,
    ParsedCondition,
    classify_condition_change,
)
from .constraint_change import (
    parse_old_new_from_line,
    classify_constraint_change,
    classify_xpath_attribute_change,
)
from .pyang_utils import get_pyang_xpath_broken_lines
from .xpath_comparison import (
    PathComparisonOutcome,
    PathComparisonResult,
    compare_single_xpath,
    normalise_context_path,
    YANG_SUB_STATEMENT_KEYWORDS,
)

__all__ = [
    # Condition analysis
    'ConditionAnalyzer',
    'ChangeDirection',
    'ConditionType',
    'ParsedCondition',
    'classify_condition_change',
    # Constraint / attribute change classification
    'parse_old_new_from_line',
    'classify_constraint_change',
    'classify_xpath_attribute_change',
    # Pyang utilities
    'get_pyang_xpath_broken_lines',
    # XPath comparison (enum-based, canonical)
    'PathComparisonOutcome',
    'PathComparisonResult',
    'compare_single_xpath',
    'normalise_context_path',
    'YANG_SUB_STATEMENT_KEYWORDS',
]
