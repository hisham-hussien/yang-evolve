#!/usr/bin/env python3
"""
yang_common.py - Shared YANG processing utilities

Common utilities used across YANG parsing tools:
- Keyword categorization (structural, constraint, attribute)
- XML rules loading from compatibility_rules.xml
- Extension handling
- Type validation

Used by:
- code/parsing/pyang_extractor.py
- yang_comparator/refactored_yang_comparator.py
"""
import os
import sys
from pathlib import Path
from typing import Dict, Set, Tuple, Optional
import xml.etree.ElementTree as ET


def _load_keywords_from_xml() -> tuple:
    """Load STRUCTURAL, CONSTRAINT, and ATTRIBUTE keyword sets from compatibility_rules.xml.

    The XML is the single source of truth for keyword categorization.
    Falls back to hardcoded sets if the XML cannot be found or parsed.

    Returns:
        (structural_set, constraint_set, attribute_set)
    """
    # Locate compatibility_rules.xml relative to this file
    _here = Path(__file__).resolve().parent
    _candidates = [
        _here.parent / 'comparator' / 'compatibility_rules.xml',
        _here.parent.parent / 'yang_rag' / 'comparator' / 'compatibility_rules.xml',
    ]
    xml_path = None
    for c in _candidates:
        if c.exists():
            xml_path = c
            break

    if xml_path is None:
        # Fallback hardcoded sets (RFC 7950 baseline)
        _struct = {
            'module', 'submodule', 'container', 'leaf', 'list', 'leaf-list',
            'namespace', 'argument', 'revision',
            'choice', 'case', 'grouping', 'typedef', 'rpc', 'notification',
            'action', 'anydata', 'anyxml', 'uses', 'augment', 'identity',
            'extension', 'feature', 'deviation', 'input', 'output', 'bit',
            'enum', 'type', 'import', 'include', 'belongs-to',
        }
        _constr = {
            'must', 'when', 'pattern', 'range', 'length', 'unique',
            'mandatory', 'min-elements', 'max-elements', 'config',
            'ordered-by', 'presence', 'if-feature', 'fraction-digits',
            'require-instance', 'modifier', 'yin-element', 'status',
        }
        _attr = {
            'description', 'reference', 'units', 'default', 'base',
            'prefix', 'yang-version', 'contact', 'organization',
            'revision-date', 'value', 'position', 'error-message',
            'error-app-tag', 'path', 'refine', 'key',
        }
        return _struct, _constr, _attr

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        structural: set = set()
        constraint: set = set()
        attribute: set = set()

        for rule in root.findall('rule'):
            for elem in rule.findall('structurals/structural'):
                if elem.text:
                    structural.add(elem.text.strip())
            for elem in rule.findall('constraints/constraint'):
                if elem.text:
                    constraint.add(elem.text.strip())
            for elem in rule.findall('attributes/attribute'):
                if elem.text:
                    attribute.add(elem.text.strip())

        # Add RFC 7950 base keywords not covered by the XML rules
        # (these are always structural regardless of rule coverage)
        _always_structural = {
            'module', 'submodule', 'container', 'leaf', 'list', 'leaf-list',
            'choice', 'case', 'grouping', 'typedef', 'rpc', 'notification',
            'action', 'anydata', 'anyxml', 'uses', 'augment', 'identity',
            'extension', 'feature', 'deviation', 'input', 'output', 'bit',
            'enum', 'type', 'import', 'include', 'belongs-to',
        }
        structural |= _always_structural

        # Remove from attribute/constraint any keyword that is structural
        # (structural takes precedence — XML structurals is authoritative)
        attribute -= structural
        constraint -= structural

        return structural, constraint, attribute

    except Exception:
        # Parse error — fall back to hardcoded baseline
        _struct = {
            'module', 'submodule', 'container', 'leaf', 'list', 'leaf-list',
            'namespace', 'argument', 'revision',
            'choice', 'case', 'grouping', 'typedef', 'rpc', 'notification',
            'action', 'anydata', 'anyxml', 'uses', 'augment', 'identity',
            'extension', 'feature', 'deviation', 'input', 'output', 'bit',
            'enum', 'type', 'import', 'include', 'belongs-to',
        }
        _constr = {
            'must', 'when', 'pattern', 'range', 'length', 'unique',
            'mandatory', 'min-elements', 'max-elements', 'config',
            'ordered-by', 'presence', 'if-feature', 'fraction-digits',
            'require-instance', 'modifier', 'yin-element', 'status',
        }
        _attr = {
            'description', 'reference', 'units', 'default', 'base',
            'prefix', 'yang-version', 'contact', 'organization',
            'revision-date', 'value', 'position', 'error-message',
            'error-app-tag', 'path', 'refine', 'key',
        }
        return _struct, _constr, _attr


