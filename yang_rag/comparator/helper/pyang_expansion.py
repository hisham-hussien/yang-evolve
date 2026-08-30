#!/usr/bin/env python3
"""
pyang_expansion.py - Handle recursive expansion of YANG statements

This module provides centralized logic for expanding YANG statements like:
- uses: Expands grouping references into their actual content
- augment: Applies augmentations to target nodes
- deviation: Applies deviations to modify existing schemas

The expansion behavior is controlled by XML rules configuration where keywords
can have a recursive="true" attribute to enable expansion.
"""

import xml.etree.ElementTree as ET
from typing import Dict, Set, Optional
from pathlib import Path
import os

try:
    from pyang import statements, context
except ImportError:
    print("ERROR: pyang not installed. Install with: pip install pyang")
    import sys
    sys.exit(1)


class ExpansionConfig:
    """Configuration for which YANG statements should be recursively expanded."""
    
    def __init__(self):
        self.expand_uses = False
        self.expand_augment = False
        self.expand_deviation = False
        
    def __repr__(self):
        return (f"ExpansionConfig(uses={self.expand_uses}, "
                f"augment={self.expand_augment}, deviation={self.expand_deviation})")


class PyangExpansionHandler:
    """
    Handles recursive expansion of YANG statements based on XML configuration.
    
    This class provides a centralized way to expand YANG statements like uses,
    augment, and deviation according to the recursive="true" attributes in
    the compatibility rules XML file.
    """
    
    def __init__(self, rules_xml_path: Optional[str] = None):
        """
        Initialize the expansion handler.
        
        Args:
            rules_xml_path: Path to compatibility_rules.xml. If None, uses default path.
        """
        if rules_xml_path is None:
            # Default to the canonical comparator rules file one level above helper/.
            # Previous behavior looked in helper/compatibility_rules.xml, which does
            # not exist in this repo and disabled all recursive expansions.
            helper_dir = os.path.dirname(os.path.abspath(__file__))
            comparator_dir = os.path.dirname(helper_dir)
            default_rules = os.path.join(comparator_dir, "compatibility_rules.xml")
            env_rules = os.environ.get("YANG_XML_RULES")

            if env_rules and os.path.isfile(env_rules):
                rules_xml_path = env_rules
            elif os.path.isfile(default_rules):
                rules_xml_path = default_rules
            else:
                # Keep a deterministic fallback path for warning diagnostics.
                rules_xml_path = default_rules
        
        self.config = self._load_expansion_config(rules_xml_path)
        print(f"[ExpansionHandler] Loaded configuration: {self.config}")
    
    def _load_expansion_config(self, rules_path: str) -> ExpansionConfig:
        """
        Parse XML rules file to determine which statements should be expanded.
        
        Args:
            rules_path: Path to the compatibility_rules.xml file
            
        Returns:
            ExpansionConfig with flags for each expansion type
        """
        config = ExpansionConfig()
        
        try:
            tree = ET.parse(rules_path)
            root = tree.getroot()
            
            for rule in root.findall("rule"):
                # Updated to use 'structurals/structural' instead of 'keywords/keyword'
                for kw in rule.findall("structurals/structural"):
                    if kw.text and kw.get("recursive") == "true":
                        keyword = kw.text.strip()
                        if keyword == "uses":
                            config.expand_uses = True
                            print(f"[ExpansionHandler] 'uses' expansion enabled (recursive='true')")
                        elif keyword == "augment":
                            config.expand_augment = True
                            print(f"[ExpansionHandler] 'augment' expansion enabled (recursive='true')")
                        elif keyword == "deviation":
                            config.expand_deviation = True
                            print(f"[ExpansionHandler] 'deviation' expansion enabled (recursive='true')")
        
        except Exception as e:
            print(f"[ExpansionHandler] Warning: Could not load expansion config from {rules_path}: {e}")
            print(f"[ExpansionHandler] Using defaults (all expansions disabled)")
        
        return config
    
    def should_expand_any(self) -> bool:
        """Check if any expansion is enabled."""
        return self.config.expand_uses or self.config.expand_augment or self.config.expand_deviation
    
    def expand_module(self, pyang_context: context.Context, module_stmt: statements.Statement) -> bool:
        """
        Apply all enabled expansions to a module statement.
        
        Args:
            pyang_context: The pyang context object (not PyangContext wrapper)
            module_stmt: The module statement to expand
            
        Returns:
            True if all expansions succeeded, False if any failed
        """
        if not self.should_expand_any():
            print(f"[ExpansionHandler] All expansions DISABLED - preserving statements as-is")
            return True
        
        print(f"[ExpansionHandler] Starting expansion process...")
        success = True
        
        # Order matters! Apply expansions in the correct sequence:
        # 1. uses (v_expand_1_uses) - expands grouping references
        # 2. augment (v_expand_2_augment) - applies augmentations
        # 3. deviation - applies deviations to modify schema
        
        if self.config.expand_uses:
            success &= self._expand_uses(pyang_context, module_stmt)
        
        if self.config.expand_augment:
            success &= self._expand_augment(pyang_context, module_stmt)
        
        if self.config.expand_deviation:
            success &= self._expand_deviation(pyang_context, module_stmt)
        
        if success:
            print(f"[ExpansionHandler] ✓ All expansions completed successfully")
        else:
            print(f"[ExpansionHandler] ⚠ Some expansions encountered warnings")
        
        return success
    
    def _expand_uses(self, pyang_context: context.Context, module_stmt: statements.Statement) -> bool:
        """
        Expand 'uses' statements - replaces uses with actual grouping content.
        
        This is pyang's v_expand_1_uses function.
        
        Args:
            pyang_context: The pyang context
            module_stmt: The module statement
            
        Returns:
            True if successful, False otherwise
        """
        try:
            print(f"[ExpansionHandler]   → Expanding 'uses' statements...")
            statements.v_expand_1_uses(pyang_context, module_stmt)
            print(f"[ExpansionHandler]   ✓ 'uses' expansion complete")
            return True
        except Exception as e:
            print(f"[ExpansionHandler]   ✗ 'uses' expansion failed: {e}")
            return False
    
    def _expand_augment(self, pyang_context: context.Context, module_stmt: statements.Statement) -> bool:
        """
        Expand 'augment' statements - applies augmentations to target nodes.
        
        This is pyang's v_expand_2_augment function.
        
        Note: Augment expansion may fail if there are no augment statements or if
        target nodes are not found. This is not always an error.
        
        Args:
            pyang_context: The pyang context
            module_stmt: The module statement
            
        Returns:
            True if successful, False otherwise
        """
        try:
            print(f"[ExpansionHandler]   → Expanding 'augment' statements...")
            
            # Collect augment statements and ensure they are linked to a module.
            # In some parsed trees, augment statements may miss i_module which
            # causes pyang internals to crash while reading i_module.i_version.
            augment_stmts = [
                stmt for stmt in getattr(module_stmt, 'substmts', [])
                if stmt.keyword == 'augment'
            ]
            
            if not augment_stmts:
                print(f"[ExpansionHandler]   ℹ No 'augment' statements found - skipping")
                return True

            def _collect_augments(stmt):
                out = []
                for child in getattr(stmt, 'substmts', []) or []:
                    if getattr(child, 'keyword', None) == 'augment':
                        out.append(child)
                    out.extend(_collect_augments(child))
                return out

            # Normalize module linkage for augments in the root module and across
            # context-loaded modules/submodules that v_expand_2_augment may inspect.
            all_augments = list(augment_stmts)
            for _key, mod in getattr(pyang_context, 'modules', {}).items():
                if mod is None:
                    continue
                all_augments.extend(_collect_augments(mod))

            seen = set()
            for augment_stmt in all_augments:
                sid = id(augment_stmt)
                if sid in seen:
                    continue
                seen.add(sid)
                owner_module = getattr(augment_stmt, 'i_module', None) or getattr(augment_stmt, 'top', None) or module_stmt
                if not hasattr(augment_stmt, 'i_module') or augment_stmt.i_module is None:
                    augment_stmt.i_module = owner_module
                if not hasattr(augment_stmt, 'i_orig_module') or augment_stmt.i_orig_module is None:
                    augment_stmt.i_orig_module = owner_module

            # Prepare augment target references before expansion when available.
            # Some pyang versions require this phase so v_expand_2_augment can
            # resolve and attach to target nodes safely.
            ref_augment = getattr(statements, 'v_reference_augment', None)
            if callable(ref_augment):
                for augment_stmt in all_augments:
                    try:
                        ref_augment(pyang_context, augment_stmt)
                    except Exception as ref_err:
                        print(f"[ExpansionHandler]   ⚠ augment reference failed for '{getattr(augment_stmt, 'arg', '')}': {ref_err}")
            
            statements.v_expand_2_augment(pyang_context, module_stmt)
            print(f"[ExpansionHandler]   ✓ 'augment' expansion complete")
            return True
        except AttributeError as e:
            # Common error when context is not fully initialized
            print(f"[ExpansionHandler]   ⚠ 'augment' expansion skipped: {e}")
            return True  # Not a fatal error
        except Exception as e:
            print(f"[ExpansionHandler]   ✗ 'augment' expansion failed: {e}")
            return False
    
    def _expand_deviation(self, pyang_context: context.Context, module_stmt: statements.Statement) -> bool:
        """
        Process 'deviation' statements - applies deviations to modify the schema.
        
        Pyang processes deviations in two phases:
        1. v_reference_deviation (reference_1 phase) - finds target nodes
        2. v_reference_deviate (reference_2 phase) - applies the actual deviations
        
        Deviations can:
        - Mark nodes as 'not-supported' (removes them from schema)
        - Add new properties to existing nodes
        - Replace existing properties
        - Delete properties from nodes
        
        Args:
            pyang_context: The pyang context
            module_stmt: The module statement
            
        Returns:
            True if successful, False otherwise
        """
        try:
            print(f"[ExpansionHandler]   → Processing 'deviation' statements...")
            
            # Check if module has any deviation statements
            deviations = [stmt for stmt in getattr(module_stmt, 'substmts', []) 
                         if stmt.keyword == 'deviation']
            
            if not deviations:
                print(f"[ExpansionHandler]   ℹ No 'deviation' statements found - skipping")
                return True
            
            print(f"[ExpansionHandler]   ℹ Found {len(deviations)} deviation statement(s)")
            
            # Ensure deviations have i_module attribute (required by pyang)
            for deviation_stmt in deviations:
                if not hasattr(deviation_stmt, 'i_module'):
                    deviation_stmt.i_module = module_stmt
            
            # Phase 1: Reference deviation - find target nodes
            # This sets the i_target_node attribute on each deviation statement
            for deviation_stmt in deviations:
                statements.v_reference_deviation(pyang_context, deviation_stmt)
            
            # Phase 2: Apply deviations - process deviate substatements
            # This applies the actual modifications (add/replace/delete/not-supported)
            for deviation_stmt in deviations:
                deviate_stmts = deviation_stmt.search('deviate')
                for deviate_stmt in deviate_stmts:
                    statements.v_reference_deviate(pyang_context, deviate_stmt)
            
            print(f"[ExpansionHandler]   ✓ 'deviation' processing complete")
            return True
            
        except AttributeError as e:
            # Common error when context is not fully initialized
            print(f"[ExpansionHandler]   ⚠ 'deviation' processing skipped: {e}")
            return True  # Not a fatal error
        except Exception as e:
            print(f"[ExpansionHandler]   ✗ 'deviation' processing failed: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def get_expansion_summary(self) -> Dict[str, bool]:
        """
        Get a summary of which expansions are enabled.
        
        Returns:
            Dictionary mapping expansion type to enabled status
        """
        return {
            "uses": self.config.expand_uses,
            "augment": self.config.expand_augment,
            "deviation": self.config.expand_deviation
        }


# Convenience function for one-shot expansion
def expand_yang_module(pyang_context: context.Context, 
                      module_stmt: statements.Statement,
                      rules_xml_path: Optional[str] = None) -> bool:
    """
    Convenience function to expand a YANG module according to XML rules.
    
    Args:
        pyang_context: The pyang context object
        module_stmt: The module statement to expand
        rules_xml_path: Optional path to rules XML (uses default if None)
        
    Returns:
        True if expansion succeeded, False otherwise
        
    Example:
        >>> from pyang_expansion import expand_yang_module
        >>> expand_yang_module(ctx, module_stmt)
    """
    handler = PyangExpansionHandler(rules_xml_path)
    return handler.expand_module(pyang_context, module_stmt)


if __name__ == "__main__":
    # Test/demo code
    print("PyangExpansionHandler - Test Mode")
    print("=" * 60)
    
    handler = PyangExpansionHandler()
    print(f"\nExpansion configuration: {handler.config}")
    print(f"Should expand any: {handler.should_expand_any()}")
    print(f"\nExpansion summary:")
    for stmt_type, enabled in handler.get_expansion_summary().items():
        status = "ENABLED" if enabled else "DISABLED"
        print(f"  {stmt_type:12} : {status}")
