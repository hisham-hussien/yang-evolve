#!/usr/bin/env python3
"""
Global constants and enums for YANG comparison.

This module centralizes all constants, enums, and default values used
across the YANG comparison tool.
"""

from enum import Enum
from typing import Set, Tuple


class ChangeType(Enum):
    """Enumeration of change types in YANG module comparison."""
    ADDED = "added"
    DELETED = "deleted"
    CHANGED = "changed"
    RENAMED = "renamed"
    UNCHANGED = "unchanged"


class SymbolicType(Enum):
    """
    Enumeration for symbolic types that have similar handling logic.
    
    These types (enum, bit) share common patterns and can be processed
    using unified logic with symbolic markers in the XML rules.
    """
    ENUM = "enum"
    BIT = "bit"


# ----------------------------------------------------------------------------------
# Global Rule Sets (populated from XML)
# ----------------------------------------------------------------------------------

# These will be populated at runtime from compatibility_rules.xml
RULE_STRUCTURAL_KEYWORDS: Set[str] = set()
RULE_ATTRIBUTES: Set[str] = set()
CONSTRAINT_KEYWORDS: Set[str] = set()

# ----------------------------------------------------------------------------------
# Default Structural Keywords
# ----------------------------------------------------------------------------------

DEFAULT_STRUCTURAL_KEYWORDS: Set[str] = {
    "namespace", "import", "revision", "module", "grouping", "container",
    "uses", "type", "list", "leaf", "augment", "deviation", "deviate", "leaf-list", "choice",
    "case", "rpc", "identity", "typedef", "action", "anydata", "anyxml", "enum", "bit",
    "extension", "argument"
}

# ----------------------------------------------------------------------------------
# Default Constraint Keywords
# ----------------------------------------------------------------------------------

DEFAULT_CONSTRAINT_KEYWORDS: Set[str] = {
    "range", "length", "mandatory", "status", "config", "min-elements",
    "max-elements", "fraction-digits", "must", "when", "pattern",
    "if-feature", "ordered-by", "yin-element", "default", "require-instance"
}

# ----------------------------------------------------------------------------------
# Default Attribute Keywords
# ----------------------------------------------------------------------------------

DEFAULT_ATTRIBUTE_KEYWORDS: Set[str] = {
    "name", "path", "value", "base", "organization", "contact","description", "prefix", "include", "units",
    "reference", "yang-version", "presence", "key", "unique", "error-message"
}

# ----------------------------------------------------------------------------------
# Path-Based Keywords
# ----------------------------------------------------------------------------------

# Path-based keywords that use XPath-style references instead of simple names
# These are defined by the YANG language specification
DEFAULT_PATH_BASED_KEYWORDS: Set[str] = {
    "augment", "deviation"
}

# ----------------------------------------------------------------------------------
# Symbolic Keywords
# ----------------------------------------------------------------------------------

# Keywords that are marked as symbolic in the XML rules
# These have similar handling logic (e.g., enum and bit)
SYMBOLIC_KEYWORDS: Set[str] = {
    SymbolicType.ENUM.value,
    SymbolicType.BIT.value
}

# ----------------------------------------------------------------------------------
# Structural Child Keywords
# ----------------------------------------------------------------------------------

# Keywords representing structural child nodes that are reported separately with their own paths
# These should NOT be reported as nested attributes of their parent nodes
# to avoid duplicate reporting (once as parent's nested detail, once as separate structural element)
STRUCTURAL_CHILD_KEYWORDS: Set[str] = {
    "type",      # type nodes have their own paths (e.g., leaf/uint8)
    "enum",      # enum nodes have their own paths (e.g., type/enumeration/ENABLED)
    "bit",       # bit nodes have their own paths (e.g., type/bits/flag1)
    "argument",  # argument nodes have their own paths under extension (e.g., extension/catalog-organization/org)
}

# ----------------------------------------------------------------------------------
# Template Keywords
# ----------------------------------------------------------------------------------

# Keywords that are templates/definitions and should not appear in schema paths
# These are expanded via other statements (e.g., grouping expanded via uses)
TEMPLATE_KEYWORDS: Set[str] = {
    "grouping"  # typedef is NOT a template - it represents real schema changes
}