# Load keyword sets from compatibility_rules.xml (single source of truth)
STRUCTURAL_KEYWORDS, CONSTRAINT_KEYWORDS, ATTRIBUTE_KEYWORDS = _load_keywords_from_xml()


# XML type attribute -> mask token mapping.
# The masker reads the XML 'type' attribute for a keyword and maps it here.
# This is the ONLY place where type names are mapped to mask tokens.
XML_TYPE_TO_MASK: dict = {
    'olist':     '<LIST>',
    'ulist':     '<LIST>',
    'boolean':   '<BOOLEAN>',
    'XPath':     '<PATH>',
    'regex':     '<REGEX>',
    'number':    '<NUMBER>',
    'numbers':   '<NUMBER>',
    'version':   '<VERSION>',
    'condition': '<CONDITION>',
    'flag':      '<NO_VALUE>',  # presence/absence flags (no value): mandatory-flag, hint-flag, etc.
    # (no type / empty) -> <STRING> is the default, handled by callers
}


def _build_keyword_xml_types() -> dict:
    """Build keyword -> XML type string mapping from compatibility_rules.xml.

    Stores the XML 'type' attribute value (e.g. 'olist', 'boolean', 'condition')
    for each keyword. The masker then applies XML_TYPE_TO_MASK at runtime.
    This way the mapping is data-driven: changing the XML type automatically
    changes the masking without any code changes.

    'name' and 'path' are excluded — they are metadata keywords handled separately.
    """
    _skip = {'name', 'path'}

    _here = Path(__file__).resolve().parent
    _candidates = [
        _here.parent / 'comparator' / 'compatibility_rules.xml',
        _here.parent.parent / 'yang_rag' / 'comparator' / 'compatibility_rules.xml',
    ]
    xml_path = None
    for c in _candidates:
        if c.exists():
            xml_path = c
            break

    if xml_path is None:
        return {}

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        # keyword -> set of XML type strings found across all rules
        kw_types: dict = {}

        for rule in root.findall('rule'):
            for cat in ['constraints', 'attributes']:
                elem = rule.find(cat)
                if elem is not None:
                    for item in elem:
                        kw = item.text.strip() if item.text else None
                        if not kw or kw in _skip:
                            continue
                        t = item.get('type', '')  # XML type attribute (may be empty)
                        kw_types.setdefault(kw, set()).add(t)

        # Resolve: if a keyword has multiple type annotations, prefer the most
        # specific one (non-empty type wins over empty/default).
        result = {}
        for kw, types in kw_types.items():
            specific = [t for t in types if t]  # non-empty types
            result[kw] = specific[0] if specific else ''  # '' means default <STRING>

        return result

    except Exception:
        return {}


# Keyword -> XML type string (e.g. 'olist', 'boolean', 'condition', '' for default)
# Use XML_TYPE_TO_MASK to convert to mask token: XML_TYPE_TO_MASK.get(type, '<STRING>')
KEYWORD_XML_TYPES: dict = _build_keyword_xml_types()


# YANG built-in types (RFC 7950 Section 9)
YANG_BUILTIN_TYPES = {
    'binary', 'bits', 'boolean', 'decimal64', 'empty',
    'enumeration', 'identityref', 'instance-identifier',
    'int8', 'int16', 'int32', 'int64',
    'leafref', 'string',
    'uint8', 'uint16', 'uint32', 'uint64',
    'union'
}

# All keywords combined
ALL_KEYWORDS = STRUCTURAL_KEYWORDS | CONSTRAINT_KEYWORDS | ATTRIBUTE_KEYWORDS

# Default sets for backward compatibility
DEFAULT_STRUCTURAL_KEYWORDS = STRUCTURAL_KEYWORDS
DEFAULT_CONSTRAINT_KEYWORDS = CONSTRAINT_KEYWORDS
DEFAULT_ATTRIBUTE_KEYWORDS = ATTRIBUTE_KEYWORDS


