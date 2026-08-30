"""
Core YANG comparison modules.

This package contains the core logic for YANG module comparison,
including strategies, calculators, and comparators.
"""

from .constants import (
    ChangeType,
    SymbolicType,
    RULE_STRUCTURAL_KEYWORDS,
    RULE_ATTRIBUTES,
    CONSTRAINT_KEYWORDS,
    DEFAULT_STRUCTURAL_KEYWORDS,
    DEFAULT_CONSTRAINT_KEYWORDS,
    DEFAULT_PATH_BASED_KEYWORDS,
    DEFAULT_ATTRIBUTE_KEYWORDS,
    SYMBOLIC_KEYWORDS,
    TEMPLATE_KEYWORDS
)
from .data_classes import ChangeRecord
from .similarity import SimilarityCalculator
from .comparison_strategies import (
    ComparisonStrategy,
    ListComparisonStrategy,
    AttributeComparisonStrategy
)
from .node_normalizer import NodeNormalizer
from .typedef_handler import TypedefHandler
from .yang_node_comparator import YANGNodeComparator

__all__ = [
    'ChangeType',
    'SymbolicType',
    'RULE_STRUCTURAL_KEYWORDS',
    'RULE_ATTRIBUTES',
    'CONSTRAINT_KEYWORDS',
    'DEFAULT_STRUCTURAL_KEYWORDS',
    'DEFAULT_CONSTRAINT_KEYWORDS',
    'DEFAULT_PATH_BASED_KEYWORDS',
    'DEFAULT_ATTRIBUTE_KEYWORDS',
    'SYMBOLIC_KEYWORDS',
    'TEMPLATE_KEYWORDS',
    'ChangeRecord',
    'SimilarityCalculator',
    'ComparisonStrategy',
    'ListComparisonStrategy',
    'AttributeComparisonStrategy',
    'NodeNormalizer',
    'TypedefHandler',
    'YANGNodeComparator',
]

