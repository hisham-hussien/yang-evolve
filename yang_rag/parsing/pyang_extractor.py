#!/usr/bin/env python3
"""
Comprehensive YANG Extractor using Pyang
-----------------------------------------
Extracts YANG statements with rich structural metadata for better semantic search.

Schema:
  - module: Module name
  - path: Canonical XPath-like path
  - statement_type: YANG keyword (leaf, container, must, etc.)
  - parent_type: Parent statement type
  - type_info: Type details (for typed statements)
  - description: Statement description
  - search_text: Human-readable search text (for semantic embedding)
  - struct_text: Structural fingerprint (for structural matching)
  -        table = Table()
        table.add_column("Category", style="cyan")
        table.add_column("Statements", style="green", justify="right")
        table.add_column("Percentage", style="yellow", justify="right")
        
        total = sum(category_counts.values())
        
        # Display categories in order: Keywords, Constraints, Attributes, Other
        for cat in ['Keywords', 'Constraints', 'Attributes', 'Other']:
            if cat in category_counts:
                count = category_counts[cat]
                pct = (count / total * 100) if total > 0 else 0
                table.add_row(cat, str(count), f"{pct:.1%}")
        
        table.add_row("[bold]TOTAL[/bold]", f"[bold]{total}[/bold]", "[bold]100.0%[/bold]")
        console.print(table)Various signatures (expression, leafref, etc.)
  - source_file: Origin file
  - line_no: Line number in source

Uses pyang's AST for accurate traversal.
"""

import sys
import json
import hashlib
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from collections import defaultdict

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    from pyang import context, repository, statements
    from pyang.error import error_codes
except ImportError:
    print("ERROR: pyang not installed. Install with: pip install pyang")
    sys.exit(1)

# Import shared YANG utilities
from yang_rag.utils.yang_common import (
    STRUCTURAL_KEYWORDS,
    CONSTRAINT_KEYWORDS,
    ATTRIBUTE_KEYWORDS,
    YANG_BUILTIN_TYPES,
    ALL_KEYWORDS,
    YANGRulesLoader,
    YANGKeywordHelper,
    YANGStatementFilter,
    KEYWORD_XML_TYPES,
    XML_TYPE_TO_MASK,
)

from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.panel import Panel

console = Console()

# Paths
# From yang_rag/parsing/pyang_extractor.py -> go up 3 levels to project root
YANG_DATA_PATH = Path(__file__).parent.parent.parent / "yang_data"
OUTPUT_JSON = Path(__file__).parent.parent.parent / "data" / "yang_pyang_extracted.json"