class YANGRulesLoader:
    """Load and manage YANG compatibility rules from XML."""
    
    @staticmethod
    def load_compatibility_rules(rules_path: Optional[str] = None) -> Optional[Dict[str, Set[str]]]:
        """
        Load allowed keywords/attributes/constraints from compatibility_rules.xml.
        
        Args:
            rules_path: Path to compatibility_rules.xml (optional, auto-detected if None)
            
        Returns:
            Dict with keys: 'keywords', 'attributes', 'constraints'
            Returns None if rules cannot be loaded (extract everything)
        """
        # Auto-detect rules path if not provided
        if rules_path is None:
            # Try multiple locations relative to this file
            possible_paths = [
                Path(__file__).parent.parent / "comparator" / "compatibility_rules.xml",
                Path(__file__).parent.parent.parent / "yang_comparator" / "compatibility_rules.xml",
                Path(__file__).parent.parent / "yang_comparator" / "compatibility_rules.xml",
                Path(__file__).parent.parent.parent / "yang_rag" / "comparator" / "compatibility_rules.xml",
                Path("yang_rag") / "comparator" / "compatibility_rules.xml",
                Path("yang_comparator") / "compatibility_rules.xml",
            ]
            
            for path in possible_paths:
                if path.exists():
                    rules_path = str(path)
                    break
            
            if rules_path is None:
                print("[YANGRulesLoader] Warning: compatibility_rules.xml not found in standard locations")
                return None
        
        allowed = {
            'keywords': set(),
            'attributes': set(),
            'constraints': set()
        }
        
        try:
            tree = ET.parse(rules_path)
            root = tree.getroot()
            
            for rule in root.findall("rule"):
                # Extract structurals (current schema) and legacy keywords.
                structurals_elem = rule.find("structurals")
                if structurals_elem is not None:
                    for structural in structurals_elem.findall("structural"):
                        structural_text = structural.text.strip() if structural.text else ""
                        if structural_text:
                            allowed['keywords'].add(structural_text)

                keywords_elem = rule.find("keywords")
                if keywords_elem is not None:
                    for kw in keywords_elem.findall("keyword"):
                        kw_text = kw.text.strip() if kw.text else ""
                        if kw_text:
                            allowed['keywords'].add(kw_text)
                
                # Extract attributes
                attrs_elem = rule.find("attributes")
                if attrs_elem is not None:
                    for attr in attrs_elem.findall("attribute"):
                        attr_text = attr.text.strip() if attr.text else ""
                        if attr_text:
                            allowed['attributes'].add(attr_text)
                
                # Extract constraints
                constraints_elem = rule.find("constraints")
                if constraints_elem is not None:
                    for const in constraints_elem.findall("constraint"):
                        const_text = const.text.strip() if const.text else ""
                        if const_text:
                            allowed['constraints'].add(const_text)
            
            return allowed if any(allowed.values()) else None
            
        except Exception as e:
            print(f"[YANGRulesLoader] Warning: Could not load rules from {rules_path}: {e}")
            return None
    
    @staticmethod
    def load_rule_sets_for_comparator(rules_path: Optional[str] = None) -> Tuple[Set[str], Set[str], Set[str]]:
        """
        Load rule sets for the comparator (returns tuple format).
        
        Returns:
            Tuple of (keywords, attributes, constraints)
        """
        rules = YANGRulesLoader.load_compatibility_rules(rules_path)
        
        if rules is None:
            return set(), set(), set()
        
        return (
            rules.get('keywords', set()),
            rules.get('attributes', set()),
            rules.get('constraints', set())
        )


class YANGKeywordHelper:
    """Helper functions for YANG keyword operations."""
    
    @staticmethod
    def categorize_keyword(keyword: str, extension_registry: Set[str] = None) -> str:
        """
        Categorize YANG keyword based on RFC 7950.
        
        Args:
            keyword: The keyword to categorize (may include prefix like 'oc-ext:regexp-posix')
            extension_registry: Set of known extension names
            
        Returns:
            'structural', 'constraint', 'attribute', 'extension', or 'unknown'
        """
        # Handle extension keywords (prefix:keyword)
        base_keyword = keyword
        if ':' in keyword:
            base_keyword = keyword.split(':', 1)[1]
            # Check if it's a known extension
            if extension_registry and base_keyword in extension_registry:
                return 'extension'
            # Default extensions to constraint category (conservative)
            return 'constraint'
        
        if base_keyword in STRUCTURAL_KEYWORDS:
            return 'structural'
        elif base_keyword in CONSTRAINT_KEYWORDS:
            return 'constraint'
        elif base_keyword in ATTRIBUTE_KEYWORDS:
            return 'attribute'
        else:
            return 'unknown'
    
    @staticmethod
    def extract_base_keyword(keyword: str) -> str:
        """
        Extract base keyword from extension keyword.
        
        Args:
            keyword: Keyword (may be 'prefix:keyword' or 'keyword')
            
        Returns:
            Base keyword without prefix
            
        Examples:
            'oc-ext:regexp-posix' -> 'regexp-posix'
            'endpoint' -> 'endpoint'
        """
        if ':' in keyword:
            return keyword.split(':', 1)[1]
        return keyword
    
    @staticmethod
    def is_structural_keyword(keyword: str) -> bool:
        """Check if keyword is structural."""
        base = YANGKeywordHelper.extract_base_keyword(keyword)
        return base in STRUCTURAL_KEYWORDS
    
    @staticmethod
    def is_constraint_keyword(keyword: str) -> bool:
        """Check if keyword is a constraint."""
        base = YANGKeywordHelper.extract_base_keyword(keyword)
        return base in CONSTRAINT_KEYWORDS
    
    @staticmethod
    def is_attribute_keyword(keyword: str) -> bool:
        """Check if keyword is an attribute."""
        base = YANGKeywordHelper.extract_base_keyword(keyword)
        return base in ATTRIBUTE_KEYWORDS
    
    @staticmethod
    def is_builtin_type(type_name: str) -> bool:
        """Check if type is a YANG built-in type."""
        return type_name in YANG_BUILTIN_TYPES


