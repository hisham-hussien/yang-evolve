"""
XML Rule Extraction Utilities
------------------------------
Extract and format rules from compatibility_rules.xml.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict
from rich.console import Console

console = Console()


def extract_rules_for_keyword(xml_path: Path, keyword: str) -> List[Dict]:
    """Extract all rules from compatibility_rules.xml that mention a specific keyword.
    
    Args:
        xml_path: Path to compatibility_rules.xml file
        keyword: YANG keyword to search for
        
    Returns:
        List of dictionaries with rule details (rule_id, category, actions, compatible, xml)
    """
    
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        
        matching_rules = []
        
        for rule in root.findall('rule'):
            rule_id = rule.find('rule-id').text if rule.find('rule-id') is not None else "unknown"
            
            # Check if keyword appears in keywords/constraints/attributes
            found = False
            category_type = None
            
            for category in ['structurals', 'keywords', 'constraints', 'attributes']:
                category_elem = rule.find(category)
                if category_elem is not None:
                    for item in category_elem:
                        if item.text and keyword in item.text.lower():
                            found = True
                            category_type = category
                            break
                if found:
                    break
            
            if found:
                # Extract rule details
                actions = []
                for action in rule.findall('.//action'):
                    action_dict = {'text': action.text}
                    if action.get('parent'):
                        action_dict['parent'] = action.get('parent')
                    actions.append(action_dict)
                
                compatible = rule.find('compatible').text if rule.find('compatible') is not None else "unknown"
                
                matching_rules.append({
                    'rule_id': rule_id,
                    'category': category_type,
                    'actions': actions,
                    'compatible': compatible,
                    'xml': ET.tostring(rule, encoding='unicode')
                })
        
        return matching_rules
    
    except Exception as e:
        console.print(f"[red]Error extracting rules: {e}[/red]")
        return []


def format_rules_as_text(rules: List[Dict]) -> str:
    """Format extracted rules as readable text for LLM context.
    
    Args:
        rules: List of rule dictionaries from extract_rules_for_keyword
        
    Returns:
        Formatted text string with rule details
    """
    
    if not rules:
        return "No existing rules found for this keyword."
    
    text_parts = []
    for rule in rules:
        text_parts.append(f"Rule ID: {rule['rule_id']}")
        text_parts.append(f"Category: {rule['category']}")
        
        # Format actions (avoiding nested f-string with backslash)
        actions_list = []
        for a in rule['actions']:
            action_str = a['text']
            if a.get('parent'):
                action_str += f" (parent={a.get('parent')})"
            actions_list.append(action_str)
        text_parts.append(f"Actions: {', '.join(actions_list)}")
        
        text_parts.append(f"Compatible: {rule['compatible']}")
        text_parts.append(f"XML:\n{rule['xml']}")
        text_parts.append("-" * 50)
    
    return "\n".join(text_parts)
