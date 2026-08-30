"""
YANG Parser Module
Extracts YANG statements and their structures from .yang files
"""
import re
from typing import Dict, List, Tuple, Optional, Set


class YANGParser:
    """Parser for YANG files to extract statements and their structures"""
    
    # YANG structural keywords - define the structure of the data model
    STRUCTURAL_KEYWORDS: Set[str] = {
        "namespace", "import", "revision", "module", "grouping", "container",
        "uses", "type", "list", "leaf", "augment", "deviation", "leaf-list", 
        "choice", "case", "rpc", "identity", "typedef", "action", "anydata", 
        "anyxml", "notification", "extension", "feature"
    }
    
    # YANG constraint keywords - define restrictions and validation rules
    CONSTRAINT_KEYWORDS: Set[str] = {
        "range", "length", "mandatory", "status", "config", "min-elements",
        "max-elements", "fraction-digits", "must", "when", "pattern",
        "if-feature", "unique", "key", "require-instance", "presence"
    }
    
    # YANG attribute keywords - provide metadata and documentation
    ATTRIBUTE_KEYWORDS: Set[str] = {
        "name", "description", "units", "default", "reference", "path", 
        "value", "base", "ordered-by", "enum", "bit", "input", "output"
    }
    
    # YANG statement keywords to extract (structural ones we want to index)
    YANG_KEYWORDS = [
        'leaf', 'leaf-list', 'list', 'container', 'choice', 'case',
        'typedef', 'grouping', 'uses', 'augment', 'rpc', 'notification',
        'identity', 'extension', 'feature', 'deviation', 'module', 
        'namespace', 'import', 'revision', 'action', 'anydata', 'anyxml'
    ]
    
    # YANG sub-statements (constraints and properties)
    YANG_SUBSTATEMENTS = [
        'type', 'default', 'units', 'description', 'reference',
        'status', 'when', 'if-feature', 'must', 'mandatory',
        'config', 'ordered-by', 'min-elements', 'max-elements',
        'key', 'unique', 'choice', 'pattern', 'length', 'range',
        'fraction-digits', 'enum', 'bit', 'path', 'require-instance',
        'base', 'presence', 'input', 'output'
    ]
    
    def __init__(self):
        self.statements = []
    
    @classmethod
    def classify_keyword(cls, keyword: str) -> str:
        """
        Classify a YANG keyword into its category
        
        Args:
            keyword: The YANG keyword to classify
            
        Returns:
            One of: 'structural', 'constraint', 'attribute', or 'unknown'
        """
        if keyword in cls.STRUCTURAL_KEYWORDS:
            return 'structural'
        elif keyword in cls.CONSTRAINT_KEYWORDS:
            return 'constraint'
        elif keyword in cls.ATTRIBUTE_KEYWORDS:
            return 'attribute'
        else:
            return 'unknown'
    
    @classmethod
    def get_keyword_categories(cls) -> Dict[str, List[str]]:
        """
        Get all keywords organized by category
        
        Returns:
            Dictionary mapping category names to lists of keywords
        """
        return {
            'structural': sorted(list(cls.STRUCTURAL_KEYWORDS)),
            'constraint': sorted(list(cls.CONSTRAINT_KEYWORDS)),
            'attribute': sorted(list(cls.ATTRIBUTE_KEYWORDS))
        }
    
    def parse_file(self, filepath: str) -> List[Dict]:
        """
        Parse a YANG file and extract all statements
        
        Args:
            filepath: Path to the YANG file
            
        Returns:
            List of extracted statement dictionaries
        """
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Remove comments
            content = self._remove_comments(content)
            
            # Extract statements
            statements = self._extract_statements(content, filepath)
            
            return statements
            
        except Exception as e:
            print(f"Error parsing {filepath}: {e}")
            return []
    
    def _remove_comments(self, content: str) -> str:
        """Remove YANG comments (// and /* */)"""
        # Remove single-line comments
        content = re.sub(r'//.*?$', '', content, flags=re.MULTILINE)
        # Remove multi-line comments
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        return content
    
    def _extract_statements(self, content: str, filepath: str) -> List[Dict]:
        """Extract all YANG statements from content"""
        statements = []
        
        for keyword in self.YANG_KEYWORDS:
            # Find all occurrences of this keyword
            pattern = rf'\b{keyword}\s+([^\s{{]+)\s*\{{([^}}]*(?:\{{[^}}]*\}}[^}}]*)*)\}}'
            matches = re.finditer(pattern, content, re.DOTALL)
            
            for match in matches:
                name = match.group(1).strip()
                body = match.group(2).strip()
                full_statement = match.group(0)
                
                # Extract sub-statements
                substatements = self._extract_substatements(body)
                
                # Classify the keyword
                keyword_category = self.classify_keyword(keyword)
                
                # Classify sub-statements
                substatement_categories = {}
                for sub_key in substatements.keys():
                    substatement_categories[sub_key] = self.classify_keyword(sub_key)
                
                statement = {
                    'keyword': keyword,
                    'keyword_category': keyword_category,
                    'name': name,
                    'body': body,
                    'full_statement': full_statement,
                    'substatements': substatements,
                    'substatement_categories': substatement_categories,
                    'source_file': filepath,
                    'line_count': full_statement.count('\n') + 1
                }
                
                statements.append(statement)
        
        return statements
    
    def _extract_substatements(self, body: str) -> Dict[str, List[str]]:
        """Extract sub-statements from a statement body"""
        substatements = {}
        
        for sub_keyword in self.YANG_SUBSTATEMENTS:
            values = []
            seen_values = set()  # Track seen values to avoid duplicates
            
            # Pattern for sub-statement with quoted value (handles multi-line and escaped quotes)
            # Match quoted strings that may span multiple lines
            pattern_quoted = rf'\b{sub_keyword}\s+"((?:[^"\\]|\\.)*)"\s*;'
            
            # Pattern for sub-statement with unquoted value
            pattern_unquoted = rf'\b{sub_keyword}\s+([^\s;{{]+)\s*;'
            
            # Pattern for sub-statement with block
            pattern_block = rf'\b{sub_keyword}\s+([^\s{{]+)\s*\{{([^}}]*)\}}'
            
            # First, try quoted values (PRIORITY: these are the canonical form)
            for match in re.finditer(pattern_quoted, body, re.DOTALL):
                quoted_value = match.group(1)
                # Preserve the quotes in the stored value for consistency
                stored_value = f'"{quoted_value}"'
                if stored_value not in seen_values:
                    values.append(stored_value)
                    seen_values.add(stored_value)
                    # Also track the unquoted version to avoid duplicates
                    seen_values.add(quoted_value)
            
            # Then, try unquoted values (only if not already seen as quoted)
            for match in re.finditer(pattern_unquoted, body):
                unquoted_value = match.group(1).strip()
                # Check if this value was already captured as a quoted value
                quoted_form = f'"{unquoted_value}"'
                if unquoted_value not in seen_values and quoted_form not in seen_values:
                    values.append(unquoted_value)
                    seen_values.add(unquoted_value)
            
            # Finally, try block values
            for match in re.finditer(pattern_block, body, re.DOTALL):
                block_name = match.group(1).strip()
                block_body = match.group(2).strip()
                block_content = f"{block_name} {{ {block_body} }}"
                if block_content not in seen_values:
                    values.append(block_content)
                    seen_values.add(block_content)
            
            if values:
                substatements[sub_keyword] = values
        
        return substatements
    
    def extract_patterns_and_tests(self, statement: Dict) -> Dict[str, List[str]]:
        """Extract pattern tests from a statement"""
        tests = {
            'pattern-test-pass': [],
            'pattern-test-fail': [],
            'patterns': [],
            'lengths': [],
            'ranges': []
        }
        
        body = statement.get('body', '')
        
        # Extract pattern-test-pass
        for match in re.finditer(r'pt:pattern-test-pass\s+"([^"]+)"', body):
            tests['pattern-test-pass'].append(match.group(1))
        
        # Extract pattern-test-fail
        for match in re.finditer(r'pt:pattern-test-fail\s+"([^"]+)"', body):
            tests['pattern-test-fail'].append(match.group(1))
        
        # Extract patterns
        for match in re.finditer(r'pattern\s+"([^"]+)"', body):
            tests['patterns'].append(match.group(1))
        
        # Extract length constraints
        for match in re.finditer(r'length\s+"([^"]+)"', body):
            tests['lengths'].append(match.group(1))
        
        # Extract range constraints
        for match in re.finditer(r'range\s+"([^"]+)"', body):
            tests['ranges'].append(match.group(1))
        
        return tests


