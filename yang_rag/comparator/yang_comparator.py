#!/usr/bin/env python3
"""
YANG Module Comparator - Main Entry Point

A generic YANG module comparator that produces hierarchical diff reports.
Unified handling for all node types with configurable comparison strategies.

This is the refactored main entry point that orchestrates the comparison
using modular core components.
"""

import os
import sys
import traceback
import xml.etree.ElementTree as ET
from typing import Set, Tuple

# Import core comparison modules
try:
    # Try relative import first (when used as module)
    from .core import (
        ChangeType,
        YANGNodeComparator,
        RULE_STRUCTURAL_KEYWORDS,
        RULE_ATTRIBUTES,
        CONSTRAINT_KEYWORDS,
        DEFAULT_STRUCTURAL_KEYWORDS,
        DEFAULT_CONSTRAINT_KEYWORDS,
        DEFAULT_ATTRIBUTE_KEYWORDS,
    )
    from .report.report_generator import ReportGenerator
    from .helper.pyang_utils import PyangStatementHelper
except ImportError:
    # Fall back to direct import (when run as script)
    from core import (
        ChangeType,
        YANGNodeComparator,
        RULE_STRUCTURAL_KEYWORDS,
        RULE_ATTRIBUTES,
        CONSTRAINT_KEYWORDS,
        DEFAULT_STRUCTURAL_KEYWORDS,
        DEFAULT_CONSTRAINT_KEYWORDS,
        DEFAULT_ATTRIBUTE_KEYWORDS,
    )
    from report_generator import ReportGenerator
    from pyang_utils import PyangStatementHelper


# ----------------------------------------------------------------------------------
# Global Rule Sets (populated from XML at initialization)
# ----------------------------------------------------------------------------------

# These will be populated from compatibility_rules.xml at startup
RULE_STRUCTURAL_KEYWORDS: Set[str] = set()
RULE_ATTRIBUTES: Set[str] = set()
CONSTRAINT_KEYWORDS: Set[str] = set()


def _load_rule_sets(rules_path: str) -> Tuple[Set[str], Set[str], Set[str]]:
    """
    Parse the XML rules file and return (structurals, attributes, constraints).
    
    Updated to parse <structural> tags instead of <keyword> tags.
    
    The function is resilient: on any parsing/IO error it returns previously
    collected sets (possibly empty) so the comparator can still operate.
    
    Args:
        rules_path: Path to compatibility_rules.xml
        
    Returns:
        Tuple of (structural_keywords, attributes, constraints)
    """
    structurals: Set[str] = set()
    attrs: Set[str] = set()
    constraints: Set[str] = set()
    
    try:
        tree = ET.parse(rules_path)
        root = tree.getroot()
        
        for rule in root.findall("rule"):
            # Parse structural elements (was keywords)
            for st in rule.findall("structurals/structural"):
                if st.text:
                    structurals.add(st.text.strip())
            
            # Parse attributes
            for at in rule.findall("attributes/attribute"):
                if at.text:
                    attrs.add(at.text.strip())
            
            # Parse constraints
            for ct in rule.findall("constraints/constraint"):
                if ct.text:
                    constraints.add(ct.text.strip())
    
    except Exception as e:
        # Non-fatal: fall back to previous/global (may be empty)
        print(f"[RuleLoader] Warning: could not load rule sets from {rules_path}: {e}")
    
    return structurals, attrs, constraints


def _initialize_rule_sets(custom_rules_file: str = None):
    """Initialize global rule sets from compatibility_rules.xml.

    Loads from (in priority order):
    1. ``custom_rules_file`` argument (explicit override)
    2. ``YANG_XML_RULES`` environment variable (set by pyang plugin for --xml-rules)
    3. The bundled ``compatibility_rules.xml`` next to this file

    When a custom file is used, its constraint/attribute/structural sets are
    merged with those from the bundled file so that Phase-2-added keywords
    (e.g. ``max-access`` added as ``<constraint>`` to a custom XML) are
    included in ``CONSTRAINT_KEYWORDS`` and correctly classified by the
    comparator on re-run.
    """
    global RULE_STRUCTURAL_KEYWORDS, RULE_ATTRIBUTES, CONSTRAINT_KEYWORDS

    script_dir = os.path.dirname(os.path.abspath(__file__))
    bundled_file = os.path.join(script_dir, "compatibility_rules.xml")

    # Resolve the active rules file
    active_file = (
        custom_rules_file
        or os.environ.get("YANG_XML_RULES")
        or bundled_file
    )

    structurals, attrs, constraints = _load_rule_sets(active_file)

    # When a custom file is used, also load the bundled file and merge so
    # that built-in rules are never lost and custom additions are included.
    if active_file != bundled_file and os.path.isfile(bundled_file):
        b_structurals, b_attrs, b_constraints = _load_rule_sets(bundled_file)
        structurals = structurals | b_structurals
        attrs = attrs | b_attrs
        constraints = constraints | b_constraints

    # Merge with defaults
    RULE_STRUCTURAL_KEYWORDS = structurals if structurals else DEFAULT_STRUCTURAL_KEYWORDS
    RULE_ATTRIBUTES = attrs if attrs else DEFAULT_ATTRIBUTE_KEYWORDS

    # Merge parsed constraints with default superset
    if constraints:
        CONSTRAINT_KEYWORDS = constraints | DEFAULT_CONSTRAINT_KEYWORDS
    else:
        CONSTRAINT_KEYWORDS = set(DEFAULT_CONSTRAINT_KEYWORDS)

    # Update the core module's globals so all modules use the same sets.
    # IMPORTANT: use in-place mutation (.clear() + .update()) rather than
    # reassignment so that modules that imported the set object directly
    # (e.g. `from .constants import CONSTRAINT_KEYWORDS`) see the updated
    # contents.  Reassignment only updates the attribute on the constants
    # module but leaves the imported references pointing at the old set.
    try:
        from . import core
        core.constants.RULE_STRUCTURAL_KEYWORDS.clear()
        core.constants.RULE_STRUCTURAL_KEYWORDS.update(RULE_STRUCTURAL_KEYWORDS)
        core.constants.RULE_ATTRIBUTES.clear()
        core.constants.RULE_ATTRIBUTES.update(RULE_ATTRIBUTES)
        core.constants.CONSTRAINT_KEYWORDS.clear()
        core.constants.CONSTRAINT_KEYWORDS.update(CONSTRAINT_KEYWORDS)
    except (ImportError, AttributeError):
        # Running as script - core modules will use their own defaults
        pass