class PyangYANGExtractor:
    """Extract YANG statements using pyang's AST with deduplication."""
    
    def __init__(self):
        # Create pyang repository and context with proper options
        self.repos = repository.FileRepository(str(YANG_DATA_PATH))
        
        # Create options object (needed for Context)
        class Options:
            def __init__(self):
                self.lint_namespace_prefixes = []
                self.lint_modulename_prefixes = []
        
        opts = Options()
        self.ctx = context.Context(self.repos)
        self.ctx.opts = opts
        
        self.documents = []
        self.stats = defaultdict(int)
        self.doc_id_counter = defaultdict(int)  # Counter for unique IDs
        self.seen_documents = set()  # Track unique document signatures to avoid duplicates
        self.duplicate_count = 0  # Count duplicates for reporting
        self.node_id_counter = 0  # Global node ID counter

        # Diversity tracking for per-keyword cap
        # keyword -> count of accepted documents
        self.keyword_counts: dict = defaultdict(int)
        # keyword -> set of (repo, masked_template) pairs already accepted
        # repo = top-level vendor dir (e.g. 'Cisco', 'Huawei', 'Nokia', 'Juipter')
        # masked_template = the snippet_masked string (captures structural variation)
        self.keyword_diversity: dict = defaultdict(set)
        # Per-keyword cap (set by extract_all via --max-per-keyword)
        self.max_per_keyword: int = 0  # 0 = unlimited (default, backward-compatible)
        
        # Extension registry: module_name -> set of extension names
        self.extension_registry = {}
        self.module_imports = {}  # module_name -> list of imported modules
        
        # Extension description registry: (module_name, extension_name) -> description
        self.extension_descriptions = {}
        
        # Typedef registry: module_name -> set of typedef names
        self.typedef_registry = {}  # Track locally defined typedefs
        
        # Load allowed keywords/attributes/constraints from compatibility_rules.xml
        self.allowed_statements = YANGRulesLoader.load_compatibility_rules()
        
        # Create statement filter for extraction decisions
        self.statement_filter = YANGStatementFilter(self.allowed_statements)
        
        # Use shared keyword sets (no need to redefine)
        self.STRUCTURAL_KEYWORDS = STRUCTURAL_KEYWORDS
        self.CONSTRAINT_KEYWORDS = CONSTRAINT_KEYWORDS
        self.ATTRIBUTE_KEYWORDS = ATTRIBUTE_KEYWORDS
        self.YANG_BUILTIN_TYPES = YANG_BUILTIN_TYPES
        self.ALL_KEYWORDS = ALL_KEYWORDS
    
    def _load_compatibility_rules(self) -> Dict[str, set]:
        """
        Load allowed keywords/attributes/constraints from compatibility_rules.xml.
        Returns a dict with keys: 'keywords', 'attributes', 'constraints'
        """
        import xml.etree.ElementTree as ET
        
        rules_path = Path(__file__).parent.parent / "comparator" / "compatibility_rules.xml"
        
        allowed = {
            'keywords': set(),
            'attributes': set(),
            'constraints': set()
        }
        
        try:
            tree = ET.parse(rules_path)
            root = tree.getroot()
            
            for rule in root.findall('rule'):
                # Extract keywords
                keywords = rule.find('keywords')
                if keywords is not None:
                    for keyword in keywords.findall('keyword'):
                        if keyword.text:
                            allowed['keywords'].add(keyword.text.strip())
                
                # Extract attributes
                attributes = rule.find('attributes')
                if attributes is not None:
                    for attr in attributes.findall('attribute'):
                        if attr.text:
                            allowed['attributes'].add(attr.text.strip())
                
                # Extract constraints
                constraints = rule.find('constraints')
                if constraints is not None:
                    for constraint in constraints.findall('constraint'):
                        if constraint.text:
                            allowed['constraints'].add(constraint.text.strip())
            
            console.print(f"[dim]Loaded compatibility rules:[/dim]")
            console.print(f"[dim]  - Keywords: {len(allowed['keywords'])} ({', '.join(sorted(allowed['keywords'])[:10])}...)[/dim]")
            console.print(f"[dim]  - Attributes: {len(allowed['attributes'])} ({', '.join(sorted(allowed['attributes']))})[/dim]")
            console.print(f"[dim]  - Constraints: {len(allowed['constraints'])} ({', '.join(sorted(allowed['constraints'])[:10])}...)[/dim]")
            
        except Exception as e:
            console.print(f"[yellow]Warning: Could not load compatibility rules: {e}[/yellow]")
            console.print("[yellow]Will extract all statements[/yellow]")
            # Return None to indicate "extract everything"
            return None
        
        return allowed
    
    def should_extract_statement(self, stmt, track_uncovered: bool = True) -> bool:
        """
        Determine if a statement should be extracted based on compatibility rules.
        Uses shared YANGStatementFilter for consistent filtering.
        
        Args:
            stmt: The pyang statement
            track_uncovered: If True, track uncovered statements for reporting
        """
        keyword_str = self.get_keyword_string(stmt)
        
        # Get source info for tracking
        source_file = "unknown"
        line_no = 0
        if hasattr(stmt, 'pos') and stmt.pos and hasattr(stmt.pos, 'ref'):
            source_file = str(stmt.pos.ref)
            line_no = stmt.pos.line if hasattr(stmt.pos, 'line') else 0
        
        # Build minimal snippet
        arg = stmt.arg if stmt.arg else ""
        snippet = f"{keyword_str} {arg}" if arg else keyword_str
        
        # Use shared filter
        return self.statement_filter.should_extract_statement(
            keyword_str,
            track_uncovered=track_uncovered,
            source_file=source_file,
            line_no=line_no,
            snippet=snippet
        )
    
    def categorize_keyword(self, keyword: str) -> str:
        """
        Categorize YANG keyword using shared helper.
        
        Returns:
            'structural', 'constraint', 'attribute', 'extension', or 'unknown'
        """
        # Use shared categorization with our extension registry
        return YANGKeywordHelper.categorize_keyword(
            keyword,
            extension_registry=self.get_all_registered_extensions()
        )
    
    def get_all_registered_extensions(self):
        """Get all registered extension names across all modules."""
        all_exts = set()
        for ext_set in self.extension_registry.values():
            all_exts.update(ext_set)
        return all_exts
    
    def build_keyword_skeleton_path(self, stmt) -> str:
        """
        Build keyword skeleton path by traversing from statement back to module.
        Removes the last segment to make paths more general and context-focused.
        
        Example: module/container/list/key -> module/container/list
                 module/leaf/type/pattern -> module/leaf/type
                 module/list/privileges -> module/list
        
        This helps matching by focusing on context rather than specific names.
        """
        parts = []
        current = stmt
        prev_keyword = None
        
        # Build path from current node up to root
        while current is not None:
            keyword = self.get_keyword_string(current)
            
            # Skip module/submodule (will add 'module' prefix at the end)
            if keyword in ['module', 'submodule']:
                break
            
            # Handle extension keywords (prefix:keyword format) - extract base keyword
            if ':' in keyword:
                keyword = keyword.split(':', 1)[1]
            
            # Add all keywords to path (skip consecutive duplicates like type/type)
            if keyword != prev_keyword:
                parts.insert(0, keyword)
                prev_keyword = keyword
            
            current = current.parent if hasattr(current, 'parent') else None
        
        # Always prepend 'module'
        parts.insert(0, 'module')
        
        # Remove the last segment to make path more general
        # This focuses on context rather than specific names
        # e.g., module/list/privileges -> module/list
        if len(parts) > 1:
            parts = parts[:-1]
        
        return '/'.join(parts)
    
    def tokenize_snippet(self, text: str) -> List[str]:
        """Tokenize YANG snippet into tokens, preserving structure."""
        import re
        # Split on whitespace but preserve quoted strings (including multi-line)
        # Use DOTALL flag to make [^"] match newlines too
        # Pattern priorities (order matters!):
        # 1. Quoted strings (multi-line)
        # 2. Version patterns (e.g., 0.1.0, 2.5.3-beta, 2023.10.15) - BEFORE range patterns
        # 3. Range expressions (e.g., 1..100, 1..100|106|108)
        # 4. Identifiers/keywords with colons (e.g., oc-ext:openconfig-version)
        # 5. Simple integers
        # 6. Special characters
        pattern = r'"[^"]*"|\'[^\']*\'|\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z0-9]+)?|[\d\-]+(?:\.\.[\d\-]+)?(?:\|[\d\-]+)*|[a-zA-Z_][\w\-.:]*|\d+|[{};,\[\]\(\)]'
        tokens = re.findall(pattern, text, re.DOTALL)
        return tokens
    
    def is_identifier(self, token: str) -> bool:
        """Check if token is an identifier (not a keyword or special char)."""
        if not token or token in ['{', '}', ';', ',', '(', ')', '[', ']']:
            return False
        if token in self.ALL_KEYWORDS:
            return False
        if token in self.YANG_BUILTIN_TYPES:
            return False
        if ':' in token:
            # Could be prefix:identifier or prefix:keyword
            parts = token.split(':', 1)
            if len(parts) == 2 and parts[1] in self.get_all_registered_extensions():
                return False
        return True
    
    def is_regex_pattern(self, token: str) -> bool:
        """Check if token looks like a regex pattern."""
        # Heuristic: contains regex metacharacters
        regex_chars = ['^', '$', '*', '+', '?', '[', ']', '|', '(', ')', '.', '\\']
        return any(c in token for c in regex_chars)
    
    def is_number(self, token: str) -> bool:
        """Check if token is a number."""
        try:
            float(token)
            return True
        except:
            return False
    
    def is_quoted_string(self, token: str) -> bool:
        """Check if token is a quoted string."""
        return (token.startswith('"') and token.endswith('"')) or \
               (token.startswith("'") and token.endswith("'"))
    
    def is_mac_address(self, value: str) -> bool:
        """Check if a value is a valid MAC address (EUI-48 standard: 6 octets)."""
        # Valid MAC address formats:
        # - 6 groups of exactly 2 hex digits with : or - separators (00:1A:2B:3C:4D:5E)
        # - 12 hex digits without separators (001A2B3C4D5E - Cisco style)
        # - Case-insensitive hex digits only
        
        # Colon or dash-separated format: 6 groups of 2 hex digits
        if re.match(r'^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$', value):
            return True
        
        # No separators: exactly 12 hex digits (Cisco style)
        if re.match(r'^[0-9A-Fa-f]{12}$', value):
            return True
        
        return False
    
    def is_ip_address(self, value: str) -> bool:
        """Check if value is a valid IPv4 or IPv6 address."""
        # IPv4: 192.168.1.1 (4 octets, each 0-255)
        # IPv6: 2001:db8::1, ::1, fe80::1 (compressed format supported)
        # Both support optional zone/interface suffix (e.g., %eth0)
        
        # IPv4 pattern with strict octet validation (0-255)
        ipv4_pattern = r'^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)(%[a-zA-Z0-9\-_]+)?$'
        if re.match(ipv4_pattern, value):
            return True
        
        # IPv6 pattern - must have at least one group with 3-4 hex digits OR contain ::
        # This prevents short hex values like "0:a:0:9:0:d:1:0" from matching
        # Valid: 2001:db8::1, fe80::1, ::1, 2001:0db8:0000:0000:0000:ff00:0042:8329
        # Invalid: 0:a:0:9 (too short), 11:11:11:11 (looks like MAC with wrong count)
        if '::' in value or any(len(part) >= 3 for part in value.split(':') if part and '%' not in part):
            ipv6_pattern = r'^([0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(%[a-zA-Z0-9\-_]+)?$'
            return bool(re.match(ipv6_pattern, value))
        
        return False
    
    def is_quoted_list(self, token: str) -> bool:
        """Check if token is a quoted list (multiple comma-separated values)."""
        if not self.is_quoted_string(token):
            return False
        # Remove quotes and check for commas
        inner = token[1:-1]
        return ',' in inner or ' ' in inner
    
    def mask_token(self, token: str, context: str = '', prev_token: str = '', is_first_token: bool = False, module_name: str = '') -> str:
        """
        Mask a single token according to masking rules.
        Token system:
        - <STRING>: Double-quoted text (description, namespace, etc.)
        - <IDENTIFIER>: Unquoted names (leaf names, container names)
        - <TYPEDEF>: Custom type references (e.g., oc-yang:counter64 or local new-type)
        - <VERSION>: Version strings (e.g., 0.1.0, 2.5.3, 2023.10.15)
        - <REGEX>: Regex patterns (in pattern/extension test statements)
        - <MAC_ADDRESS>: MAC addresses (EUI-48, EUI-64)
        - <IP_ADDRESS>: IP addresses (IPv4, IPv6)
        - <NUMBER>: Numeric values (simple integers/floats)
        - <RANGE>: Range expressions like "1..100" or "1..100|106|108"
        - <BOOLEAN>: true/false values
        - <LIST>: Space-separated list (e.g., key "prefix origin path-id")
        - <NO_VALUE>: Extensions with no argument (e.g., oc-ext:regexp-posix;)
        
        Args:
            token: Token to mask
            context: Statement context ('pattern', 'boolean', 'number', 'type', 'key', 'version')
            prev_token: Previous token for context detection
            is_first_token: True if this is the first token in the snippet (extension statements)
            module_name: Current module name for typedef lookup
        """
        # Preserve special characters
        if token in ['{', '}', ';', ',', '(', ')', '[', ']']:
            return token
        
        # Preserve keywords
        if token in self.ALL_KEYWORDS:
            return token
        
        # Preserve YANG built-in types
        if token in self.YANG_BUILTIN_TYPES:
            return token
        
        # Handle prefixed tokens (extensions and typedefs)
        if ':' in token:
            parts = token.split(':', 1)
            if len(parts) == 2:
                prefix, name = parts
                # If this is the first token with a colon, it's an extension statement - PRESERVE IT
                if is_first_token:
                    return token
                # In type context, prefixed names are typedef references (e.g., oc-yang:counter64)
                if prev_token == 'type' or context == 'type':
                    return '<TYPEDEF>'
                # Preserve known YANG keywords or registered extensions with full prefixed name
                if name in self.ALL_KEYWORDS or name in self.get_all_registered_extensions():
                    return token
                # For vendor sub-extension keywords inside a block (e.g. ext:default-value inside
                # ext:dynamic-default { ... }), preserve the BASE NAME (strip prefix).
                # This gives templates like: ext:dynamic-default { default-value <STRING> { ... } }
                # instead of: ext:dynamic-default { <IDENTIFIER> <STRING> { ... } }
                return name
        
        # Check for local typedef references (unprefixed, in type context)
        if (prev_token == 'type' or context == 'type') and module_name:
            # Check if this token is a locally defined typedef in the current module
            if module_name in self.typedef_registry and token in self.typedef_registry[module_name]:
                return '<TYPEDEF>'
        
        # Boolean keywords context (config, mandatory, etc.) - works at any position
        boolean_keywords = {'config', 'mandatory', 'ordered-by', 'yin-element', 'require-instance'}
        if prev_token in boolean_keywords:
            if token.lower() in ['true', 'false']:
                return '<BOOLEAN>'
        
        # Number/range keywords context - works at any position
        number_keywords = {'range', 'length', 'min-elements', 'max-elements', 'fraction-digits', 'position', 'value'}
        if prev_token in number_keywords:
            # Check for range pattern (only for range/length statements)
            if prev_token in ['range', 'length']:
                content = token.strip('"').strip("'") if self.is_quoted_string(token) else token
                # Range pattern: digits with .. or | separators (allows spaces around ..)
                if re.match(r'^[\d\-]+\s*\.\.\s*[\d\-]+', content) or (re.match(r'^[\d\.\-|\s]+$', content) and '|' in content):
                    return '<RANGE>'
            # Simple numbers for any other number keyword (not range/length)
            if prev_token not in ['range', 'length']:
                if self.is_quoted_string(token) or self.is_number(token):
                    return '<NUMBER>'
        
        # Pattern context
        if context == 'pattern' or prev_token == 'pattern':
            if self.is_quoted_string(token):
                return '<REGEX>'
            if self.is_regex_pattern(token):
                return '<REGEX>'
        
        # Context-aware masking (for initial keyword context)
        if context == 'boolean':
            if token.lower() in ['true', 'false']:
                return '<BOOLEAN>'
        
        if context == 'number':
            content = token.strip('"').strip("'") if self.is_quoted_string(token) else token
            # Range pattern detection (only for range/length contexts) - allows spaces around ..
            if context == 'number' and (re.match(r'^[\d\-]+\s*\.\.\s*[\d\-]+', content) or (re.match(r'^[\d\.\-|\s]+$', content) and '|' in content)):
                return '<RANGE>'
            # Simple numbers
            if self.is_number(token) or self.is_quoted_string(token):
                return '<NUMBER>'
        
        # Mask quoted strings (check for range patterns, typedefs, and regex tests)
        if self.is_quoted_string(token):
            content = token[1:-1]
            # Range patterns like "1..100" or "1 .. 100" or "1..100|106|108" (allows spaces)
            if re.match(r'^[\d\-]+\s*\.\.\s*[\d\-]+', content) or (re.match(r'^[\d\.\-|\s]+$', content) and '|' in content):
                return '<RANGE>'
            # In type context, quoted prefixed names are typedef references (e.g., "oc-yang:counter64")
            if (prev_token == 'type' or context == 'type') and ':' in content:
                return '<TYPEDEF>'
            # Pattern test extensions (pt:pattern-test-pass, pt:pattern-test-fail) use test values
            if prev_token and ('pattern-test-pass' in prev_token or 'pattern-test-fail' in prev_token):
                # Check if it's a MAC address
                if self.is_mac_address(content):
                    return '<MAC_ADDRESS>'
                # Check if it's an IP address
                if self.is_ip_address(content):
                    return '<IP_ADDRESS>'
                # Otherwise treat as a regex pattern
                return '<REGEX>'
            # XPath/path expressions: start with / or contain YANG path segments (word/word or ns:word/word)
            if content.startswith('/') or re.match(r'^[\w:\-]+(?:/[\w:\-]+)+$', content):
                return '<PATH>'
            # Version strings: semver (1.2.3), release tags (rel25), date-versions (2023.10.15)
            if (re.match(r'^\d+\.\d+(?:\.\d+)?(?:[.\-][a-zA-Z0-9]+)*$', content) or
                    re.match(r'^rel\d+(?:\.\d+)*$', content)):
                return '<VERSION>'
            # XML-driven masking: if prev_token has a known XML type, use it.
            # This handles keywords not covered by the specific checks above,
            # e.g. when/must -> <CONDITION>, yang-version -> <VERSION>, key/unique -> <LIST>.
            # Existing logic (boolean, number, pattern, path, version) already ran above
            # and takes priority — this is the fallback for remaining XML-typed keywords.
            if prev_token and prev_token in KEYWORD_XML_TYPES:
                xml_type = KEYWORD_XML_TYPES[prev_token]
                if xml_type:
                    return XML_TYPE_TO_MASK.get(xml_type, '<STRING>')
            # Multi-value list: quoted string containing multiple values separated by
            # space, comma, semicolon, or pipe (e.g. "create update", "a,b,c", "x|y|z")
            if re.search(r'[\s,;|]', content):
                return '<LIST>'
            return '<STRING>'
        
        # Check for unquoted range patterns (e.g., 0..0, 1..10, -10..10) - allows spaces
        if re.match(r'^[\d\-]+\s*\.\.\s*[\d\-]+', token):
            return '<RANGE>'
        
        # Check for version patterns (e.g., 0.1.0, 2.5.3, 2023.10.15)
        # Version pattern: digit(s).digit(s) or digit(s).digit(s).digit(s) with optional suffix
        # Match: 0.1.0, 1.0, 2.5.3-beta, 2023.10.15
        if re.match(r'^\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z0-9]+)?$', token):
            return '<VERSION>'

        # XML-driven masking for unquoted tokens (e.g. yang-version 1 -> <VERSION>).
        # Must run BEFORE the generic is_number() check so that keywords whose XML type
        # is 'version' (or any other non-number type) are not incorrectly masked as <NUMBER>.
        if prev_token and prev_token in KEYWORD_XML_TYPES:
            xml_type = KEYWORD_XML_TYPES[prev_token]
            if xml_type:
                return XML_TYPE_TO_MASK.get(xml_type, '<STRING>')

        # Mask simple numbers
        if self.is_number(token):
            return '<NUMBER>'
        
        # Mask unquoted regex patterns (only if in pattern context)
        if context == 'pattern' and self.is_regex_pattern(token):
            return '<REGEX>'
        
        # Mask identifiers (leaf/container/type names, etc.)
        if self.is_identifier(token):
            return '<IDENTIFIER>'
        
        # Default: keep as-is
        return token
    
    def mask_snippet(self, text: str, module_name: str = '') -> str:
        """Mask a YANG snippet according to rules, detecting statement context."""
        tokens = self.tokenize_snippet(text)
        
        # Detect context from the first keyword (statement type)
        context = ''
        boolean_keywords = {'config', 'mandatory', 'ordered-by', 'yin-element', 'require-instance'}
        number_keywords = {'range', 'length', 'min-elements', 'max-elements', 'fraction-digits', 'position', 'value'}
        # Structural keywords that have an identifier name as first argument
        name_taking_keywords = {'leaf', 'container', 'list', 'leaf-list', 'grouping', 
                                'typedef', 'identity', 'extension', 'feature', 'rpc',
                                'notification', 'action', 'choice', 'case', 'bit', 'enum'}
        
        # Check if this is an extension statement
        # Extensions can be: 1) prefixed (oc-ext:regexp-posix), or 2) unknown keywords (privileges)
        is_extension_stmt = False
        if len(tokens) > 0:
            first_tok = tokens[0]
            # Prefixed extension
            if ':' in first_tok:
                is_extension_stmt = True
            # Unknown keyword (not in YANG keywords, not punctuation, looks like identifier)
            elif (first_tok not in self.ALL_KEYWORDS and 
                  first_tok not in ['{', '}', ';', ','] and
                  re.match(r'^[a-zA-Z_][\w\-]*$', first_tok)):
                # This is likely an unknown extension keyword
                is_extension_stmt = True
        
        if len(tokens) > 0:
            first_keyword = tokens[0]
            # Type context (for typedef references)
            if first_keyword == 'type':
                context = 'type'
            # Pattern context
            elif first_keyword == 'pattern':
                context = 'pattern'
            # Key context (for list keys)
            elif first_keyword == 'key':
                context = 'key'
            # Name context (for structural elements with identifier names)
            elif first_keyword in name_taking_keywords:
                context = 'name'
            # Boolean constraints
            elif first_keyword in boolean_keywords:
                context = 'boolean'
            # Numeric constraints
            elif first_keyword in number_keywords:
                context = 'number'
        
        masked = []
        for i, tok in enumerate(tokens):
            prev_tok = tokens[i-1] if i > 0 else ''
            is_first_token = (i == 0)
            
            # Special case: extension with no value (e.g., "oc-ext:regexp-posix;")
            # If it's an extension statement and only has 2 tokens (extension name + semicolon)
            if is_extension_stmt and len(tokens) == 2 and tokens[1] == ';':
                if i == 0:
                    masked.append(tok)  # Keep extension name
                elif i == 1:
                    masked.append('<NO_VALUE>')  # Add <NO_VALUE> before semicolon
                    masked.append(tok)  # Add semicolon
                    break
                continue
            
            # For extension statements, preserve the extension keyword and mask its value
            if is_extension_stmt and i == 0:
                # Preserve the extension keyword (whether prefixed or not)
                masked.append(tok)
                continue
            elif is_extension_stmt and i == 1 and tok not in ['{', ';']:
                # Mask the extension value with comprehensive type detection
                if (tok.startswith('"') and tok.endswith('"')) or (tok.startswith("'") and tok.endswith("'")):
                    content = tok[1:-1]
                    # Boolean values: true/false
                    if content.lower() in ('true', 'false'):
                        masked.append('<BOOLEAN>')
                    # Space-separated list (unique keys, multi-value): <LIST>
                    # Applies when content has spaces — covers YANG key lists, multi-value enums,
                    # and human-readable strings (all are multi-token, not single-value).
                    elif ' ' in content:
                        masked.append('<LIST>')
                    # XPath/path expressions:
                    #   - absolute: /foo/bar or /ns:foo/bar
                    #   - relative: ../foo, ./foo, ../../foo/bar
                    #   - dotted path segments: foo/bar/baz
                    elif (content.startswith('/')
                          or content.startswith('../')
                          or content.startswith('./')
                          or re.match(r'^[\w:\-]+(?:/[\w:\-]+)+$', content)):
                        masked.append('<PATH>')
                    # SNMP OIDs: all-numeric dotted notation with 4+ segments (1.3.6.1.2.1.96)
                    elif re.match(r'^\d+(?:\.\d+){3,}$', content):
                        masked.append('<STRING>')
                    # Pure numeric values (integers, decimals like 3.5, 45)
                    elif re.match(r'^\d+(?:\.\d+)?$', content):
                        masked.append('<NUMBER>')
                    # Version strings: exactly 3 numeric segments (1.0.2) or release tags (rel25)
                    elif re.match(r'^\d+\.\d+\.\d+(?:[.\-][a-zA-Z0-9]+)?$', content):
                        masked.append('<VERSION>')
                    elif re.match(r'^rel\d+(?:\.\d+)*$', content):
                        masked.append('<VERSION>')
                    # Range expressions: N..M or N..M|P|Q (YANG range/length syntax)
                    elif re.match(r'^[\d\-]+\s*\.\.\s*[\d\-]+', content) or \
                         (re.match(r'^[\d\.\-|\s]+$', content) and '..' in content):
                        masked.append('<RANGE>')
                    # Check if it looks like a regex pattern
                    elif self.is_regex_pattern(content):
                        masked.append('<REGEX>')
                    else:
                        masked.append('<STRING>')
                else:
                    # Unquoted value — check boolean first, then number, then identifier
                    if tok.lower() in ('true', 'false'):
                        masked.append('<BOOLEAN>')
                    else:
                        try:
                            float(tok)
                            masked.append('<NUMBER>')
                        except ValueError:
                            masked.append('<IDENTIFIER>')
                continue
            
            # For boolean/number contexts, mask the argument (token after keyword)
            if context in ['boolean', 'number'] and i == 1 and tok not in ['{', ';']:
                if context == 'boolean':
                    if tok.lower() in ['true', 'false']:
                        masked.append('<BOOLEAN>')
                    else:
                        masked.append(self.mask_token(tok, context, prev_tok, is_first_token, module_name))
                elif context == 'number':
                    # Check for range pattern (only for range/length keywords) - allows spaces
                    content = tok.strip('"').strip("'")
                    if first_keyword in ['range', 'length'] and (re.match(r'^[\d\-]+\s*\.\.\s*[\d\-]+', content) or (re.match(r'^[\d\.\-|\s]+$', content) and '|' in content)):
                        masked.append('<RANGE>')
                    else:
                        masked.append('<NUMBER>')
            # For key context, mask the argument specially
            elif context == 'key' and i == 1 and tok not in ['{', ';']:
                # Check if it's a quoted list (contains spaces inside quotes)
                if (tok.startswith('"') and tok.endswith('"')) or (tok.startswith("'") and tok.endswith("'")):
                    content = tok[1:-1]
                    # If it contains spaces, it's a list of keys
                    if ' ' in content:
                        masked.append('<LIST>')
                    else:
                        # Single quoted key
                        masked.append('<STRING>')
                else:
                    # Unquoted single identifier
                    masked.append('<IDENTIFIER>')
            # For name context (structural keywords), mask the name as <IDENTIFIER>
            elif context == 'name' and i == 1 and tok not in ['{', ';']:
                # The first argument is always the identifier name
                masked.append('<IDENTIFIER>')
            else:
                masked.append(self.mask_token(tok, context, prev_tok, is_first_token, module_name))
        
        return ' '.join(masked)
    
    def detect_keywords_in_snippet(self, text: str) -> List[str]:
        """Detect YANG keywords present in snippet."""
        tokens = self.tokenize_snippet(text)
        keywords = set()
        
        for i, tok in enumerate(tokens):
            # Skip special characters
            if tok in ['{', '}', ';', ',', '(', ')', '[', ']']:
                continue
            
            # Skip quoted strings (they are values, not keywords)
            if self.is_quoted_string(tok):
                continue
            
            # Only detect keywords in statement position (first token or after special chars)
            # This prevents leaf names like "prefix" from being detected as the prefix keyword
            prev_tok = tokens[i-1] if i > 0 else ''
            is_statement_position = (i == 0) or (prev_tok in ['{', '}', ';'])
            
            # Extension keywords (e.g., oc-ext:openconfig-version -> "openconfig-version")
            if ':' in tok:
                parts = tok.split(':', 1)
                if len(parts) == 2:
                    prefix, name = parts
                    # Always add the extension base name as keyword (e.g., "openconfig-version" from "oc-ext:openconfig-version")
                    keywords.add(name)
                    # Also check if it's a YANG keyword after prefix
                    if name in self.ALL_KEYWORDS:
                        keywords.add(name)
            # Direct keyword match - only in statement position
            elif is_statement_position and tok in self.ALL_KEYWORDS:
                keywords.add(tok)
            # Check if token itself is an extension name
            elif tok in self.get_all_registered_extensions():
                keywords.add(tok)
            # Unknown extension keywords (first token, not in known keywords, looks like identifier)
            elif is_statement_position and i == 0 and tok not in self.ALL_KEYWORDS:
                # Check if it looks like an identifier (potential unknown extension)
                if re.match(r'^[a-zA-Z_][\w\-]*$', tok):
                    keywords.add(tok)
        
        return sorted(keywords)
    
    def determine_category_new(self, keywords: List[str], stmt) -> str:
        """
        Determine category using new 4-category system:
        STRUCTURAL, CONSTRAINT, ATTRIBUTE, ATTRIBUTE_OR_CONSTRAINT
        
        Extensions are categorized based on structure:
        - Extension with block {} → STRUCTURAL
        - Extension with value → ATTRIBUTE_OR_CONSTRAINT (can't be 100% sure)
        
        Priority: Statement's own keyword > Structure-based > Keywords > UNKNOWN
        """
        # First check the statement's own keyword (highest priority)
        keyword_str = self.get_keyword_string(stmt)
        
        # Check if this is an extension (prefixed keyword or unknown keyword)
        is_extension = False
        if ':' in keyword_str:
            is_extension = True
            base_keyword = keyword_str.split(':', 1)[1] if ':' in keyword_str else keyword_str
        else:
            base_keyword = keyword_str
            # Check if it's an unknown keyword (not in our keyword sets)
            if base_keyword not in (self.STRUCTURAL_KEYWORDS | self.CONSTRAINT_KEYWORDS | self.ATTRIBUTE_KEYWORDS):
                is_extension = True
        
        # If it's an extension, categorize based on structure
        if is_extension:
            # Check if extension has a block (substmts)
            has_block = stmt.substmts and len(stmt.substmts) > 0
            
            if has_block:
                # Extension with block → STRUCTURAL
                return 'STRUCTURAL'
            else:
                # Extension with value only → "ATTRIBUTE, CONSTRAINT"
                # We can't be 100% sure if it's an attribute or constraint
                # Use comma-separated format to indicate both possibilities
                return 'ATTRIBUTE, CONSTRAINT'
        
        # For known keywords, use standard categorization
        if base_keyword in self.CONSTRAINT_KEYWORDS:
            return 'CONSTRAINT'
        elif base_keyword in self.STRUCTURAL_KEYWORDS:
            return 'STRUCTURAL'
        elif base_keyword in self.ATTRIBUTE_KEYWORDS:
            return 'ATTRIBUTE'
        
        # Fall back to checking all keywords in snippet
        keywords_set = set(keywords)
        
        # Check if any constraint keywords
        if any(kw in self.CONSTRAINT_KEYWORDS for kw in keywords_set):
            return 'CONSTRAINT'
        
        # Check if any structural keywords
        if any(kw in self.STRUCTURAL_KEYWORDS for kw in keywords_set):
            return 'STRUCTURAL'
        
        # Check if any attribute keywords
        if any(kw in self.ATTRIBUTE_KEYWORDS for kw in keywords_set):
            return 'ATTRIBUTE'
        
        # Fallback for truly unknown (not prefixed, not in our keyword sets)
        # Treat conservatively as could be either attribute or constraint
        return 'ATTRIBUTE, CONSTRAINT'
    
    def build_keyword_snippet(self, masked_snippet: str, keywords: List[str]) -> str:
        """
        Build keyword snippet: focused substring highlighting 2-3 most important keywords.
        Priority: CONSTRAINT > STRUCTURAL > ATTRIBUTE
        
        Returns a concise snippet that shows the key structure, not the full masked snippet.
        """
        if not keywords:
            # If no keywords, return a very short version
            return masked_snippet[:50].strip()
        
        # Tokenize masked snippet
        tokens = masked_snippet.split()
        
        # Prioritize keywords by category
        constraint_kws = [kw for kw in keywords if kw in self.CONSTRAINT_KEYWORDS]
        structural_kws = [kw for kw in keywords if kw in self.STRUCTURAL_KEYWORDS]
        attribute_kws = [kw for kw in keywords if kw in self.ATTRIBUTE_KEYWORDS]
        extension_kws = [kw for kw in keywords if kw in self.get_all_registered_extensions()]
        
        # Build priority list: constraints first, then structural, then extensions, then attributes
        priority_kws = constraint_kws[:2] + structural_kws[:2] + extension_kws[:1] + attribute_kws[:1]
        
        # Find positions of priority keywords in token list
        keyword_positions = []
        for i, tok in enumerate(tokens):
            if tok in priority_kws:
                keyword_positions.append(i)
        
        if not keyword_positions:
            # No keywords found in tokens, return short version
            return ' '.join(tokens[:8])
        
        # Extract window around keyword positions
        # Strategy: take first and last keyword position, expand by 3 tokens on each side
        min_pos = min(keyword_positions)
        max_pos = max(keyword_positions)
        
        # If keywords are far apart, focus on first few keywords only
        if max_pos - min_pos > 15:
            # Take window around first 2-3 keywords
            relevant_positions = keyword_positions[:3]
            min_pos = min(relevant_positions)
            max_pos = max(relevant_positions)
        
        # Expand window
        start = max(0, min_pos - 2)
        end = min(len(tokens), max_pos + 4)
        
        # Build snippet
        snippet_tokens = tokens[start:end]
        snippet = ' '.join(snippet_tokens)
        
        # Limit length
        if len(snippet) > 100:
            snippet = snippet[:100].rsplit(' ', 1)[0] + ' ...'
        
        return snippet
    
    def build_embedding_template(self, stmt, module_name: str) -> Tuple[str, Dict[str, Any]]:
        """
        Build the embedding template string and metadata.
        Template: masked_snippet only
        
        This ensures clean separation:
        - Embeddings capture semantic similarity of raw YANG code
        - Path, category, keywords, and tokens are scored explicitly (no double-counting)
        
        Note: Description is extracted AFTER this in extract_statement(), so we build
        the template in two stages. This method prepares the base template.
        """
        # Build keyword skeleton path (for metadata and explicit scoring)
        skeleton_path = self.build_keyword_skeleton_path(stmt)
        
        # Get raw snippet
        raw_snippet = self.get_raw_statement(stmt, max_length=500)
        
        # Mask snippet (pass module_name for typedef context)
        masked_snippet = self.mask_snippet(raw_snippet, module_name)
        
        # Detect keywords
        keywords = self.detect_keywords_in_snippet(raw_snippet)
        
        # Determine category
        category = self.determine_category_new(keywords, stmt)
        
        # Build template: MASKED SNIPPET + keyword semantic hint for short snippets.
        # For very short snippets (single-keyword attributes with no description), the
        # embedding is dominated by the keyword token alone, which may be semantically
        # distant from vendor-prefixed equivalents (e.g. 'yang-version' vs 'module-version').
        # Appending the keyword name as a plain English phrase improves recall without
        # introducing double-counting (path/category/tokens are still scored explicitly).
        keyword_hint = ''
        if len(masked_snippet.split()) <= 4:  # short snippet: keyword <MASK> ;
            # Use the first detected keyword as the hint
            if keywords:
                kw = keywords[0]
                # Convert hyphenated YANG keyword to readable phrase (e.g. yang-version -> yang version)
                keyword_hint = ' '.join(kw.replace('-', ' ').split())
        template = f"{masked_snippet} {keyword_hint}".strip() if keyword_hint else masked_snippet
        
        metadata = {
            'keyword_skeleton_path': skeleton_path,
            'category': category,
            'keywords': keywords,
            'snippet_masked': masked_snippet,
            'snippet_raw': raw_snippet
        }
        
        return template, metadata
    
    def compute_depth(self, stmt) -> int:
        """Compute depth of statement in tree."""
        depth = 0
        current = stmt
        while current is not None and hasattr(current, 'parent') and current.parent is not None:
            depth += 1
            current = current.parent
        return depth
    
    def extract_module_imports(self, module_stmt) -> List[str]:
        """Extract imported modules from module statement."""
        imports = []
        for imp in module_stmt.search('import'):
            if imp.arg:
                imports.append(imp.arg)
        return imports
    
    def extract_module_extensions(self, module_stmt) -> List[str]:
        """Extract extension definitions from module and their descriptions."""
        extensions = []
        module_name = module_stmt.arg
        
        for ext in module_stmt.search('extension'):
            if ext.arg:
                extensions.append(ext.arg)
                
                # Extract description for this extension
                desc_stmt = ext.search_one('description')
                if desc_stmt and desc_stmt.arg:
                    # Store extension description with key (module_name, extension_name)
                    self.extension_descriptions[(module_name, ext.arg)] = desc_stmt.arg.strip()
        
        return extensions
    
    def extract_module_typedefs(self, module_stmt, module_name: str):
        """Extract typedef names from module and register them."""
        def collect_typedefs(stmt):
            """Recursively collect all typedef names."""
            keyword = stmt.keyword if isinstance(stmt.keyword, str) else str(stmt.keyword[1]) if isinstance(stmt.keyword, tuple) else str(stmt.keyword)
            if keyword == 'typedef' and stmt.arg:
                if module_name not in self.typedef_registry:
                    self.typedef_registry[module_name] = set()
                self.typedef_registry[module_name].add(stmt.arg)
            
            # Recurse into children
            for substmt in stmt.substmts:
                collect_typedefs(substmt)
        
        collect_typedefs(module_stmt)
    
    def get_keyword_string(self, stmt) -> str:
        """Get keyword as string, handling tuple case for extension statements."""
        if isinstance(stmt.keyword, str):
            return stmt.keyword
        elif isinstance(stmt.keyword, tuple) and len(stmt.keyword) == 2:
            # Extension statement: ('prefix', 'keyword')
            return f"{stmt.keyword[0]}:{stmt.keyword[1]}"
        else:
            return str(stmt.keyword)
    
    def canonicalize_path(self, stmt) -> str:
        """Generate canonical XPath-like path for a statement."""
        parts = []
        current = stmt
        
        while current is not None:
            keyword = self.get_keyword_string(current)
            
            if keyword in ['module', 'submodule']:
                # Add module prefix
                parts.insert(0, f"{current.arg}")
                break
            elif keyword in ['leaf', 'container', 'list', 'leaf-list', 
                                    'choice', 'case', 'augment', 'grouping',
                                    'typedef', 'rpc', 'notification', 'action',
                                    'anydata', 'anyxml']:
                # Data/schema nodes
                parts.insert(0, current.arg)
            elif keyword in ['must', 'when', 'pattern', 'range', 
                                    'length', 'default', 'config', 'mandatory',
                                    'presence', 'ordered-by', 'unique', 'key']:
                # Constraint/attribute nodes - don't add to path, just mark
                parts.insert(0, keyword)
            
            current = current.parent if hasattr(current, 'parent') else None
        
        # Format as XPath
        if parts:
            path = '/' + '/'.join(parts)
            return path
        return '/unknown'
    
    def get_parent_info(self, stmt) -> Tuple[Optional[str], Optional[str]]:
        """Get parent statement type and name."""
        if stmt.parent is None:
            return None, None
        
        parent = stmt.parent
        parent_type = parent.keyword
        parent_name = parent.arg if parent.arg else None
        
        if parent_name:
            return parent_type, f"{parent_type}({parent_name})"
        return parent_type, parent_type
    
    def extract_type_info(self, stmt) -> Optional[str]:
        """Extract type information from typed statements."""
        # For leaf/leaf-list, get the type
        if stmt.keyword in ['leaf', 'leaf-list']:
            type_stmt = stmt.search_one('type')
            if type_stmt:
                base_type = type_stmt.arg
                
                # Get additional type info
                type_details = [base_type]
                
                # Range
                range_stmt = type_stmt.search_one('range')
                if range_stmt:
                    type_details.append(f"range({range_stmt.arg})")
                
                # Length
                length_stmt = type_stmt.search_one('length')
                if length_stmt:
                    type_details.append(f"length({length_stmt.arg})")
                
                # Pattern
                pattern_stmts = type_stmt.search('pattern')
                if pattern_stmts:
                    type_details.append(f"pattern({len(pattern_stmts)})")
                
                # Enum
                enum_stmts = type_stmt.search('enum')
                if enum_stmts:
                    type_details.append(f"enum({len(enum_stmts)})")
                
                # Bits
                bit_stmts = type_stmt.search('bit')
                if bit_stmts:
                    type_details.append(f"bits({len(bit_stmts)})")
                
                # Union
                if base_type == 'union':
                    union_types = type_stmt.search('type')
                    if union_types:
                        type_details.append(f"union({len(union_types)})")
                
                return ' '.join(type_details)
        
        # For typedef
        elif stmt.keyword == 'typedef':
            type_stmt = stmt.search_one('type')
            if type_stmt:
                return type_stmt.arg
        
        # For must/when - boolean expression
        elif stmt.keyword in ['must', 'when']:
            return 'boolean-expression'
        
        # For pattern/range/length
        elif stmt.keyword in ['pattern', 'range', 'length']:
            return f'{stmt.keyword}-constraint'
        
        return None
    
    def extract_description(self, stmt) -> Optional[str]:
        """Extract description from statement, error-message, or extension definition."""
        # Try description first
        desc_stmt = stmt.search_one('description')
        if desc_stmt and desc_stmt.arg:
            return desc_stmt.arg.strip()
        
        # Try error-message for must/when
        if stmt.keyword in ['must', 'when']:
            err_msg = stmt.search_one('error-message')
            if err_msg and err_msg.arg:
                return err_msg.arg.strip()
        
        # For extension statements, look up the extension's description from the registry
        keyword_str = self.get_keyword_string(stmt)
        if ':' in keyword_str:
            # This is an extension statement (e.g., "junos:must-message")
            prefix, ext_name = keyword_str.split(':', 1)
            
            # Find the module that defines this extension by checking imports
            # Get the module name from the statement's context
            current = stmt
            while current is not None:
                if current.keyword in ['module', 'submodule']:
                    module_name = current.arg
                    # Get the imported modules for this module
                    imported_modules = self.module_imports.get(module_name, [])
                    
                    # Search for the extension description in imported modules
                    for imported_module in imported_modules:
                        ext_key = (imported_module, ext_name)
                        if ext_key in self.extension_descriptions:
                            return self.extension_descriptions[ext_key]
                    
                    # Also check if the extension is defined in the current module
                    ext_key = (module_name, ext_name)
                    if ext_key in self.extension_descriptions:
                        return self.extension_descriptions[ext_key]
                    
                    break
                
                current = current.parent if hasattr(current, 'parent') else None
        
        return None
    
    def build_search_text(self, stmt, module_name: str, path: str,
                         type_info: Optional[str], description: Optional[str]) -> str:
        """Build human-readable search text for semantic embedding."""
        parts = []
        
        keyword_str = self.get_keyword_string(stmt)
        
        # Statement type and argument
        if stmt.arg:
            parts.append(f"{keyword_str}: {stmt.arg}")
        else:
            parts.append(keyword_str)
        
        # Type info
        if type_info:
            parts.append(f"type({type_info})")
        
        # Description
        if description:
            # Limit description length
            desc = description[:200] + '...' if len(description) > 200 else description
            parts.append(f"- {desc}")
        
        # For must/when, include the expression
        if keyword_str in ['must', 'when'] and stmt.arg:
            expr = stmt.arg[:150] + '...' if len(stmt.arg) > 150 else stmt.arg
            parts.append(f"expr({expr})")
        
        # For pattern, include the pattern
        if keyword_str == 'pattern' and stmt.arg:
            pattern = stmt.arg[:100] + '...' if len(stmt.arg) > 100 else stmt.arg
            parts.append(f"pattern({pattern})")
        
        # Context: include parent path for nested statements
        if '/' in path and path.count('/') > 2:
            parts.append(f"in({path.rsplit('/', 1)[0]})")
        
        return ' '.join(parts)
    
    def build_struct_text(self, stmt, parent_type: Optional[str], 
                         type_info: Optional[str]) -> str:
        """Build structural fingerprint for structure-based matching."""
        parts = []
        
        keyword_str = self.get_keyword_string(stmt)
        
        # Statement type
        parts.append(f"STATEMENT:{keyword_str}")
        
        # Parent context
        if parent_type:
            parts.append(f"PARENT:{parent_type}")
        
        # Type info
        if type_info:
            parts.append(f"TYPE:{type_info}")
        
        # For must/when - analyze expression structure
        if keyword_str in ['must', 'when'] and stmt.arg:
            expr_struct = self._analyze_expression_structure(stmt.arg)
            if expr_struct:
                parts.append(f"EXPR:{expr_struct}")
        
        # For pattern - structural info
        if keyword_str == 'pattern' and stmt.arg:
            pattern_struct = self._analyze_pattern_structure(stmt.arg)
            if pattern_struct:
                parts.append(f"PATTERN:{pattern_struct}")
        
        # For leafref - target path
        if type_info and 'leafref' in type_info:
            path_stmt = stmt.search_one('type')
            if path_stmt:
                path_arg = path_stmt.search_one('path')
                if path_arg and path_arg.arg:
                    parts.append(f"REFPATH:{path_arg.arg}")
        
        return ' | '.join(parts)
    
    def _analyze_expression_structure(self, expr: str) -> Optional[str]:
        """Analyze structure of XPath expression (must/when)."""
        features = []
        
        # Check for comparison operators
        if '>=' in expr:
            features.append('compare(>=)')
        elif '<=' in expr:
            features.append('compare(<=)')
        elif '>' in expr:
            features.append('compare(>)')
        elif '<' in expr:
            features.append('compare(<)')
        elif '=' in expr:
            features.append('compare(=)')
        
        # Check for boolean operators
        if ' and ' in expr:
            features.append('and')
        if ' or ' in expr:
            features.append('or')
        if 'not(' in expr:
            features.append('not')
        
        # Check for path references
        if '../' in expr:
            count = expr.count('../')
            features.append(f'relpath(../{count})')
        if '//' in expr:
            features.append('abspath')
        
        # Check for functions
        if 'current()' in expr:
            features.append('current()')
        if 'count(' in expr:
            features.append('count()')
        if 'string-length(' in expr:
            features.append('string-length()')
        
        return ','.join(features) if features else None
    
    def _analyze_pattern_structure(self, pattern: str) -> Optional[str]:
        """Analyze structure of regex pattern."""
        features = []
        
        # Character classes
        if '[0-9]' in pattern or r'\d' in pattern:
            features.append('digits')
        if '[a-zA-Z]' in pattern or r'\w' in pattern:
            features.append('alpha')
        if '[0-9A-Fa-f]' in pattern:
            features.append('hex')
        
        # Quantifiers
        if '+' in pattern:
            features.append('oneOrMore')
        if '*' in pattern:
            features.append('zeroOrMore')
        if '{' in pattern:
            features.append('exact-count')
        
        # Anchors
        if pattern.startswith('^'):
            features.append('anchor-start')
        if pattern.endswith('$'):
            features.append('anchor-end')
        
        # Alternation
        if '|' in pattern:
            features.append('alternation')
        
        return ','.join(features) if features else None
    
    def compute_fingerprints(self, stmt) -> Dict[str, Any]:
        """Compute various fingerprints for deduplication and matching."""
        fingerprints = {}
        
        keyword_str = self.get_keyword_string(stmt)
        
        # Expression signature (for must/when)
        if keyword_str in ['must', 'when'] and stmt.arg:
            # Normalize and hash the expression
            normalized = re.sub(r'\s+', ' ', stmt.arg.strip())
            fingerprints['expr_signature'] = hashlib.sha1(normalized.encode()).hexdigest()[:12]
        
        # Pattern signature
        if keyword_str == 'pattern' and stmt.arg:
            fingerprints['pattern_signature'] = hashlib.sha1(stmt.arg.encode()).hexdigest()[:12]
        
        # Leafref target (only if it actually exists)
        if keyword_str in ['leaf', 'leaf-list']:
            type_stmt = stmt.search_one('type')
            if type_stmt and type_stmt.arg == 'leafref':
                path_stmt = type_stmt.search_one('path')
                if path_stmt and path_stmt.arg:
                    fingerprints['leafref_target'] = path_stmt.arg
        
        # Default value signature (only if it exists)
        default_stmt = stmt.search_one('default')
        if default_stmt and default_stmt.arg:
            fingerprints['default_value'] = default_stmt.arg
        
        # Only return fingerprints dict if it has content
        return fingerprints if fingerprints else None
    
    def get_raw_statement(self, stmt, max_length: int = 500) -> str:
        """
        Extract raw YANG statement text in searchable format.
        Builds a clean YANG snippet with important sub-statements.
        
        Returns minimal YANG syntax for embedding.
        """
        keyword_str = self.get_keyword_string(stmt)
        arg = stmt.arg if stmt.arg else None
        
        # Build minimal YANG syntax
        parts = []
        
        # Keywords that should always have quoted arguments (string values)
        STRING_ARG_KEYWORDS = {
            'description', 'reference', 'contact', 'organization',
            'namespace', 'prefix', 'error-message', 'error-app-tag', 'default',
            'units', 'path', 'pattern', 'must', 'when'
        }
        
        # Main statement - quote string arguments or if has special chars
        if arg:
            should_quote = (keyword_str in STRING_ARG_KEYWORDS or 
                           ' ' in arg or ';' in arg or '{' in arg or '}' in arg or '\n' in arg or 
                           '/' in arg or ':' in arg or '(' in arg)
            
            if should_quote:
                # Escape quotes in arg
                arg_clean = arg.replace('"', '\\"')
                parts.append(f'{keyword_str} "{arg_clean}"')
            else:
                parts.append(f'{keyword_str} {arg}')
        else:
            parts.append(keyword_str)
        
        # Add important sub-statements for context (limit number)
        important_sub_keywords = ['type', 'default', 'key', 'config', 
                                 'mandatory', 'range', 'pattern', 'length', 'must',
                                 'when', 'base', 'path', 'units', 'description']
        
        sub_parts = []
        for sub_kw in important_sub_keywords:
            sub_stmt = stmt.search_one(sub_kw)
            if sub_stmt and sub_stmt.arg:
                sub_arg = str(sub_stmt.arg)
                
                # Limit description/reference length
                if sub_kw in ['description', 'reference'] and len(sub_arg) > 80:
                    sub_arg = sub_arg[:80] + "..."
                # Limit pattern length
                elif sub_kw == 'pattern' and len(sub_arg) > 60:
                    sub_arg = sub_arg[:60] + "..."
                
                # Quote if needed
                if ' ' in sub_arg or ';' in sub_arg or '{' in sub_arg or '\n' in sub_arg:
                    sub_arg_clean = sub_arg.replace('"', '\\"')
                    sub_parts.append(f'{sub_kw} "{sub_arg_clean}"')
                else:
                    sub_parts.append(f'{sub_kw} {sub_arg}')
                
                # Limit to 3-4 sub-statements
                if len(sub_parts) >= 4:
                    break
        
        # Format as YANG block
        if len(sub_parts) == 0:
            result = f"{parts[0]};"
        else:
            main = parts[0]
            sub_str = "; ".join(sub_parts)
            result = f"{main} {{ {sub_str}; }}"
        
        # Limit total length - ensure we don't break quoted strings
        if len(result) > max_length:
            # Find a safe truncation point (before the last quote if exists)
            truncated = result[:max_length]
            # Count quotes to see if we have an unclosed one
            quote_count = truncated.count('"') - truncated.count('\\"')
            if quote_count % 2 == 1:
                # Unclosed quote - find the last opening quote and close it
                last_quote = truncated.rfind('"')
                if last_quote > 0:
                    truncated = truncated[:last_quote] + '"..."; }'
                else:
                    truncated += '...";}'
            else:
                truncated += "...};"
            result = truncated
        
        return result
    
    def extract_statement(self, stmt, module_name: str, source_file: str) -> Dict[str, Any]:
        """Extract a single statement into structured document with new template."""
        # Generate unique node ID
        self.node_id_counter += 1
        node_id = f"node_{self.node_id_counter}"
        
        # Get keyword first (needed for extension detection)
        keyword_str = self.get_keyword_string(stmt)
        
        # Build embedding template and metadata
        embedding_template, template_metadata = self.build_embedding_template(stmt, module_name)
        
        # Get path (old style, for compatibility)
        path = self.canonicalize_path(stmt)
        
        # Get parent info
        parent_kw, parent_desc = self.get_parent_info(stmt)
        parent_id = getattr(stmt.parent, '_node_id', None) if hasattr(stmt, 'parent') and stmt.parent else None
        
        # Store node_id on statement for parent/child tracking
        stmt._node_id = node_id
        
        # Get type info
        type_info = self.extract_type_info(stmt)
        
        # Get description for all statement types
        description = self.extract_description(stmt)
        
        # Create separate description embedding template for ALL statements with descriptions
        description_template = None
        if description:
            # Limit to 400 chars to stay within embedding model limits (rare case)
            desc_for_embedding = description[:400] if len(description) > 400 else description
            # Clean description: remove extra whitespace/newlines for embedding
            desc_cleaned = ' '.join(desc_for_embedding.split())
            # Create description-focused template
            # Include keyword to provide context about what's being described
            description_template = f"{keyword_str}: {desc_cleaned}"
        
        # Get depth
        depth = self.compute_depth(stmt)
        
        # Get line number if available
        line_no = stmt.pos.line if hasattr(stmt, 'pos') and stmt.pos else None
        
        # Get extension imports (from module level)
        extension_imports = self.module_imports.get(module_name, [])
        
        # Build display text (human-readable format)
        display_parts = [
            f"YANG {template_metadata['category']}: {keyword_str}",
            f"Skeleton Path: {template_metadata['keyword_skeleton_path']}",
            f"Path: {path}",
            f"Module: {module_name}",
            f"Depth: {depth}"
        ]
        if parent_desc and parent_desc != 'N/A':
            display_parts.append(f"Parent: {parent_desc}")
        if type_info:
            display_parts.append(f"Type: {type_info}")
        if template_metadata['keywords']:
            display_parts.append(f"Keywords: {', '.join(template_metadata['keywords'])}")
        if description:
            desc_preview = description[:100] + "..." if len(description) > 100 else description
            display_parts.append(f"Description: {desc_preview}")
        
        display_text = "\n".join(display_parts)
        
        # Create document with new structure
        doc = {
            'node_id': node_id,
            'embedding_template': embedding_template,  # Structure embedding
            '0': description_template,  # Separate description embedding (None if no description)
            'searchable_text': template_metadata['snippet_raw'],  # Original snippet
            'display_text': display_text,
            'metadata': {
                'node_id': node_id,
                'keyword_skeleton_path': template_metadata['keyword_skeleton_path'],
                'category': template_metadata['category'],
                'keywords': template_metadata['keywords'],
                'snippet_masked': template_metadata['snippet_masked'],
                'snippet_raw': template_metadata['snippet_raw'],
                'depth': depth,
                'parent_id': parent_id,
                'extension_imports': extension_imports,
                # Legacy fields for compatibility
                'source': source_file,
                'yang_keyword': keyword_str,
                'yang_keyword_category': self.categorize_keyword(keyword_str),
                'name': stmt.arg if stmt.arg else 'unnamed',
                'description': description or '',
                'module': module_name,
                'path': path,
                'parent_type': parent_desc,
                'type_info': type_info,
                'line_no': line_no
            }
        }
        
        return doc
    
    def traverse_statement(self, stmt, module_name: str, source_file: str):
        """Recursively traverse statement tree with deduplication and filtering."""
        # Check if this statement should be extracted based on compatibility rules
        if not self.should_extract_statement(stmt):
            # Skip this statement but still traverse children
            if hasattr(stmt, 'substmts'):
                for substmt in stmt.substmts:
                    self.traverse_statement(substmt, module_name, source_file)
            return
        
        # Extract current statement
        doc = self.extract_statement(stmt, module_name, source_file)
        
        # Create unique signature for deduplication using embedding template
        doc_signature = f"{doc['embedding_template']}|{module_name}"
        
        # Only add if not a duplicate
        if doc_signature not in self.seen_documents:
            # Get keyword as string (handle tuple case)
            keyword = stmt.keyword if isinstance(stmt.keyword, str) else str(stmt.keyword[1]) if isinstance(stmt.keyword, tuple) else str(stmt.keyword)

            # Per-keyword hard cap
            if self.max_per_keyword > 0:
                kw_count = self.keyword_counts[keyword]
                if kw_count >= self.max_per_keyword:
                    # Hard stop once the per-keyword maximum is reached.
                    # Still traverse children so other keywords can be extracted.
                    if hasattr(stmt, 'substmts'):
                        for substmt in stmt.substmts:
                            self.traverse_statement(substmt, module_name, source_file)
                    return

            self.documents.append(doc)
            self.seen_documents.add(doc_signature)
            self.keyword_counts[keyword] += 1
            self.stats[keyword] += 1
        else:
            self.duplicate_count += 1
        
        # Traverse sub-statements
        if hasattr(stmt, 'substmts'):
            for substmt in stmt.substmts:
                self.traverse_statement(substmt, module_name, source_file)
    
    def parse_module_metadata(self, yang_file: Path) -> Optional[Dict[str, Any]]:
        """
        Phase 1: Parse module and extract metadata (imports, extensions, typedefs).
        Returns module data for second pass, or None if failed.
        """
        try:
            # Normalize to absolute path to avoid relative/absolute mismatches
            yang_path = Path(yang_file).resolve()

            # Parse the module
            with open(yang_path, 'r', encoding='utf-8') as f:
                text = f.read()
            
            module = self.ctx.add_module(str(yang_path), text)
            
            if module is None:
                return None
            
            # Get module name
            module_name = module.arg
            # Prefer path relative to project root (.. / YANG_DATA_PATH.parent),
            # else fallback to CWD or filename
            try:
                source_file = str(yang_path.relative_to(YANG_DATA_PATH.parent.resolve()))
            except Exception:
                try:
                    source_file = str(yang_path.relative_to(Path.cwd()))
                except Exception:
                    source_file = yang_path.name
            
            # Extract imports and extensions for this module
            imports = self.extract_module_imports(module)
            extensions = self.extract_module_extensions(module)
            
            # Store in registries
            self.module_imports[module_name] = imports
            if extensions:
                self.extension_registry[module_name] = set(extensions)
            
            # Extract and register typedefs BEFORE traversing (so they're available for masking)
            self.extract_module_typedefs(module, module_name)
            
            # Return module data for second pass
            return {
                'module': module,
                'module_name': module_name,
                'source_file': source_file
            }
            
        except Exception as e:
            import traceback
            console.print(f"[yellow]Warning: Failed to parse {yang_file.name}: {e}[/yellow]")
            # Uncomment for debugging:
            # console.print(traceback.format_exc())
            return None
    
    def traverse_module(self, module_data: Dict[str, Any]):
        """
        Phase 2: Traverse module and extract statements.
        Now extension descriptions are available.
        """
        module = module_data['module']
        module_name = module_data['module_name']
        source_file = module_data['source_file']
        
        # Traverse the module tree
        self.traverse_statement(module, module_name, source_file)
    
    def process_module(self, yang_file: Path) -> bool:
        """
        Process a single YANG module file (legacy single-pass method).
        Kept for backwards compatibility but prefer using extract_all with two-pass.
        """
        try:
            # Normalize to absolute path to avoid relative/absolute mismatches
            yang_path = Path(yang_file).resolve()

            # Parse the module
            with open(yang_path, 'r', encoding='utf-8') as f:
                text = f.read()
            
            module = self.ctx.add_module(str(yang_path), text)
            
            if module is None:
                return False
            
            # Get module name
            module_name = module.arg
            # Prefer path relative to project root (.. / YANG_DATA_PATH.parent),
            # else fallback to CWD or filename
            try:
                source_file = str(yang_path.relative_to(YANG_DATA_PATH.parent.resolve()))
            except Exception:
                try:
                    source_file = str(yang_path.relative_to(Path.cwd()))
                except Exception:
                    source_file = yang_path.name
            
            # Extract imports and extensions for this module
            imports = self.extract_module_imports(module)
            extensions = self.extract_module_extensions(module)
            
            # Store in registries
            self.module_imports[module_name] = imports
            if extensions:
                self.extension_registry[module_name] = set(extensions)
            
            # Extract and register typedefs BEFORE traversing (so they're available for masking)
            self.extract_module_typedefs(module, module_name)
            
            # Traverse the module tree
            self.traverse_statement(module, module_name, source_file)
            
            return True
            
        except Exception as e:
            import traceback
            console.print(f"[yellow]Warning: Failed to process {yang_file.name}: {e}[/yellow]")
            # Uncomment for debugging:
            # console.print(traceback.format_exc())
            return False
    
    def extract_all(self, yang_files: List[Path], show_progress: bool = True,
                    max_per_keyword: int = 0):
        """Extract from all YANG files using a two-pass approach.

        Args:
            yang_files: List of YANG file paths to process
            show_progress: Show rich progress bar
            max_per_keyword: Hard per-keyword document cap.
                0 = unlimited (default, backward-compatible).
                When > 0, no keyword will exceed this number of extracted documents.
        """
        self.max_per_keyword = max_per_keyword
        if max_per_keyword > 0:
            console.print(f"[dim]Per-keyword hard cap: {max_per_keyword}[/dim]")
        console.print(f"\n[bold cyan]📚 Extracting YANG Statements with Pyang[/bold cyan]\n")
        console.print(f"Processing {len(yang_files)} files...\n")
        
        successful = 0
        failed = 0
        
        # Store parsed modules for second pass
        parsed_modules = []
        
        # PASS 1: Parse all modules and extract extension/typedef metadata
        console.print("[bold yellow]Phase 1: Parsing modules and extracting extension metadata...[/bold yellow]")
        if show_progress:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console
            ) as progress:
                task = progress.add_task("[cyan]Parsing modules...", total=len(yang_files))
                
                for yang_file in yang_files:
                    result = self.parse_module_metadata(yang_file)
                    if result:
                        parsed_modules.append(result)
                        successful += 1
                    else:
                        failed += 1
                    progress.update(task, advance=1)
        else:
            for i, yang_file in enumerate(yang_files, 1):
                if i % 100 == 0:
                    console.print(f"[dim]Parsed {i}/{len(yang_files)}...[/dim]")
                result = self.parse_module_metadata(yang_file)
                if result:
                    parsed_modules.append(result)
                    successful += 1
                else:
                    failed += 1
        
        console.print(f"[green]✓ Phase 1 complete: {successful} modules parsed[/green]")
        
        # PASS 2: Traverse statements and extract data
        console.print("\n[bold yellow]Phase 2: Extracting statements...[/bold yellow]")
        if show_progress:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console
            ) as progress:
                task = progress.add_task("[cyan]Extracting statements...", total=len(parsed_modules))
                
                for module_data in parsed_modules:
                    self.traverse_module(module_data)
                    progress.update(task, advance=1)
        else:
            for i, module_data in enumerate(parsed_modules, 1):
                if i % 100 == 0:
                    console.print(f"[dim]Extracted {i}/{len(parsed_modules)}...[/dim]")
                self.traverse_module(module_data)
        
        console.print(f"\n[green]✓ Successfully processed: {successful} files[/green]")
        if failed > 0:
            console.print(f"[yellow]⚠ Failed: {failed} files[/yellow]")
    
    def load_xml_categories(self) -> Dict[str, List[str]]:
        """Load keywords, attributes, and constraints from XML rules."""
        xml_path = Path(__file__).parent.parent / "comparator" / "compatibility_rules.xml"
        
        keywords = set()
        attributes = set()
        constraints = set()
        
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(xml_path)
            root = tree.getroot()
            
            # Extract keywords (structurals)
            # Updated to use 'structurals/structural' instead of 'keywords/keyword'
            for kw in root.findall('.//structurals/structural'):
                if kw.text:
                    keywords.add(kw.text.strip())
            
            # Extract attributes
            for attr in root.findall('.//attributes/attribute'):
                if attr.text:
                    attributes.add(attr.text.strip())
            
            # Extract constraints
            for const in root.findall('.//constraints/constraint'):
                if const.text:
                    constraints.add(const.text.strip())
            
            return {
                'Keywords': sorted(keywords),
                'Attributes': sorted(attributes),
                'Constraints': sorted(constraints)
            }
        except Exception as e:
            console.print(f"[yellow]Warning: Could not load XML categories: {e}[/yellow]")
            # Fallback to hardcoded values
            return {
                'Keywords': ['container', 'leaf', 'list', 'typedef', 'type'],
                'Attributes': ['description', 'units', 'default'],
                'Constraints': ['pattern', 'range', 'length', 'mandatory']
            }
    
    def print_statistics(self):
        """Print extraction statistics with new categories."""
        console.print("\n[bold]Extraction Statistics[/bold]")

        def _is_extension_doc(doc: Dict[str, Any]) -> bool:
            kw = doc.get('metadata', {}).get('yang_keyword', '')
            if not kw:
                return False
            if ':' in kw:
                return True
            return kw not in (self.STRUCTURAL_KEYWORDS | self.CONSTRAINT_KEYWORDS | self.ATTRIBUTE_KEYWORDS)
        
        # Count by category system
        category_counts = defaultdict(int)
        for doc in self.documents:
            cat = doc['metadata']['category']
            category_counts[cat] += 1
        
        table = Table()
        table.add_column("Category", style="cyan")
        table.add_column("Statements", style="green", justify="right")
        table.add_column("Percentage", style="yellow", justify="right")
        
        total = sum(category_counts.values())
        
        # Display categories in priority order
        # Show "ATTRIBUTE, CONSTRAINT" as "Extension: ATTRIBUTE, CONSTRAINT"
        for cat in ['STRUCTURAL', 'CONSTRAINT', 'ATTRIBUTE', 'ATTRIBUTE, CONSTRAINT', 'UNKNOWN']:
            if cat in category_counts and category_counts[cat] > 0:
                count = category_counts[cat]
                pct = (count / total * 100) if total > 0 else 0
                
                # Format display name
                display_cat = f"Extension: {cat}" if cat == 'ATTRIBUTE, CONSTRAINT' else cat
                table.add_row(display_cat, str(count), f"{pct:.1f}%")
        
        table.add_row("[bold]TOTAL[/bold]", f"[bold]{total}[/bold]", "[bold]100.0%[/bold]")
        console.print(table)

        # Show extension split explicitly for easier interpretation.
        ext_structural = 0
        ext_value_only = 0
        for doc in self.documents:
            if not _is_extension_doc(doc):
                continue
            cat = doc['metadata']['category']
            if cat == 'STRUCTURAL':
                ext_structural += 1
            elif cat == 'ATTRIBUTE, CONSTRAINT':
                ext_value_only += 1

        ext_total = ext_structural + ext_value_only
        ext_table = Table()
        ext_table.add_column("Extension Type", style="cyan")
        ext_table.add_column("Statements", style="green", justify="right")
        ext_table.add_column("Percentage", style="yellow", justify="right")

        if ext_total > 0:
            structural_pct = f"{(ext_structural / ext_total * 100):.1f}%"
            value_only_pct = f"{(ext_value_only / ext_total * 100):.1f}%"
        else:
            structural_pct = "0.0%"
            value_only_pct = "0.0%"

        ext_table.add_row("EXTENSION_STRUCTURAL", str(ext_structural), structural_pct)
        ext_table.add_row("EXTENSION_VALUE_ONLY", str(ext_value_only), value_only_pct)

        console.print("\n[bold]Extension Breakdown[/bold]")
        console.print(ext_table)
        
        # Extension registry stats
        if self.extension_registry:
            console.print(f"\n[bold cyan]Extension Registry:[/bold cyan]")
            console.print(f"  Modules with extensions: {len(self.extension_registry)}")
            all_exts = self.get_all_registered_extensions()
            console.print(f"  Total unique extensions: {len(all_exts)}")
            if all_exts:
                console.print(f"  Extensions: {', '.join(sorted(all_exts)[:10])}" + 
                            (" ..." if len(all_exts) > 10 else ""))
        
        # Display deduplication info
        if self.duplicate_count > 0:
            total_extracted = total + self.duplicate_count
            console.print(f"\n[yellow]⚠️  Removed {self.duplicate_count} duplicate documents[/yellow]")
            console.print(f"[dim]  Total extracted: {total_extracted} → Unique: {total} ({(total/total_extracted*100):.1f}% unique)[/dim]")
        else:
            console.print(f"\n[green]✓ No duplicates found - all {total} documents are unique[/green]")
        
        # Show top keywords
        console.print("\n[bold]Top 15 Statement Types:[/bold]")
        top_keywords = sorted(self.stats.items(), key=lambda x: x[1], reverse=True)[:15]
        
        kw_table = Table()
        kw_table.add_column("Rank", style="dim", width=4)
        kw_table.add_column("Keyword", style="magenta")
        kw_table.add_column("Count", style="green", justify="right")
        
        for i, (kw, count) in enumerate(top_keywords, 1):
            kw_table.add_row(str(i), kw, str(count))
        
        console.print(kw_table)
        
        # Show uncovered statements summary
        uncovered = self.statement_filter.get_uncovered_statements()
        if uncovered:
            console.print(f"\n[yellow]⚠️  Found {len(uncovered)} uncovered statements (not in compatibility_rules.xml)[/yellow]")
            console.print("[dim]  Use save_uncovered_statements() to export to file[/dim]")
    
    def save_uncovered_statements(self, output_file: Optional[Path] = None):
        """Save uncovered statements to a text file."""
        if output_file is None:
            output_file = Path(__file__).parent.parent.parent / "data" / "uncovered_statements.txt"
        
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Get uncovered statements from the shared filter
        uncovered = self.statement_filter.get_uncovered_statements()
        
        if not uncovered:
            console.print("[green]✓ No uncovered statements to save[/green]")
            return
        
        # Group by keyword for better organization
        by_keyword = defaultdict(list)
        for stmt in uncovered:
            by_keyword[stmt['keyword']].append(stmt)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write("UNCOVERED YANG STATEMENTS\n")
            f.write("(Statements not covered by compatibility_rules.xml)\n")
            f.write("=" * 80 + "\n\n")
            f.write(f"Total uncovered statements: {len(uncovered)}\n")
            f.write(f"Unique keywords: {len(by_keyword)}\n")
            f.write(f"Generated: {Path(__file__).parent.parent.parent}\n")
            f.write("\n" + "=" * 80 + "\n\n")
            
            # Write summary table
            f.write("SUMMARY BY KEYWORD\n")
            f.write("-" * 80 + "\n")
            f.write(f"{'Keyword':<30} {'Count':>10}\n")
            f.write("-" * 80 + "\n")
            
            sorted_keywords = sorted(by_keyword.items(), key=lambda x: len(x[1]), reverse=True)
            for keyword, stmts in sorted_keywords:
                f.write(f"{keyword:<30} {len(stmts):>10}\n")
            
            f.write("\n" + "=" * 80 + "\n\n")
            
            # Write detailed listings grouped by keyword
            f.write("DETAILED LISTINGS\n")
            f.write("=" * 80 + "\n\n")
            
            for keyword, stmts in sorted_keywords:
                f.write(f"\n{'=' * 80}\n")
                f.write(f"KEYWORD: {keyword} ({len(stmts)} occurrences)\n")
                f.write(f"{'=' * 80}\n\n")
                
                for i, stmt in enumerate(stmts[:50], 1):  # Limit to 50 per keyword
                    f.write(f"  [{i}] File: {stmt['file']}\n")
                    f.write(f"      Line: {stmt['line']}\n")
                    # Path field might not exist in lightweight version
                    if 'path' in stmt:
                        f.write(f"      Path: {stmt['path']}\n")
                    f.write(f"      Snippet: {stmt['snippet'][:120]}\n")
                    f.write("\n")
                
                if len(stmts) > 50:
                    f.write(f"  ... and {len(stmts) - 50} more occurrences\n\n")
            
            f.write("\n" + "=" * 80 + "\n")
            f.write("END OF REPORT\n")
            f.write("=" * 80 + "\n")
        
        console.print(f"[green]✓ Saved {len(uncovered)} uncovered statements to:[/green]")
        console.print(f"[cyan]  {output_file}[/cyan]")
        
        # Show top uncovered keywords
        if len(by_keyword) > 0:
            console.print(f"\n[yellow]Top 10 uncovered keywords:[/yellow]")
            for keyword, stmts in sorted_keywords[:10]:
                console.print(f"  - {keyword}: {len(stmts)} occurrences")
    
    def show_samples(self, num_samples: int = 3):
        """Show sample extracted documents."""
        console.print(f"\n[bold]Sample Extracted Documents ({num_samples}):[/bold]")
        
        for i, doc in enumerate(self.documents[:num_samples], 1):
            console.print(f"\n[bold cyan]Sample {i}:[/bold cyan]")
            
            metadata = doc.get('metadata', {})
            template = doc.get('embedding_template', '')
            display = doc.get('display_text', '')
            
            # Truncate long text
            template_preview = template[:200] + '...' if len(template) > 200 else template
            
            console.print(Panel(
                f"[cyan]Embedding Template:[/cyan]\n{template_preview}\n\n"
                f"[cyan]Display Text:[/cyan]\n{display}\n\n"
                f"[cyan]Metadata Highlights:[/cyan]\n"
                f"  • Node ID: {metadata.get('node_id', 'N/A')}\n"
                f"  • Category: {metadata.get('category', 'N/A')}\n"
                f"  • Skeleton Path: {metadata.get('keyword_skeleton_path', 'N/A')}\n"
                f"  • Keywords: {', '.join(metadata.get('keywords', []))}\n"
                f"  • Snippet: {metadata.get('snippet_masked', 'N/A')[:80]}\n"
                f"  • Depth: {metadata.get('depth', 'N/A')}\n"
                f"  • Module: {metadata.get('module', 'N/A')}\n"
                f"  • Parent ID: {metadata.get('parent_id', 'N/A')}",
                border_style="dim"
            ))


