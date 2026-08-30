"""
Integration module for recursive expansion in the YANG comparison tool.

This module integrates the recursive expander with the existing comparison workflow.
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from .recursive_expander import RecursiveExpander, should_expand_recursively

logger = logging.getLogger(__name__)


def enhance_comparison_with_recursive_expansion(
    old_yang_file: str,
    new_yang_file: str,
    comparison_result: Dict,
    old_parsed_data: Dict = None,
    new_parsed_data: Dict = None
) -> Dict:
    """
    Enhance a comparison result with recursive expansion for elements marked as recursive.
    
    Args:
        old_yang_file: Path to old YANG file
        new_yang_file: Path to new YANG file
        comparison_result: The existing comparison result dictionary
        old_parsed_data: Pre-parsed old YANG data (optional, will parse if not provided)
        new_parsed_data: Pre-parsed new YANG data (optional, will parse if not provided)
    
    Returns:
        Enhanced comparison result with recursive expansions added
    """
    try:
        # Load parsed data if not provided
        if not old_parsed_data:
            old_parsed_data = _load_yang_json(old_yang_file)
        if not new_parsed_data:
            new_parsed_data = _load_yang_json(new_yang_file)
        
        if not old_parsed_data or not new_parsed_data:
            logger.warning("Could not load YANG data for recursive expansion")
            return comparison_result
        
        # Create expander
        expander = RecursiveExpander(
            old_parsed_data,
            new_parsed_data,
            old_yang_file,
            new_yang_file
        )
        
        # Process both compatible and non-compatible changes
        for compat_key in ['compatible', 'non_compatible']:
            if compat_key not in comparison_result:
                continue
            
            enhanced_items = []
            
            for item in comparison_result[compat_key]:
                keyword = item.get('keyword', '')
                action = item.get('action', '')
                
                # Check if this keyword should be expanded recursively
                if should_expand_recursively(keyword) and action == 'changed':
                    # This is a uses statement that changed
                    enhanced_item = _expand_uses_change(item, expander, old_parsed_data, new_parsed_data)
                    enhanced_items.append(enhanced_item)
                else:
                    # Keep original item
                    enhanced_items.append(item)
            
            comparison_result[compat_key] = enhanced_items
        
        return comparison_result
    
    except Exception as e:
        logger.error(f"Error in recursive expansion: {e}", exc_info=True)
        return comparison_result


def _load_yang_json(yang_file: str) -> Optional[Dict]:
    """
    Load YANG JSON representation.
    
    Attempts to find the JSON file corresponding to the YANG file.
    Looks for:
    1. {filename}_graph.json
    2. {filename}.json
    3. In data/ subdirectory
    
    Args:
        yang_file: Path to YANG file
    
    Returns:
        Parsed JSON data, or None if not found
    """
    yang_path = Path(yang_file)
    
    # Try various locations
    candidates = [
        yang_path.with_suffix('.json'),
        yang_path.parent / 'data' / f"{yang_path.stem}_graph.json",
        yang_path.parent / 'data' / f"{yang_path.stem}.json",
        yang_path.parent.parent / 'data' / f"{yang_path.stem}_graph.json",
    ]
    
    for candidate in candidates:
        if candidate.exists():
            try:
                with open(candidate, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load JSON from {candidate}: {e}")
    
    logger.info(f"No JSON data found for {yang_file}")
    return None


def _find_uses_in_data(data: Dict, path: str) -> Optional[Dict]:
    """
    Find a specific uses statement in the parsed data by path.
    
    Args:
        data: Parsed YANG data
        path: Path to the uses statement (e.g., "module/container/uses")
    
    Returns:
        The uses statement dict, or None if not found
    """
    parts = path.strip('/').split('/')
    current = data
    
    for part in parts:
        if not isinstance(current, dict):
            return None
        
        # Handle array index notation (e.g., "leaf[0]")
        if '[' in part and ']' in part:
            key = part[:part.index('[')]
            index = int(part[part.index('[')+1:part.index(']')])
            
            if key in current:
                items = current[key]
                if isinstance(items, list) and 0 <= index < len(items):
                    current = items[index]
                else:
                    return None
            else:
                return None
        else:
            if part in current:
                current = current[part]
            else:
                return None
    
    return current


def _expand_uses_change(item: Dict, expander: RecursiveExpander, old_data: Dict, new_data: Dict) -> Dict:
    """
    Expand a uses statement change to show the actual structural differences.
    
    Args:
        item: The comparison item for the uses statement
        expander: RecursiveExpander instance
        old_data: Old parsed YANG data
        new_data: New parsed YANG data
    
    Returns:
        Enhanced item with expansion details
    """
    path = item.get('path', '')
    
    # Try to find the uses statements in the data
    old_uses = _find_uses_in_data(old_data, path)
    new_uses = _find_uses_in_data(new_data, path)
    
    if not old_uses or not new_uses:
        # Could not find the uses statements, return original
        logger.warning(f"Could not find uses statements for path: {path}")
        item['expansion_note'] = 'Could not locate uses statements for expansion'
        return item
    
    # Perform the expansion comparison
    comparison = expander.compare_uses_statements(old_uses, new_uses)
    
    # Add expansion details to the item
    item['recursive_expansion'] = {
        'old_grouping': comparison['old_expansion']['grouping_name'],
        'new_grouping': comparison['new_expansion']['grouping_name'],
        'structurally_equivalent': comparison['structurally_equivalent'],
        'added_elements_count': len(comparison['added_elements']),
        'removed_elements_count': len(comparison['removed_elements']),
        'changed_elements_count': len(comparison['changed_elements']),
        'added_elements': comparison['added_elements'][:5],  # First 5
        'removed_elements': comparison['removed_elements'][:5],  # First 5
        'changed_elements': comparison['changed_elements'][:5],  # First 5
    }
    
    # Generate detailed report
    expansion_report = expander.generate_expansion_report(comparison)
    item['expansion_report'] = expansion_report
    
    # Update the action description to be more informative
    if comparison['structurally_equivalent']:
        item['action'] = f"changed (grouping renamed: {comparison['old_expansion']['grouping_name']} → {comparison['new_expansion']['grouping_name']}, structure equivalent)"
        # If structures are equivalent, this is actually compatible
        item['recursive_compatibility'] = 'backward-compatible'
    else:
        removed_count = len(comparison['removed_elements'])
        added_count = len(comparison['added_elements'])
        changed_count = len(comparison['changed_elements'])
        
        item['action'] = f"changed (grouping: {comparison['old_expansion']['grouping_name']} → {comparison['new_expansion']['grouping_name']}, -{removed_count}/+{added_count}/~{changed_count} elements)"
        
        # Determine compatibility based on changes
        if removed_count > 0 or changed_count > 0:
            item['recursive_compatibility'] = 'non-backward-compatible'
        else:
            item['recursive_compatibility'] = 'backward-compatible'
    
    return item


def create_recursive_expansion_report(comparison_result: Dict, output_file: str = None) -> str:
    """
    Create a detailed report of all recursive expansions in the comparison.
    
    Args:
        comparison_result: The comparison result with recursive expansions
        output_file: Optional file to write the report to
    
    Returns:
        The report as a string
    """
    lines = []
    lines.append("=" * 100)
    lines.append("RECURSIVE EXPANSION REPORT")
    lines.append("=" * 100)
    lines.append("")
    
    expansion_count = 0
    
    for compat_key in ['compatible', 'non_compatible']:
        if compat_key not in comparison_result:
            continue
        
        section_title = "BACKWARD COMPATIBLE CHANGES" if compat_key == 'compatible' else "NON-BACKWARD COMPATIBLE CHANGES"
        lines.append(f"\n{'=' * 100}")
        lines.append(section_title)
        lines.append('=' * 100)
        
        for item in comparison_result[compat_key]:
            if 'recursive_expansion' not in item:
                continue
            
            expansion_count += 1
            expansion = item['recursive_expansion']
            
            lines.append(f"\n{'-' * 100}")
            lines.append(f"Path: {item.get('path', 'N/A')}")
            lines.append(f"Keyword: {item.get('keyword', 'N/A')}")
            lines.append(f"Action: {item.get('action', 'N/A')}")
            lines.append(f"{'-' * 100}")
            
            lines.append(f"\nOld Grouping: {expansion['old_grouping']}")
            lines.append(f"New Grouping: {expansion['new_grouping']}")
            lines.append(f"Structurally Equivalent: {expansion['structurally_equivalent']}")
            lines.append(f"Recursive Compatibility: {item.get('recursive_compatibility', 'N/A')}")
            
            if expansion['added_elements_count'] > 0:
                lines.append(f"\n  ✅ Added Elements ({expansion['added_elements_count']} total):")
                for elem in expansion['added_elements']:
                    lines.append(f"     + {elem['keyword']}: {elem['name']} (type: {elem.get('type', 'N/A')})")
            
            if expansion['removed_elements_count'] > 0:
                lines.append(f"\n  ❌ Removed Elements ({expansion['removed_elements_count']} total):")
                for elem in expansion['removed_elements']:
                    lines.append(f"     - {elem['keyword']}: {elem['name']} (type: {elem.get('type', 'N/A')})")
            
            if expansion['changed_elements_count'] > 0:
                lines.append(f"\n  🔄 Changed Elements ({expansion['changed_elements_count']} total):")
                for change in expansion['changed_elements']:
                    elem_key = change['element']
                    lines.append(f"     ~ {elem_key[0]}: {elem_key[1]}")
                    for prop, vals in change['changes'].items():
                        lines.append(f"         {prop}: {vals['old']} → {vals['new']}")
            
            # Add the full expansion report if available
            if 'expansion_report' in item:
                lines.append(f"\n{item['expansion_report']}")
    
    lines.append(f"\n{'=' * 100}")
    lines.append(f"SUMMARY: {expansion_count} recursive expansions processed")
    lines.append('=' * 100)
    
    report = '\n'.join(lines)
    
    if output_file:
        try:
            with open(output_file, 'w') as f:
                f.write(report)
            logger.info(f"Recursive expansion report written to {output_file}")
        except Exception as e:
            logger.error(f"Could not write report to {output_file}: {e}")
    
    return report