# Initialize rule sets on module load
_initialize_rule_sets()


def validate_environment() -> bool:
    """
    Validate that required dependencies are available.
    
    Returns:
        True if all dependencies are installed, False otherwise
    """
    try:
        import pyang
        import deepdiff
        return True
    except ImportError as e:
        print(f"❌ Missing required dependency: {e}")
        print("\n📦 Please install required packages:")
        print("   pip install pyang deepdiff")
        print("\nOr install the full package:")
        print("   pip install -e .")
        return False


def main(file1: str, file2: str, modules_dir1: str, modules_dir2: str, 
         output_file: str = "output/report.txt"):
    """
    Main entry point for the YANG comparator.
    
    Args:
        file1: Name of the first YANG module file
        file2: Name of the second YANG module file
        modules_dir1: Directory containing the first module and dependencies
        modules_dir2: Directory containing the second module and dependencies
        output_file: Path to output report file
    """
    # Import here to avoid circular imports
    try:
        from .core import YANGNodeComparator
        from .report.report_generator import ReportGenerator
    except ImportError:
        from core import YANGNodeComparator
        from report_generator import ReportGenerator
    
    # Validate environment
    if not validate_environment():
        sys.exit(1)
    
    try:
        print("=" * 60)
        print("YANG Module Comparator - Refactored Version")
        print("=" * 60)
        
        comparator = YANGNodeComparator()
        
        # Load old version first and cache typedef definitions
        comparator.typedef_handler.set_loading_version("old")
        nodes1 = comparator.load_nodes_from_module(file1, modules_dir1)
        
        # Load new version and cache typedef definitions separately
        comparator.typedef_handler.set_loading_version("new")
        nodes2 = comparator.load_nodes_from_module(file2, modules_dir2)
        
        print(f"\n🔍 Comparing modules...")
        print(f"   Module 1: {len(nodes1)} nodes")
        print(f"   Module 2: {len(nodes2)} nodes")
        
        results = comparator.compare_nodes(nodes1, nodes2, similarity_threshold=48.0)
        
        # Count meaningful changes
        meaningful_changes = sum(
            len([c for c in changes if c.is_meaningful()]) 
            for changes in results.values()
        )
        
        print(f"   Found changes in {len(results)} paths")
        print(f"   Total meaningful changes: {meaningful_changes}")
        
        # Extract module name from file1 to prefix paths
        module_name = os.path.splitext(file1)[0]
        
        print(f"\n📄 Generating report...")
        ReportGenerator.generate_report(results, output_file, module_prefix=module_name)
        print(f"   Report saved to: {os.path.abspath(output_file)}")
        
        print(f"\n✅ Comparison completed successfully!")
        
        if meaningful_changes == 0:
            print("🎉 No differences found between the modules!")
        else:
            print(f"📊 Summary: {meaningful_changes} changes detected")
        
    except KeyboardInterrupt:
        print("\n⚠️  Operation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error during comparison: {e}")
        print(f"   Error type: {type(e).__name__}")
        print("\n🔧 Troubleshooting tips:")
        print("  1. Ensure all YANG files exist in the specified directories")
        print("  2. Check that module names match file names (case-sensitive)")
        print("  3. Verify that all imported/included modules are available")
        print("  4. Make sure you have write permissions for the output directory")
        print("\n📋 Full error trace:")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("YANG Module Comparator - Refactored Version")
        print("=" * 60)
        print("Usage: python yang_comparator.py <yang_file1> <yang_file2> \\")
        print("                                  <modules_dir1> <modules_dir2>")
        print()
        print("Parameters:")
        print("  yang_file1   - Name of the first YANG module file")
        print("                 (e.g., 'openconfig-platform.yang')")
        print("  yang_file2   - Name of the second YANG module file")
        print("  modules_dir1 - Directory containing the first module and dependencies")
        print("  modules_dir2 - Directory containing the second module and dependencies")
        print()
        print("Example:")
        print("  python yang_comparator.py openconfig-platform.yang \\")
        print("                            openconfig-platform.yang \\")
        print("                            ./old_modules ./new_modules")
        print()
        print("Features:")
        print("  • Enhanced dependency checking")
        print("  • Comprehensive error handling")
        print("  • Detailed hierarchical reports")
        print("  • Modular architecture with core components")
        print()
        sys.exit(1)
    
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