def find_yang_files(max_files: Optional[int] = None) -> List[Path]:
    """Find YANG files to process."""
    console.print(f"[cyan]Scanning for .yang files in:[/cyan]\n{YANG_DATA_PATH}")
    
    all_yang_files = list(YANG_DATA_PATH.rglob("*.yang"))
    console.print(f"[yellow]Found {len(all_yang_files)} total .yang files[/yellow]")
    
    # Deduplicate by filename
    distinct_files = {}
    for yang_file in all_yang_files:
        filename = yang_file.name
        if filename not in distinct_files:
            distinct_files[filename] = yang_file
    
    yang_files = list(distinct_files.values())
    
    # Limit if requested
    if max_files is not None and max_files > 0:
        if max_files < len(yang_files):
            console.print(f"[yellow]⚠ Limiting to {max_files} files (out of {len(yang_files)} distinct)[/yellow]")
            yang_files = yang_files[:max_files]
        else:
            console.print(f"[green]✓ Processing all {len(yang_files)} distinct files[/green]")
    else:
        console.print(f"[green]✓ Processing {len(yang_files)} distinct .yang files[/green]")
    
    console.print(f"[dim]  (Skipped {len(all_yang_files) - len(distinct_files)} duplicates)[/dim]")
    
    return yang_files