class YANGStatementFilter:
    """Filter for determining which YANG statements to extract/process."""
    
    def __init__(self, allowed_statements: Optional[Dict[str, Set[str]]] = None):
        """
        Initialize statement filter.
        
        Args:
            allowed_statements: Dict with 'keywords', 'attributes', 'constraints' sets
                                If None, all statements are allowed
        """
        self.allowed_statements = allowed_statements
        self.uncovered_statements = []
        self._uncovered_seen = set()
    
    def should_extract_statement(self, keyword: str, track_uncovered: bool = False,
                                 source_file: str = "unknown", line_no: int = 0,
                                 snippet: str = "") -> bool:
        """
        Determine if a statement should be extracted based on rules.
        
        Args:
            keyword: The statement keyword
            track_uncovered: If True, track uncovered statements
            source_file: Source file path (for tracking)
            line_no: Line number (for tracking)
            snippet: Code snippet (for tracking)
            
        Returns:
            True if statement should be extracted, False otherwise
        """
        # If rules weren't loaded, extract everything
        if self.allowed_statements is None:
            return True

        # Reject vendor-prefixed extension keywords (e.g. junos:must, tailf:callpoint).
        # The corpus is designed to contain only standard YANG keywords so that the RAG
        # system surfaces standard constructs as candidates for unknown vendor extensions.
        # Vendor extensions are the *query* side (uncovered statements), not the corpus side.
        if ':' in keyword:
            if track_uncovered:
                self._track_uncovered_statement(keyword, source_file, line_no, snippet)
            return False

        # Extract base keyword (remove prefix if present — defensive, should be no-op now)
        base_keyword = YANGKeywordHelper.extract_base_keyword(keyword)

        # Check if keyword is in allowed lists
        if base_keyword in self.allowed_statements.get('keywords', set()):
            return True
        if base_keyword in self.allowed_statements.get('attributes', set()):
            return True
        if base_keyword in self.allowed_statements.get('constraints', set()):
            return True
        
        # Not in allowed lists - track as uncovered if requested
        if track_uncovered:
            self._track_uncovered_statement(base_keyword, source_file, line_no, snippet)
        
        return False
    
    def _track_uncovered_statement(self, keyword: str, source_file: str,
                                   line_no: int, snippet: str):
        """Track an uncovered statement for reporting."""
        # Create signature for deduplication
        signature = f"{keyword}|{source_file}"
        
        if signature in self._uncovered_seen:
            return
        
        self._uncovered_seen.add(signature)
        
        self.uncovered_statements.append({
            'keyword': keyword,
            'file': source_file,
            'line': line_no,
            'snippet': snippet[:100]  # Keep it short
        })
    
    def get_uncovered_statements(self) -> list:
        """Get list of uncovered statements."""
        return self.uncovered_statements
    
    def get_uncovered_count(self) -> int:
        """Get count of unique uncovered statement types."""
        return len(self._uncovered_seen)


# Export main components
__all__ = [
    # Keyword sets
    'STRUCTURAL_KEYWORDS',
    'CONSTRAINT_KEYWORDS',
    'ATTRIBUTE_KEYWORDS',
    'YANG_BUILTIN_TYPES',
    'ALL_KEYWORDS',
    'DEFAULT_STRUCTURAL_KEYWORDS',
    'DEFAULT_CONSTRAINT_KEYWORDS',
    'DEFAULT_ATTRIBUTE_KEYWORDS',
    
    # Classes
    'YANGRulesLoader',
    'YANGKeywordHelper',
    'YANGStatementFilter',
]
