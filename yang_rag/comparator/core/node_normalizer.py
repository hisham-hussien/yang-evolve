#!/usr/bin/env python3
"""
Node normalizer for YANG statements.

This module handles parsing and normalization of YANG statements
from pyang AST into structured dictionaries.
"""

from typing import Dict, List, Any
from ..helper.pyang_utils import PyangStatementHelper
from .constants import (
    DEFAULT_STRUCTURAL_KEYWORDS,
    SYMBOLIC_KEYWORDS,
    SymbolicType
)
from . import constants


class NodeNormalizer:
    """Handles parsing and normalization of YANG statements."""
    
    # Class-level helper instance for pyang statement operations
    helper = PyangStatementHelper()
    
    @staticmethod
    def parse_symbolic_entry(statement, symbolic_type: SymbolicType) -> Dict[str, Any]:
        """
        Parse a symbolic entry (enum or bit) from a YANG type statement.
        
        This unified method handles both enum and bit entries since they
        have identical structure and processing logic.
        
        Args:
            statement: The pyang statement for the symbolic entry
            symbolic_type: The type of symbolic entry (ENUM or BIT)
            
        Returns:
            Dictionary containing the symbolic entry data
        """
        helper = NodeNormalizer.helper
        entry = {"name": helper.get_argument(statement)}
        
        # Extract line number and file for this entry
        line_num, file_path = helper.get_position_info(statement)
        if line_num is not None or file_path is not None:
            metadata = {}
            if line_num is not None:
                metadata["line"] = line_num
            if file_path is not None:
                metadata["file"] = file_path
            entry["metadata"] = metadata
        
        # Extract the resolved value from pyang (i_value for enums, i_position for bits)
        # Pyang resolves implicit values/positions during validation phase
        if symbolic_type == SymbolicType.ENUM:
            if hasattr(statement, 'i_value'):
                entry["value"] = statement.i_value
            else:
                # Debug: check what attributes are available
                if helper.get_argument(statement) in ['AUTO', 'FULL', 'HALF']:
                    print(f"[NodeNormalizer] WARNING: Enum '{helper.get_argument(statement)}' has no i_value attribute")
                    print(f"  Available attributes: {[attr for attr in dir(statement) if attr.startswith('i_')]}")
        elif symbolic_type == SymbolicType.BIT and hasattr(statement, 'i_position'):
            entry["position"] = statement.i_position
        
        # Extract sub-statements (like value, position, status, description, etc.)
        for sub_stmt in helper.get_substmts(statement):
            keyword = helper.get_keyword(sub_stmt)
            arg = helper.get_argument(sub_stmt)
            
            # For explicit value/position statements, convert to int if possible
            if keyword in ['value', 'position'] and arg is not None:
                try:
                    entry[keyword] = int(arg)
                except (ValueError, TypeError):
                    entry[keyword] = arg
            else:
                entry[keyword] = arg
        
        return entry
    
    @staticmethod
    def parse_type_statement(type_statement) -> Dict[str, Any]:
        """
        Parse a YANG 'type' statement preserving order.
        
        Handles special cases:
        - enum: list of enum entries (using unified symbolic logic)
        - bit: list of bit entries (using unified symbolic logic)
        - type: list of union member types (nested type statements)
        
        Args:
            type_statement: The pyang type statement to parse
            
        Returns:
            Dictionary containing type details
        """
        helper = NodeNormalizer.helper
        type_arg = helper.get_argument(type_statement)
        type_details = {"name": type_arg}
        
        # Extract line number and file metadata from type statement.
        # When the type is a typedef reference (i_typedef is set by pyang), use
        # the typedef's 'type' child statement position instead of the reference
        # site's position.  This ensures that a union member like "type ipv6-prefix"
        # (at line 191) reports the same line number as the typedef's own type
        # statement "type string" (at line 160), so both entries can be
        # grouped/deduplicated in the final report.
        i_typedef = getattr(type_statement, 'i_typedef', None)
        if i_typedef is not None:
            # Walk the typedef's substmts to find its 'type' child and use its position.
            # Fall back to the typedef statement itself if no 'type' child is found.
            typedef_type_sub = None
            for td_sub in getattr(i_typedef, 'substmts', []):
                if getattr(td_sub, 'keyword', None) == 'type':
                    typedef_type_sub = td_sub
                    break
            if typedef_type_sub is not None:
                line_num, file_path = helper.get_position_info(typedef_type_sub)
            else:
                line_num, file_path = helper.get_position_info(i_typedef)
        else:
            line_num, file_path = helper.get_position_info(type_statement)
        if line_num is not None or file_path is not None:
            metadata = {}
            if line_num is not None:
                metadata["line"] = line_num
            if file_path is not None:
                metadata["file"] = file_path
            type_details["metadata"] = metadata
        
        enum_count = 0
        for sub in helper.get_substmts(type_statement):
            keyword = helper.get_keyword(sub)
            
            # Unified handling for symbolic types (enum and bit)
            if keyword in SYMBOLIC_KEYWORDS:
                symbolic_type = SymbolicType.ENUM if keyword == "enum" else SymbolicType.BIT
                type_details.setdefault(keyword, [])
                entry = NodeNormalizer.parse_symbolic_entry(sub, symbolic_type)
                type_details[keyword].append(entry)
                if keyword == "enum":
                    enum_count += 1
            elif keyword == "type":
                # Handle union types: nested 'type' statements are union members
                type_details.setdefault("type", [])
                # Recursively parse the nested type statement
                nested_type = NodeNormalizer.parse_type_statement(sub)
                type_details["type"].append(nested_type)
            else:
                # Regular type attributes
                type_details[keyword] = helper.get_argument(sub)

        # If this type references an external typedef (i_typedef is set by pyang
        # during validation), resolve its restrictions so that changes inside the
        # typedef (e.g. a narrowed range in a different module) are visible in the
        # extracted node data and will be detected as a change during comparison.
        # We extract: range, length, pattern, fraction-digits from the typedef's
        # own base type statement.  These are merged into type_details only when
        # NOT already overridden by the leaf's own type sub-statements (i.e. only
        # when the key is not already present).
        # NOTE: i_typedef was already resolved above for metadata purposes.
        _TYPEDEF_RESTRICTIONS = frozenset({'range', 'length', 'pattern', 'fraction-digits'})
        if i_typedef is not None:
            # Walk the typedef's substmts to find its 'type' child, then extract
            # restriction keywords from that base type.
            for td_sub in getattr(i_typedef, 'substmts', []):
                if getattr(td_sub, 'keyword', None) == 'type':
                    # td_sub is e.g. "type union { type uint16 { range ... } }"
                    # For a union typedef, resolve each member's restrictions.
                    # For a simple type, resolve directly.
                    member_types = [s for s in getattr(td_sub, 'substmts', [])
                                    if getattr(s, 'keyword', None) == 'type']
                    if member_types:
                        # union: rebuild the member list with resolved restrictions
                        resolved_members = []
                        for mt in member_types:
                            mt_details = NodeNormalizer.parse_type_statement(mt)
                            resolved_members.append(mt_details)
                        # Only set if not already extracted from direct substmts
                        if 'type' not in type_details:
                            type_details['type'] = resolved_members
                    else:
                        # simple type: pick up range/length/pattern directly
                        for restr in getattr(td_sub, 'substmts', []):
                            restr_kw = getattr(restr, 'keyword', None)
                            if restr_kw in _TYPEDEF_RESTRICTIONS and restr_kw not in type_details:
                                type_details[restr_kw] = helper.get_argument(restr)

        return type_details
    
    @staticmethod
    def extract_node_data(statement) -> Dict[str, Any]:
        """
        Extract normalized data from a pyang Statement.
        
        Args:
            statement: The pyang statement to extract data from
            
        Returns:
            Dictionary with 'attributes', 'constraints', 'children', and optionally 'metadata'
        """
        helper = NodeNormalizer.helper
        attributes = {"name": helper.get_argument(statement)}
        constraints: Dict[str, Any] = {}
        children: List[Dict[str, Any]] = []
        
        # Structural determination now relies on RULE_STRUCTURAL_KEYWORDS from XML;
        # fall back to DEFAULT_STRUCTURAL_KEYWORDS if XML provided none
        structural_keywords = (constants.RULE_STRUCTURAL_KEYWORDS 
                             if constants.RULE_STRUCTURAL_KEYWORDS 
                             else DEFAULT_STRUCTURAL_KEYWORDS)

        # Extract line number and file metadata from pyang statement
        line_num, file_path = helper.get_position_info(statement)
        metadata = {}
        if line_num is not None:
            metadata["line"] = line_num
        if file_path is not None:
            metadata["file"] = file_path

        # Track the 'when' sub-statement position for potential override below.
        _when_sub = None

        for sub in helper.get_substmts(statement):
            keyword = helper.get_keyword(sub)
            
            # Handle "type" keyword:
            # - Always parse the type details (enums, bits, constraints)
            # - Also add as structural child for hierarchical tracking
            if keyword == "type":
                # Parse type details into attributes
                attributes["type"] = NodeNormalizer.parse_type_statement(sub)
                
                # ALSO add as structural child for hierarchical tracking
                # This allows tracking type changes in the tree structure
                if keyword in structural_keywords:
                    children.append({
                        "type": keyword,
                        **NodeNormalizer.extract_node_data(sub)
                    })
            elif keyword in structural_keywords:
                children.append({
                    "type": keyword,
                    **NodeNormalizer.extract_node_data(sub)
                })
            else:
                # YANG condition/filter keywords: always store their XPath argument
                # in constraints — never as a nested attribute dict.  These keywords
                # may have sub-statements (e.g. a `description` child inside `when`)
                # that are irrelevant to comparison and must not cause them to be
                # mis-routed into the attributes dict via the generic else-branch.
                _ALWAYS_CONSTRAINT = frozenset({'when', 'must', 'if-feature'})
                if keyword in _ALWAYS_CONSTRAINT:
                    arg = helper.get_argument(sub)
                    constraints[keyword] = arg if arg is not None else True
                    if keyword == 'when' and _when_sub is None:
                        _when_sub = sub
                elif keyword in constants.CONSTRAINT_KEYWORDS:
                    arg = helper.get_argument(sub)
                    constraints[keyword] = arg if arg is not None else True
                else:
                    existing = attributes.get(keyword)
                    sub_arg = helper.get_argument(sub)
                    sub_substmts = helper.get_substmts(sub)
                    value = sub_arg if not sub_substmts else NodeNormalizer.extract_node_data(sub)
                    if existing is None:
                        attributes[keyword] = value
                    elif isinstance(existing, list):
                        existing.append(value)
                    else:
                        attributes[keyword] = [existing, value]

        # Override the node's metadata line/file with the 'when' constraint's position
        # when the 'when' comes from a 'uses' refinement in the instantiating file.
        #
        # Background: when a 'uses' statement has a 'when' refinement, pyang attaches
        # the 'when' sub-statement to the expanded node.  The 'when' sub-statement's
        # pos points to the 'uses' statement in the instantiating file (e.g. line 282
        # in openconfig-network-instance.yang), while the node's own pos points to the
        # grouping definition (e.g. line 236 in openconfig-network-instance-l2.yang).
        #
        # Using the 'when' position gives each instantiation a unique, correct line
        # number (config uses at line 282, state uses at line 300), which:
        #   1. Prevents false grouping of distinct schema nodes in the report.
        #   2. Points the user to the actual location of the change (the 'uses' stmt).
        if _when_sub is not None:
            when_line, when_file = helper.get_position_info(_when_sub)
            if when_file is not None and when_line is not None:
                import os as _os
                node_file_base = _os.path.basename(file_path) if file_path else None
                when_file_base = _os.path.basename(when_file)
                # Only override when the 'when' is in a DIFFERENT file than the node
                # (i.e. it came from a 'uses' refinement, not from the grouping itself).
                if node_file_base != when_file_base:
                    metadata["line"] = when_line
                    metadata["file"] = when_file_base

        result = {
            "attributes": dict(sorted(attributes.items())),
            "constraints": dict(sorted(constraints.items())),
            "children": sorted(children, key=lambda x: (x["type"], x["attributes"]["name"]))
        }
        
        # Add metadata if present
        if metadata:
            result["metadata"] = metadata
        
        return result
