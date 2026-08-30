"""
Recursive expansion module for YANG comparison tool.

Handles recursive expansion of YANG statements (like 'uses' with groupings)
to compare actual content rather than just statement names.

Features:
- Expands grouping references recursively
- Resolves cross-file references using prefixes
- Detects semantic equivalence between different groupings
- Generates detailed expansion reports
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
import logging

logger = logging.getLogger(__name__)


class RecursiveExpander:
    """
    Expands recursive YANG elements (like uses/grouping) for deep comparison.
    """
    
    def __init__(self, old_data: Dict, new_data: Dict, old_file_path: str = None, new_file_path: str = None):
        """
        Initialize the expander with old and new YANG data structures.
        
        Args:
            old_data: Parsed old YANG file structure
            new_data: Parsed new YANG file structure
            old_file_path: Path to old YANG file (for resolving imports)
            new_file_path: Path to new YANG file (for resolving imports)
        """
        self.old_data = old_data
        self.new_data = new_data
        self.old_file_path = Path(old_file_path) if old_file_path else None
        self.new_file_path = Path(new_file_path) if new_file_path else None
        
        # Cache for resolved groupings to avoid infinite recursion
        self.old_grouping_cache: Dict[str, Dict] = {}
        self.new_grouping_cache: Dict[str, Dict] = {}
        
        # Track visited groupings to detect circular references
        self.old_visited: Set[str] = set()
        self.new_visited: Set[str] = set()
        
        # Import prefix mappings
        self.old_imports: Dict[str, str] = {}  # prefix -> module_name
        self.new_imports: Dict[str, str] = {}
        
        self._extract_imports()
        self._cache_groupings()
    
    def _extract_imports(self):
        """Extract import statements to build prefix-to-module mappings."""
        # Old file imports
        if 'import' in self.old_data:
            imports = self.old_data['import']
            if not isinstance(imports, list):
                imports = [imports]
            for imp in imports:
                module_name = imp.get('name', '')
                prefix = imp.get('prefix', {}).get('value', '')
                if module_name and prefix:
                    self.old_imports[prefix] = module_name
        
        # New file imports
        if 'import' in self.new_data:
            imports = self.new_data['import']
            if not isinstance(imports, list):
                imports = [imports]
            for imp in imports:
                module_name = imp.get('name', '')
                prefix = imp.get('prefix', {}).get('value', '')
                if module_name and prefix:
                    self.new_imports[prefix] = module_name
    
    def _cache_groupings(self):
        """Cache all groupings from both files for quick lookup."""
        # Cache old groupings
        self._cache_groupings_recursive(self.old_data, self.old_grouping_cache)
        
        # Cache new groupings
        self._cache_groupings_recursive(self.new_data, self.new_grouping_cache)
    
    def _cache_groupings_recursive(self, node: Dict, cache: Dict, parent_path: str = ""):
        """Recursively cache all groupings in the tree."""
        if not isinstance(node, dict):
            return
        
        # If this is a grouping, cache it
        if 'name' in node and parent_path.endswith('/grouping'):
            grouping_name = node['name'].get('value') if isinstance(node['name'], dict) else node['name']
            cache[grouping_name] = node
        
        # Recurse into children
        for key, value in node.items():
            if key == 'grouping':
                # Handle both single grouping and list of groupings
                groupings = value if isinstance(value, list) else [value]
                for grouping in groupings:
                    if isinstance(grouping, dict) and 'name' in grouping:
                        name = grouping['name'].get('value') if isinstance(grouping['name'], dict) else grouping['name']
                        cache[name] = grouping
            elif isinstance(value, dict):
                self._cache_groupings_recursive(value, cache, f"{parent_path}/{key}")
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        self._cache_groupings_recursive(item, cache, f"{parent_path}/{key}")
    
    def _resolve_grouping_reference(self, uses_name: str, is_old: bool) -> Optional[Dict]:
        """
        Resolve a grouping reference (possibly with prefix) to its definition.
        
        Args:
            uses_name: The grouping name (may include prefix like "oc-if:interface-config")
            is_old: Whether to search in old (True) or new (False) file
        
        Returns:
            The grouping definition dict, or None if not found
        """
        cache = self.old_grouping_cache if is_old else self.new_grouping_cache
        imports = self.old_imports if is_old else self.new_imports
        file_path = self.old_file_path if is_old else self.new_file_path
        
        # Check if it's a prefixed reference
        if ':' in uses_name:
            prefix, local_name = uses_name.split(':', 1)
            
            # If prefix refers to an import, we'd need to load that file
            if prefix in imports:
                module_name = imports[prefix]
                logger.info(f"Cross-file reference detected: {prefix}:{local_name} -> module {module_name}")
                
                # Try to find and load the imported file
                if file_path:
                    imported_file = self._find_imported_file(module_name, file_path.parent)
                    if imported_file:
                        imported_grouping = self._load_external_grouping(imported_file, local_name)
                        if imported_grouping:
                            return imported_grouping
                
                logger.warning(f"Could not resolve cross-file grouping: {uses_name}")
                return None
            else:
                # Prefix not in imports, treat as local
                uses_name = local_name
        
        # Look up in local cache
        return cache.get(uses_name)
    
    def _find_imported_file(self, module_name: str, search_dir: Path) -> Optional[Path]:
        """
        Find the YANG file for an imported module.
        
        Args:
            module_name: Name of the module to find
            search_dir: Directory to search in
        
        Returns:
            Path to the module file, or None if not found
        """
        # Try standard naming: module_name.yang
        candidate = search_dir / f"{module_name}.yang"
        if candidate.exists():
            return candidate
        
        # Try searching parent directories
        for parent in [search_dir, search_dir.parent, search_dir.parent.parent]:
            candidate = parent / f"{module_name}.yang"
            if candidate.exists():
                return candidate
            
            # Try in subfolders
            for yang_file in parent.rglob(f"{module_name}.yang"):
                return yang_file
        
        return None
    
    def _load_external_grouping(self, file_path: Path, grouping_name: str) -> Optional[Dict]:
        """
        Load a grouping definition from an external file.
        
        Args:
            file_path: Path to the YANG file
            grouping_name: Name of the grouping to find
        
        Returns:
            The grouping definition, or None if not found
        """
        try:
            # We need to parse the external file
            # For now, return None - this requires integration with the main parser
            logger.info(f"External grouping loading not yet implemented: {file_path} -> {grouping_name}")
            return None
        except Exception as e:
            logger.error(f"Error loading external grouping from {file_path}: {e}")
            return None
    
    def expand_uses(self, uses_node: Dict, is_old: bool, max_depth: int = 10) -> Dict:
        """
        Expand a 'uses' statement recursively to get the full structure.
        
        Args:
            uses_node: The 'uses' statement node
            is_old: Whether this is from old (True) or new (False) file
            max_depth: Maximum recursion depth to prevent infinite loops
        
        Returns:
            Dictionary with expanded structure:
            {
                'grouping_name': str,
                'resolved': bool,
                'elements': [list of expanded elements],
                'nested_uses': [list of nested uses statements],
                'signature': str  # for structural comparison
            }
        """
        if max_depth <= 0:
            logger.warning("Maximum recursion depth reached in expand_uses")
            return {
                'grouping_name': 'MAX_DEPTH_REACHED',
                'resolved': False,
                'elements': [],
                'nested_uses': [],
                'signature': 'MAX_DEPTH'
            }
        
        visited = self.old_visited if is_old else self.new_visited
        
        # Extract grouping name from uses statement
        grouping_name = uses_node.get('name', {})
        if isinstance(grouping_name, dict):
            grouping_name = grouping_name.get('value', '')
        
        # Check for circular reference
        if grouping_name in visited:
            logger.warning(f"Circular reference detected: {grouping_name}")
            return {
                'grouping_name': grouping_name,
                'resolved': False,
                'elements': [],
                'nested_uses': [],
                'signature': f'CIRCULAR:{grouping_name}'
            }
        
        # Mark as visited
        visited.add(grouping_name)
        
        try:
            # Resolve the grouping
            grouping_def = self._resolve_grouping_reference(grouping_name, is_old)
            
            if not grouping_def:
                return {
                    'grouping_name': grouping_name,
                    'resolved': False,
                    'elements': [],
                    'nested_uses': [],
                    'signature': f'UNRESOLVED:{grouping_name}'
                }
            
            # Extract structure
            elements = []
            nested_uses = []
            
            # Extract leaf, leaf-list, container, list, etc.
            for keyword in ['leaf', 'leaf-list', 'container', 'list', 'choice', 'anyxml', 'anydata']:
                if keyword in grouping_def:
                    items = grouping_def[keyword]
                    if not isinstance(items, list):
                        items = [items]
                    
                    for item in items:
                        name = item.get('name', {})
                        if isinstance(name, dict):
                            name = name.get('value', '')
                        
                        # Extract type information (can be nested dict or simple string)
                        type_value = None
                        if 'type' in item:
                            type_info = item['type']
                            if isinstance(type_info, dict):
                                # Try to get name from nested structure
                                type_name = type_info.get('name', type_info)
                                if isinstance(type_name, dict):
                                    type_value = type_name.get('value', '')
                                elif isinstance(type_name, str):
                                    type_value = type_name
                            elif isinstance(type_info, str):
                                type_value = type_info
                        
                        # Extract mandatory flag
                        mandatory = False
                        if 'mandatory' in item:
                            mand_info = item['mandatory']
                            if isinstance(mand_info, dict):
                                mandatory = mand_info.get('value', False)
                            elif isinstance(mand_info, bool):
                                mandatory = mand_info
                        
                        # Extract config flag
                        config = True
                        if 'config' in item:
                            config_info = item['config']
                            if isinstance(config_info, dict):
                                config = config_info.get('value', True)
                            elif isinstance(config_info, bool):
                                config = config_info
                        
                        element_info = {
                            'keyword': keyword,
                            'name': name,
                            'type': type_value,
                            'mandatory': mandatory,
                            'config': config
                        }
                        elements.append(element_info)
            
            # Find nested uses statements
            if 'uses' in grouping_def:
                nested_uses_nodes = grouping_def['uses']
                if not isinstance(nested_uses_nodes, list):
                    nested_uses_nodes = [nested_uses_nodes]
                
                for nested_uses_node in nested_uses_nodes:
                    # Recursively expand nested uses
                    expanded_nested = self.expand_uses(nested_uses_node, is_old, max_depth - 1)
                    nested_uses.append(expanded_nested)
                    
                    # Add nested elements to our elements list
                    elements.extend(expanded_nested['elements'])
            
            # Create a structural signature for comparison
            signature = self._create_signature(elements)
            
            return {
                'grouping_name': grouping_name,
                'resolved': True,
                'elements': elements,
                'nested_uses': nested_uses,
                'signature': signature
            }
        
        finally:
            # Remove from visited set
            visited.discard(grouping_name)
    
    def _create_signature(self, elements: List[Dict]) -> str:
        """
        Create a structural signature for a list of elements.
        Used to compare if two different groupings have the same structure.
        
        Args:
            elements: List of element dictionaries
        
        Returns:
            A signature string representing the structure
        """
        # Sort elements by name for consistent comparison
        sorted_elements = sorted(elements, key=lambda x: (x['keyword'], x['name']))
        
        # Create signature: keyword:name:type
        parts = []
        for elem in sorted_elements:
            type_str = elem.get('type', 'none')
            mandatory_str = 'M' if elem.get('mandatory') else 'O'
            config_str = 'C' if elem.get('config') else 'S'
            parts.append(f"{elem['keyword']}:{elem['name']}:{type_str}:{mandatory_str}:{config_str}")
        
        return '|'.join(parts)
    
    def compare_uses_statements(self, old_uses: Dict, new_uses: Dict) -> Dict:
        """
        Compare two uses statements by expanding and comparing their structures.
        
        Args:
            old_uses: The uses statement from old file
            new_uses: The uses statement from new file
        
        Returns:
            Comparison result:
            {
                'structurally_equivalent': bool,
                'old_expansion': dict,
                'new_expansion': dict,
                'added_elements': list,
                'removed_elements': list,
                'changed_elements': list
            }
        """
        # Expand both uses statements
        old_expanded = self.expand_uses(old_uses, is_old=True)
        new_expanded = self.expand_uses(new_uses, is_old=False)
        
        # Check if both were resolved
        if not old_expanded['resolved'] or not new_expanded['resolved']:
            return {
                'structurally_equivalent': False,
                'old_expansion': old_expanded,
                'new_expansion': new_expanded,
                'added_elements': [],
                'removed_elements': [],
                'changed_elements': [],
                'comparison_status': 'UNRESOLVED'
            }
        
        # Compare signatures
        structurally_equivalent = (old_expanded['signature'] == new_expanded['signature'])
        
        # Detailed element comparison
        old_elements_map = {(e['keyword'], e['name']): e for e in old_expanded['elements']}
        new_elements_map = {(e['keyword'], e['name']): e for e in new_expanded['elements']}
        
        old_keys = set(old_elements_map.keys())
        new_keys = set(new_elements_map.keys())
        
        added_elements = [new_elements_map[k] for k in (new_keys - old_keys)]
        removed_elements = [old_elements_map[k] for k in (old_keys - new_keys)]
        
        changed_elements = []
        for key in (old_keys & new_keys):
            old_elem = old_elements_map[key]
            new_elem = new_elements_map[key]
            
            # Check if any properties changed
            changes = {}
            if old_elem.get('type') != new_elem.get('type'):
                changes['type'] = {'old': old_elem.get('type'), 'new': new_elem.get('type')}
            if old_elem.get('mandatory') != new_elem.get('mandatory'):
                changes['mandatory'] = {'old': old_elem.get('mandatory'), 'new': new_elem.get('mandatory')}
            if old_elem.get('config') != new_elem.get('config'):
                changes['config'] = {'old': old_elem.get('config'), 'new': new_elem.get('config')}
            
            if changes:
                changed_elements.append({
                    'element': key,
                    'changes': changes
                })
        
        return {
            'structurally_equivalent': structurally_equivalent,
            'old_expansion': old_expanded,
            'new_expansion': new_expanded,
            'added_elements': added_elements,
            'removed_elements': removed_elements,
            'changed_elements': changed_elements,
            'comparison_status': 'RESOLVED'
        }
    
    def generate_expansion_report(self, comparison: Dict) -> str:
        """
        Generate a human-readable report of the uses comparison.
        
        Args:
            comparison: Result from compare_uses_statements
        
        Returns:
            Formatted report string
        """
        lines = []
        lines.append("=" * 80)
        lines.append("RECURSIVE USES EXPANSION REPORT")
        lines.append("=" * 80)
        
        old_exp = comparison['old_expansion']
        new_exp = comparison['new_expansion']
        
        lines.append(f"\nOld grouping: {old_exp['grouping_name']}")
        lines.append(f"New grouping: {new_exp['grouping_name']}")
        lines.append(f"Structurally equivalent: {comparison['structurally_equivalent']}")
        lines.append(f"Comparison status: {comparison['comparison_status']}")
        
        if comparison['comparison_status'] == 'RESOLVED':
            lines.append(f"\nOld structure ({len(old_exp['elements'])} elements):")
            for elem in old_exp['elements'][:10]:  # Show first 10
                lines.append(f"  - {elem['keyword']}: {elem['name']} (type: {elem.get('type', 'N/A')})")
            if len(old_exp['elements']) > 10:
                lines.append(f"  ... and {len(old_exp['elements']) - 10} more")
            
            lines.append(f"\nNew structure ({len(new_exp['elements'])} elements):")
            for elem in new_exp['elements'][:10]:  # Show first 10
                lines.append(f"  - {elem['keyword']}: {elem['name']} (type: {elem.get('type', 'N/A')})")
            if len(new_exp['elements']) > 10:
                lines.append(f"  ... and {len(new_exp['elements']) - 10} more")
            
            if comparison['added_elements']:
                lines.append(f"\n✅ Added elements ({len(comparison['added_elements'])}):")
                for elem in comparison['added_elements'][:5]:
                    lines.append(f"  + {elem['keyword']}: {elem['name']}")
                if len(comparison['added_elements']) > 5:
                    lines.append(f"  ... and {len(comparison['added_elements']) - 5} more")
            
            if comparison['removed_elements']:
                lines.append(f"\n❌ Removed elements ({len(comparison['removed_elements'])}):")
                for elem in comparison['removed_elements'][:5]:
                    lines.append(f"  - {elem['keyword']}: {elem['name']}")
                if len(comparison['removed_elements']) > 5:
                    lines.append(f"  ... and {len(comparison['removed_elements']) - 5} more")
            
            if comparison['changed_elements']:
                lines.append(f"\n🔄 Changed elements ({len(comparison['changed_elements'])}):")
                for change in comparison['changed_elements'][:5]:
                    elem_key = change['element']
                    lines.append(f"  ~ {elem_key[0]}: {elem_key[1]}")
                    for prop, vals in change['changes'].items():
                        lines.append(f"      {prop}: {vals['old']} → {vals['new']}")
                if len(comparison['changed_elements']) > 5:
                    lines.append(f"  ... and {len(comparison['changed_elements']) - 5} more")
        
        lines.append("\n" + "=" * 80)
        
        return '\n'.join(lines)


def should_expand_recursively(keyword: str, rules_file: str = None) -> bool:
    """
    Check if a keyword should be expanded recursively based on rules.
    
    Args:
        keyword: The YANG keyword to check
        rules_file: Path to compatibility_rules.xml (optional)
    
    Returns:
        True if the keyword should be expanded recursively
    """
    # Default: only 'uses' is recursive
    if keyword == 'uses':
        return True
    
    # If rules file provided, check it
    if rules_file:
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(rules_file)
            root = tree.getroot()
            
            for rule in root.findall('.//keyword'):
                if rule.text == keyword and rule.get('recursive') == 'true':
                    return True
        except Exception as e:
            logger.error(f"Error reading rules file: {e}")
    
    return False