def save_to_json(documents: List[Dict[str, Any]]):
    """Save extracted documents to JSON and build graph."""
    console.print(f"\n[bold]Saving to:[/bold]\n{OUTPUT_JSON}")
    
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    
    # Save main documents
    with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(documents, f, indent=2, ensure_ascii=False)
    
    size_mb = OUTPUT_JSON.stat().st_size / (1024 * 1024)
    console.print(f"[green]✓ Saved {len(documents)} documents ({size_mb:.2f} MB)[/green]")
    
    # Build and save graph structure
    graph_path = OUTPUT_JSON.parent / "yang_graph.json"
    graph = build_graph_structure(documents)
    
    with open(graph_path, 'w', encoding='utf-8') as f:
        json.dump(graph, f, indent=2, ensure_ascii=False)
    
    graph_size = graph_path.stat().st_size / (1024 * 1024)
    console.print(f"[green]✓ Saved graph structure ({graph_size:.2f} MB)[/green]")
    console.print(f"[dim]  Graph path: {graph_path}[/dim]")


def build_graph_structure(documents: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build graph structure with nodes and edges."""
    console.print("\n[cyan]Building graph structure...[/cyan]")
    
    nodes = {}
    edges = []
    
    # Build node index
    for doc in documents:
        node_id = doc['node_id']
        metadata = doc['metadata']
        
        nodes[node_id] = {
            'id': node_id,
            'category': metadata['category'],
            'keywords': metadata['keywords'],
            'snippet': metadata['snippet_masked'],
            'skeleton_path': metadata['keyword_skeleton_path'],
            'depth': metadata['depth'],
            'parent_id': metadata.get('parent_id'),
            'neighbors': metadata.get('neighbors', [])
        }
    
    # Build edges
    for doc in documents:
        node_id = doc['node_id']
        metadata = doc['metadata']
        
        # Parent-child edges
        if metadata.get('parent_id'):
            edges.append({
                'source': metadata['parent_id'],
                'target': node_id,
                'type': 'parent-child'
            })
        
        # Neighbor edges (uses, augment)
        for neighbor in metadata.get('neighbors', []):
            edges.append({
                'source': node_id,
                'target': neighbor,
                'type': 'reference'
            })
    
    graph = {
        'nodes': nodes,
        'edges': edges,
        'stats': {
            'total_nodes': len(nodes),
            'total_edges': len(edges)
        }
    }
    
    console.print(f"[green]✓ Graph: {len(nodes)} nodes, {len(edges)} edges[/green]")
    
    return graph


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Extract YANG statements using pyang with rich structural metadata",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python yang_rag/parsing/pyang_extractor.py                              # Process all files
    python yang_rag/parsing/pyang_extractor.py --max-files 100              # Process only 100 files
    python yang_rag/parsing/pyang_extractor.py --max-per-keyword 5000       # Cap at 5000 per keyword
    python yang_rag/parsing/pyang_extractor.py --max-per-keyword 5000 \\
        --max-files 1000                                                     # Both limits
        """
    )
    parser.add_argument(
        '--max-files',
        type=int,
        default=None,
        help='Maximum number of YANG files to process (default: all files)'
    )
    parser.add_argument(
        '--no-progress',
        action='store_true',
        help='Disable progress bar'
    )
    parser.add_argument(
        '--max-per-keyword',
        type=int,
        default=0,
        help=(
            'Hard per-keyword document cap (default: 0 = unlimited). '
            'When set, each keyword is limited to at most this many extracted documents. '
            'Recommended: 5000 for a balanced index size.'
        )
    )

    args = parser.parse_args()

    # Find files
    yang_files = find_yang_files(max_files=args.max_files)

    if not yang_files:
        console.print("[red]No YANG files found![/red]")
        return

    # Extract
    extractor = PyangYANGExtractor()
    extractor.extract_all(yang_files, show_progress=not args.no_progress,
                          max_per_keyword=args.max_per_keyword)
    
    # Show statistics
    extractor.print_statistics()
    extractor.show_samples()
    
    # Save uncovered statements
    extractor.save_uncovered_statements()
    
    # Save
    if extractor.documents:
        save_to_json(extractor.documents)
        
        console.print("\n[bold green]✅ Extraction Complete![/bold green]")
        console.print("\n[cyan]Next steps:[/cyan]")
        console.print("  1. Review: data/yang_pyang_extracted.json")
        console.print("  2. Review uncovered: data/uncovered_statements.txt")
        console.print("  3. Index: python3 -m yang_rag.rag.indexer --use-gpu --model all-MiniLM-L6-v2 --batch-size 256")
    else:
        console.print("\n[bold red]❌ No documents extracted[/bold red]")


if __name__ == "__main__":
    main()
