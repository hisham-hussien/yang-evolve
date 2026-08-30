"""
Post-process the final report to expand uses statements marked with recursive="true".

This script takes a final_report.json and expands any 'uses' statements to show
the actual inner elements that changed, making the report more informative.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Import the recursive expansion modules
try:
    from .recursive_expander import RecursiveExpander, should_expand_recursively
    from .pyang_utils import parse_yang_with_pyang
except ImportError:
    from recursive_expander import RecursiveExpander, should_expand_recursively
    from pyang_utils import parse_yang_with_pyang

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def expand_uses_in_report(
    report_data: dict,
    old_yang_file: str,
    new_yang_file: str,
    old_dir: str,
    new_dir: str
) -> dict:
    """
    Expand uses statements in the final report to show actual structural changes.
    
    Args:
        report_data: The final_report.json data
        old_yang_file: Path to old YANG file
        new_yang_file: Path to new YANG file
        old_dir: Directory containing old dependencies
        new_dir: Directory containing new dependencies
        
    Returns:
        Enhanced report with expanded uses statements
    """
    try:
        # Parse the YANG files to get their structure
        logger.info(f"Parsing old YANG file: {old_yang_file}")
        old_parsed = parse_yang_with_pyang(old_yang_file, old_dir)
        
        logger.info(f"Parsing new YANG file: {new_yang_file}")
        new_parsed = parse_yang_with_pyang(new_yang_file, new_dir)
        
        if not old_parsed or not new_parsed:
            logger.warning("Could not parse YANG files, skipping recursive expansion")
            return report_data
        
        # Create expander
        expander = RecursiveExpander(
            old_parsed,
            new_parsed,
            old_yang_file,
            new_yang_file
        )
        
        # Process both compatible and non-compatible changes
        for compat_key in ['compatible', 'non_compatible']:
            if compat_key not in report_data:
                continue
            
            expanded_items = []
            
            for item in report_data[compat_key]:
                keyword = item.get('keyword', '')
                action = item.get('action', '')
                
                # Check if this is a uses statement that should be expanded
                if keyword == 'uses' and should_expand_recursively(keyword):
                    logger.info(f"Expanding uses statement at path: {item.get('path', 'N/A')}")
                    
                    # Extract grouping name from path
                    # Path format: "module/path/to/grouping-name"
                    path = item.get('path', '')
                    if '/' in path:
                        grouping_name = path.split('/')[-1]  # Last segment is the grouping name
                    else:
                        grouping_name = path
                    
                    # For changed uses statements, both old and new use the same grouping name
                    # (the grouping definition itself changed, not which grouping is used)
                    old_grouping_name = grouping_name
                    new_grouping_name = grouping_name
                    
                    # Check if the grouping name actually changed (rare case)
                    if 'attributes' in item:
                        for attr in item['attributes']:
                            if attr.get('attribute') == 'name':
                                if 'old_value' in attr:
                                    old_grouping_name = attr['old_value']
                                if 'new_value' in attr:
                                    new_grouping_name = attr['new_value']
                                elif 'value' in attr:
                                    new_grouping_name = attr['value']
                    
                    if not old_grouping_name or not new_grouping_name:
                        logger.warning(f"Could not extract grouping names from uses at {item.get('path')}")
                        expanded_items.append(item)
                        continue
                    
                    # Create synthetic uses nodes for comparison
                    old_uses = {'name': old_grouping_name}
                    new_uses = {'name': new_grouping_name}
                    
                    # Compare the uses statements
                    try:
                        comparison = expander.compare_uses_statements(old_uses, new_uses)
                        
                        # Create parent uses item
                        parent_item = item.copy()
                        parent_item['expanded'] = True
                        parent_item['expansion_summary'] = {
                            'old_grouping': old_grouping_name,
                            'new_grouping': new_grouping_name,
                            'structurally_equivalent': comparison['structurally_equivalent'],
                            'elements_removed': len(comparison['removed_elements']),
                            'elements_added': len(comparison['added_elements']),
                            'elements_changed': len(comparison['changed_elements'])
                        }
                        
                        # Add children for each removed, added, or changed element
                        children = []
                        
                        # Removed elements (breaking changes)
                        for elem in comparison['removed_elements']:
                            child = {
                                'level': f"{item.get('level', '0')}.1",
                                'path': f"{item.get('path', '')}/{elem['keyword']}:{elem['name']}",
                                'keyword': elem['keyword'],
                                'action': 'deleted',
                                'from_uses_expansion': True,
                                'parent_uses': old_grouping_name,
                                'attributes': [
                                    {
                                        'attribute': 'name',
                                        'value': elem['name']
                                    }
                                ]
                            }
                            if elem.get('type'):
                                child['attributes'].append({
                                    'attribute': 'type',
                                    'value': elem['type']
                                })
                            children.append(child)
                        
                        # Added elements (usually compatible)
                        for elem in comparison['added_elements']:
                            child = {
                                'level': f"{item.get('level', '0')}.2",
                                'path': f"{item.get('path', '')}/{elem['keyword']}:{elem['name']}",
                                'keyword': elem['keyword'],
                                'action': 'added',
                                'from_uses_expansion': True,
                                'parent_uses': new_grouping_name,
                                'attributes': [
                                    {
                                        'attribute': 'name',
                                        'value': elem['name']
                                    }
                                ]
                            }
                            if elem.get('type'):
                                child['attributes'].append({
                                    'attribute': 'type',
                                    'value': elem['type']
                                })
                            children.append(child)
                        
                        # Changed elements
                        for change in comparison['changed_elements']:
                            elem_key = change['element']
                            child = {
                                'level': f"{item.get('level', '0')}.3",
                                'path': f"{item.get('path', '')}/{elem_key[0]}:{elem_key[1]}",
                                'keyword': elem_key[0],
                                'action': 'changed',
                                'from_uses_expansion': True,
                                'parent_uses': f"{old_grouping_name} → {new_grouping_name}",
                                'attributes': []
                            }
                            
                            for prop, vals in change['changes'].items():
                                child['attributes'].append({
                                    'attribute': prop,
                                    'old_value': vals['old'],
                                    'new_value': vals['new'],
                                    'action': 'changed'
                                })
                            
                            children.append(child)
                        
                        # Add children to parent
                        if children:
                            parent_item['expanded_children'] = children
                            
                            # Determine compatibility based on children
                            has_breaking_changes = (
                                len(comparison['removed_elements']) > 0 or
                                len(comparison['changed_elements']) > 0
                            )
                            
                            parent_item['recursive_compatibility'] = (
                                'non-backward-compatible' if has_breaking_changes 
                                else 'backward-compatible'
                            )
                            
                            # If this uses is currently in compatible but has breaking changes,
                            # note that it should be in non_compatible
                            if has_breaking_changes and compat_key == 'compatible':
                                parent_item['warning'] = (
                                    'This uses statement should be non-backward-compatible '
                                    'due to removed or changed inner elements'
                                )
                        
                        expanded_items.append(parent_item)
                        
                        # Also add the children as separate items in the appropriate section
                        for child in children:
                            if child['action'] == 'deleted' or (child['action'] == 'changed'):
                                # These should go to non_compatible
                                if compat_key == 'compatible':
                                    # Move to non_compatible later
                                    child['_should_be_non_compatible'] = True
                            expanded_items.append(child)
                        
                        logger.info(f"Expanded uses statement: {len(children)} inner changes found")
                    
                    except Exception as e:
                        logger.error(f"Error expanding uses statement: {e}", exc_info=True)
                        expanded_items.append(item)
                else:
                    # Keep original item
                    expanded_items.append(item)
            
            report_data[compat_key] = expanded_items
        
        # Move items that should be in non_compatible
        items_to_move = []
        report_data['compatible'] = [
            item for item in report_data.get('compatible', [])
            if not item.get('_should_be_non_compatible', False) or 
               items_to_move.append(item) is None  # Collect and filter
        ][::-1]  # Reverse to fix the order
        
        # Actually move them
        items_to_move_filtered = [
            item for item in report_data.get('compatible', [])
            if item.get('_should_be_non_compatible', False)
        ]
        report_data['compatible'] = [
            item for item in report_data.get('compatible', [])
            if not item.get('_should_be_non_compatible', False)
        ]
        for item in items_to_move_filtered:
            item.pop('_should_be_non_compatible', None)
            report_data.setdefault('non_compatible', []).append(item)
        
        logger.info("Recursive expansion completed successfully")
        return report_data
    
    except Exception as e:
        logger.error(f"Error in recursive expansion: {e}", exc_info=True)
        return report_data


def main():
    parser = argparse.ArgumentParser(
        description="Post-process final report to expand uses statements recursively"
    )
    parser.add_argument("--report", required=True, help="Path to final_report.json")
    parser.add_argument("--old-yang", required=True, help="Old YANG file")
    parser.add_argument("--new-yang", required=True, help="New YANG file")
    parser.add_argument("--old-dir", required=True, help="Directory with old dependencies")
    parser.add_argument("--new-dir", required=True, help="Directory with new dependencies")
    parser.add_argument("--output", help="Output path (default: overwrites input)")
    
    args = parser.parse_args()
    
    # Load the report
    with open(args.report, 'r') as f:
        report_data = json.load(f)
    
    # Expand uses statements
    expanded_report = expand_uses_in_report(
        report_data,
        args.old_yang,
        args.new_yang,
        args.old_dir,
        args.new_dir
    )
    
    # Write output
    output_path = args.output or args.report
    with open(output_path, 'w') as f:
        json.dump(expanded_report, f, indent=2, ensure_ascii=False)
    
    print(f"[expand_uses_report] Expanded report written to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