def extract_yang_examples(directory: str, min_examples: int = 100) -> Dict[str, List[Dict]]:
    """
    Extract YANG examples from all .yang files in a directory
    
    Args:
        directory: Root directory to search for .yang files
        min_examples: Minimum number of examples to extract per keyword
        
    Returns:
        Dictionary mapping YANG keywords to lists of examples
    """
    import os
    
    parser = YANGParser()
    examples_by_keyword = {keyword: [] for keyword in parser.YANG_KEYWORDS}
    
    print(f"Scanning {directory} for .yang files...")
    
    # Walk through directory
    yang_files = []
    for root, dirs, files in os.walk(directory):
        for filename in files:
            if filename.endswith('.yang'):
                yang_files.append(os.path.join(root, filename))
    
    print(f"Found {len(yang_files)} .yang files")
    
    # Parse each file
    for i, filepath in enumerate(yang_files):
        if i % 100 == 0:
            print(f"Processing file {i+1}/{len(yang_files)}: {filepath}")
        
        statements = parser.parse_file(filepath)
        
        for statement in statements:
            keyword = statement['keyword']
            examples_by_keyword[keyword].append(statement)
    
    # Report statistics
    print("\nExtraction Statistics:")
    print("-" * 60)
    for keyword, examples in examples_by_keyword.items():
        count = len(examples)
        status = "✓" if count >= min_examples else "✗"
        print(f"{status} {keyword:20s}: {count:6d} examples")
    print("-" * 60)
    
    return examples_by_keyword


if __name__ == "__main__":
    # Test the parser
    import sys
    import json
    
    if len(sys.argv) > 1:
        test_file = sys.argv[1]
        parser = YANGParser()
        statements = parser.parse_file(test_file)
        
        print(f"Found {len(statements)} statements in {test_file}")
        
        for stmt in statements[:5]:  # Show first 5
            print("\n" + "="*60)
            print(f"Keyword: {stmt['keyword']}")
            print(f"Name: {stmt['name']}")
            print(f"Substatements: {list(stmt['substatements'].keys())}")
            tests = parser.extract_patterns_and_tests(stmt)
            if any(tests.values()):
                print(f"Tests: {json.dumps({k: v for k, v in tests.items() if v}, indent=2)}")
    else:
        print("Usage: python yang_parser.py <path-to-yang-file>")
