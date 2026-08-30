"""
YANG Node Comparator - Main orchestration class for YANG module comparison.

This module contains the YANGNodeComparator class which:
- Loads YANG modules using pyang
- Extracts and normalizes nodes from the AST
- Compares nodes between old and new versions
- Handles submodules and typedef expansion
- Returns structured ChangeRecord results
"""

import os
import re
import traceback
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from . import constants as _constants
from .constants import (
    ChangeType,
    DEFAULT_STRUCTURAL_KEYWORDS,
    DEFAULT_CONSTRAINT_KEYWORDS,
    DEFAULT_ATTRIBUTE_KEYWORDS,
    DEFAULT_PATH_BASED_KEYWORDS,
)
from .data_classes import ChangeRecord
from .similarity import SimilarityCalculator
from .node_normalizer import NodeNormalizer
from .comparison_strategies import (
    ListComparisonStrategy,
    AttributeComparisonStrategy
)
from .typedef_handler import TypedefHandler
from .symbolic_comparator import SymbolicComparator
from yang_rag.comparator.helper.uncovered_yang_statements import (
    is_uncovered_scalar_extension_statement,
    find_single_wrapper_owner,
    drop_redundant_wrapper_name_echoes,
)

# Import pyang utilities
from yang_rag.comparator.helper.pyang_utils import (
    PyangStatementHelper,
    create_pyang_context,
    load_yang_module
)
from yang_rag.comparator.helper.pyang_expansion import PyangExpansionHandler


class YANGNodeComparator:
    """Main comparator class that orchestrates the comparison process."""
    
    def __init__(self):
        self.strategies = [
            ListComparisonStrategy(),
            AttributeComparisonStrategy()
        ]
        self.nodes_cache = []
        self.pyang_ctx = None  # Will be initialized when loading modules
        self.helper = PyangStatementHelper()  # Helper for working with pyang statements
        self.typedef_handler = TypedefHandler()  # Handler for typedef definitions and comparisons
        self.symbolic_comparator = SymbolicComparator()  # Handler for symbolic entry comparisons
    
    def _normalize_path_for_comparison(self, path: str) -> str:
        """
        Normalize a node path by removing the module/submodule prefix and stripping
        namespace prefixes from all path segments.

        This allows proper comparison when:
        - One file is a module and the other is a submodule
        - A namespace prefix is renamed (e.g. 'ocif' → 'oc-if') but the augment
          target is the same schema node

        Examples:
            "openconfig-network-instance-l2/l2ni-instance" -> "l2ni-instance"
            "openconfig-network-instance/l2ni-instance" -> "l2ni-instance"
            "openconfig-network-instance/network-instances/network-instance" -> "network-instances/network-instance"
            "openconfig-platform//ocif:interfaces/ocif:interface/ocif:state" ->
                "/interfaces/interface/state"
            "openconfig-platform//oc-if:interfaces/oc-if:interface/oc-if:state" ->
                "/interfaces/interface/state"
        """
        if not path or '/' not in path:
            return path

        # Split on first '/' to separate module prefix from rest
        parts = path.split('/', 1)
        if len(parts) == 2:
            rest = parts[1]
        else:
            rest = path

        # Strip namespace prefixes from each path segment.
        # A segment like "ocif:interfaces" becomes "interfaces".
        # Segments without a colon are left unchanged.
        # Empty segments (from double slashes like "//ocif:...") are preserved.
        def _strip_segment_prefix(segment: str) -> str:
            if ':' in segment:
                return segment.split(':', 1)[1]
            return segment

        normalized_segments = [_strip_segment_prefix(s) for s in rest.split('/')]
        return '/'.join(normalized_segments)
    
    def _is_submodule_file(self, file_path: str) -> bool:
        """Return True if file is a YANG submodule using simple text parsing."""
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as fh:
                for line in fh:
                    stripped = line.split('//')[0].strip()
                    if not stripped:
                        continue
                    if stripped.startswith('submodule '):
                        return True
                    # Stop scanning once we reach the first top-level statement
                    # (module/submodule must appear before any other top-level keyword)
                    if stripped.startswith('module '):
                        return False
            return False
        except Exception as e:
            print(f"[DEBUG] Error in _is_submodule_file: {e}")
            return False

    def _parse_submodule_outline(self, file_path: str) -> Tuple[Optional[str], Dict[str, Set[str]]]:
        """Parse submodule and return (parent_module_name, declared_defs_by_keyword)."""
        parent: Optional[str] = None
        defs: Dict[str, Set[str]] = defaultdict(set)
        
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as fh:
                content = fh.read()
            
            # Find belongs-to statement
            for line in content.split('\n'):
                line = line.strip()
                if line.startswith('belongs-to '):
                    # Extract module name from "belongs-to openconfig-aft {" or "belongs-to "openconfig-aft" {"
                    parts = line.split()
                    if len(parts) >= 2:
                        parent = parts[1].rstrip(' {').strip('"\'')
                        break
            
            # Find top-level definitions using simple parsing
            # Look for statements that appear after the submodule declaration but before any nested structures
            is_in_submodule = False
            
            for line in content.split('\n'):
                line_clean = line.split('//')[0].strip()
                if not line_clean:
                    continue
                
                # Start processing after we see the submodule declaration
                if line_clean.startswith('submodule '):
                    is_in_submodule = True
                    continue
                
                if is_in_submodule:
                    # Use dynamic rule-driven keywords for top-level definitions
                    structural_keywords = _constants.RULE_STRUCTURAL_KEYWORDS if _constants.RULE_STRUCTURAL_KEYWORDS else DEFAULT_STRUCTURAL_KEYWORDS
                    
                    # Look for top-level definitions (they start at the beginning of a line, not indented inside other blocks)
                    # Filter to name-based keywords (not path-based like augment/deviation)
                    # Use rule-driven approach for path-based keywords classification
                    path_based_set = DEFAULT_PATH_BASED_KEYWORDS  # Could be extended from XML in future
                    name_based_keywords = [kw for kw in structural_keywords if kw not in path_based_set]
                    
                    for keyword in name_based_keywords:
                        if line_clean.startswith(f'{keyword} '):
                            # Extract the name
                            rest = line_clean[len(keyword):].strip()
                            name_part = rest.split()[0] if rest.split() else ""
                            name = name_part.rstrip(' {').strip('\'"')
                            if name:
                                defs[keyword].add(name)
                                print(f"[DEBUG] Found {keyword}: {name}")
                            break
                    
                    # Handle path-based keywords (augment and deviation if they exist in structural keywords)
                    path_based_keywords = [kw for kw in structural_keywords if kw in path_based_set]
                    for keyword in path_based_keywords:
                        if line_clean.startswith(f'{keyword} '):
                            # Extract the path (may have quotes)
                            rest = line_clean[len(keyword):].strip()
                            if rest.startswith('"') and '"' in rest[1:]:
                                path = rest[1:rest.index('"', 1)]
                            elif rest.startswith("'") and "'" in rest[1:]:
                                path = rest[1:rest.index("'", 1)]
                            else:
                                path = rest.split()[0] if rest.split() else ""
                            if path:
                                defs[keyword].add(path)
                                print(f"[DEBUG] Found {keyword}: {path}")
                            break
            
            return parent, defs
        except Exception as e:
            print(f"[DEBUG] Error parsing submodule outline: {e}")
            return None, {}

    def _load_nodes_from_submodule_via_parent(self, submodule_file: str, modules_dir: str) -> List[Dict]:
        """Load submodule by parsing it directly and creating nodes from the definitions."""
        
        # Resolve submodule file
        submodule_path = os.path.join(modules_dir, submodule_file)
        if not os.path.exists(submodule_path):
            for root, _, files in os.walk(modules_dir):
                if submodule_file in files:
                    modules_dir = root
                    submodule_path = os.path.join(root, submodule_file)
                    print(f"[Submodule] Located {submodule_file} under {modules_dir}")
                    break
        if not os.path.exists(submodule_path):
            raise FileNotFoundError(f"Submodule file {submodule_file} not found")

        parent_name, def_index = self._parse_submodule_outline(submodule_path)
        if not parent_name:
            raise ValueError(f"Could not determine belongs-to parent for {submodule_file}")

        # Log all found definitions generically
        total_defs = sum(len(defs) for defs in def_index.values())
        print(f"[Submodule] Found parent module: {parent_name}")
        print(f"[Submodule] Found {total_defs} definitions: {dict((k, len(v)) for k, v in def_index.items() if v)}")

        # Create nodes directly from the parsed content using a simplified approach
        self.nodes_cache = []
        
        try:
            with open(submodule_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Parse and create nodes for each structural element found
            self._extract_nodes_from_submodule_content(content, parent_name)
            
            self.nodes_cache.sort(key=lambda x: x["path"])
            print(f"[Submodule] Extracted {len(self.nodes_cache)} nodes from submodule {submodule_file}")
            return self.nodes_cache
            
        except Exception as e:
            print(f"[Submodule] Error processing submodule: {e}")
            return []

    def _extract_nodes_from_submodule_content(self, content: str, parent_name: str):
        """Extract nodes from submodule content using comprehensive parsing."""
        structural_keywords = _constants.RULE_STRUCTURAL_KEYWORDS if _constants.RULE_STRUCTURAL_KEYWORDS else DEFAULT_STRUCTURAL_KEYWORDS
        
        # Split content into lines and process
        lines = content.split('\n')
        current_context = []
        in_submodule = False
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line or line.startswith('//'):
                i += 1
                continue
            
            # Remove inline comments
            line = line.split('//')[0].strip()
            if not line:
                i += 1
                continue
            
            # Mark when we enter the submodule
            if line.startswith('submodule '):
                in_submodule = True
                current_context = [parent_name]
                i += 1
                continue
            
            if not in_submodule:
                i += 1
                continue
            
            # Look for structural keywords
            found_keyword = None
            found_name = None
            
            for keyword in structural_keywords:
                if line.startswith(f'{keyword} '):
                    found_keyword = keyword
                    # Extract name from line like "keyword name {" where keyword is any structural element
                    rest = line[len(keyword):].strip()
                    name_part = rest.split()[0] if rest.split() else ""
                    found_name = name_part.rstrip(' {').strip('\'"')
                    break
            
            if found_keyword and found_name:
                # Create a node for this structural element
                path = '/'.join(current_context + [found_name])
                
                # Extract content between braces for this element
                content_lines, i = self._extract_block_content(lines, i)
                
                # Create the node data similar to NodeNormalizer.extract_node_data
                node_data = {
                    'path': path,
                    'type': found_keyword,
                    'attributes': {
                        'name': found_name
                    },
                    'constraints': {}
                }
                
                # Parse additional attributes from the content
                self._parse_attributes_from_content(content_lines, node_data)
                
                self.nodes_cache.append(node_data)
                
                # Process nested content recursively if it contains structural elements
                nested_content = '\n'.join(content_lines)
                if any(kw in nested_content for kw in structural_keywords):
                    # Create a mini-submodule content and recurse
                    nested_submodule = f"submodule temp {{\n{nested_content}\n}}"
                    self._extract_nodes_from_submodule_content(nested_submodule, path)
                
            else:
                i += 1

    def _extract_block_content(self, lines: List[str], start_index: int) -> Tuple[List[str], int]:
        """Extract content between braces starting from the given line."""
        content_lines = []
        brace_count = 0
        i = start_index
        
        # Find the opening brace
        while i < len(lines):
            line = lines[i]
            brace_count += line.count('{') - line.count('}')
            
            if '{' in line:
                # Found opening brace, start collecting content
                remaining = line.split('{', 1)[-1]
                if remaining.strip():
                    content_lines.append(remaining)
                i += 1
                break
            i += 1
        
        # Collect content until closing brace
        while i < len(lines) and brace_count > 0:
            line = lines[i]
            brace_count += line.count('{') - line.count('}')
            
            if brace_count > 0:
                content_lines.append(line)
            elif brace_count == 0 and '}' in line:
                # Add content before the closing brace
                before_brace = line.split('}')[0]
                if before_brace.strip():
                    content_lines.append(before_brace)
            
            i += 1
        
        return content_lines, i

    def _parse_attributes_from_content(self, content_lines: List[str], node_data: Dict):
        """Parse additional attributes from block content and properly separate attributes from constraints."""
        for line in content_lines:
            line = line.strip()
            if not line or line.startswith('//'):
                continue
                
            line = line.split('//')[0].strip()
            if not line:
                continue
            
            # Use dynamic rule-driven attributes and constraints for parsing
            all_parseable_attrs = set()
            if _constants.RULE_ATTRIBUTES:
                all_parseable_attrs.update(_constants.RULE_ATTRIBUTES)
            if _constants.CONSTRAINT_KEYWORDS:
                all_parseable_attrs.update(_constants.CONSTRAINT_KEYWORDS)
            
            # Fallback to comprehensive YANG attribute/constraint set if no rules loaded
            if not all_parseable_attrs:
                all_parseable_attrs = set(_constants.RULE_ATTRIBUTES) if _constants.RULE_ATTRIBUTES else set()
                all_parseable_attrs.update(_constants.CONSTRAINT_KEYWORDS if _constants.CONSTRAINT_KEYWORDS else DEFAULT_CONSTRAINT_KEYWORDS)
                # Final safety fallback using default constraint keywords if still completely empty
                if not all_parseable_attrs:
                    all_parseable_attrs = set(DEFAULT_CONSTRAINT_KEYWORDS)
            
            # Look for attribute/constraint statements
            for attr in all_parseable_attrs:
                if line.startswith(f'{attr} '):
                    value = line[len(attr):].strip().rstrip(';').strip('\'"')
                    # Properly separate constraints from attributes like NodeNormalizer does
                    if attr in _constants.CONSTRAINT_KEYWORDS:
                        node_data['constraints'][attr] = value
                    else:
                        node_data['attributes'][attr] = value
                    break

    def load_nodes_from_module(self, module_file: str, modules_dir: Optional[str] = None, *args, **kwargs) -> List[Dict]:
        """Load and normalize nodes from a YANG module using pyang.

        Backward compatibility:
        - Older call sites passed (module_file, None, modules_dir, auto_create_library=True)
        - New call sites pass (module_file, modules_dir)
        The deprecated ``auto_create_library`` argument is ignored.
        """
        self.nodes_cache = []

        # Legacy signature support: load_nodes_from_module(file, None, modules_dir, ...)
        if modules_dir is None and args:
            modules_dir = args[0]

        if modules_dir is None:
            raise ValueError("modules_dir is required")

        # Deprecated/unused compatibility kwarg.
        _ = kwargs.pop('auto_create_library', None)
        
        try:
            print(f"\n[PyangLoader] Loading module: {module_file}")
            
            # Extract module name from file
            module_name = os.path.splitext(module_file)[0]

            # Locate module file (check nested subdirectories)
            candidate_path = os.path.join(modules_dir, module_file)
            if not os.path.exists(candidate_path):
                # Search recursively
                for root, _, files in os.walk(modules_dir):
                    if module_file in files:
                        candidate_path = os.path.join(root, module_file)
                        modules_dir = root
                        print(f"[PyangLoader] Found module at: {candidate_path}")
                        break
            
            if not os.path.exists(candidate_path):
                raise FileNotFoundError(f"Module file '{module_file}' not found in {modules_dir}")
            
            print(f"[PyangLoader] Module path: {candidate_path}")

            # Track if this is a submodule (important for path normalization)
            is_submodule = self._is_submodule_file(candidate_path)
            parent_module_name = None
            
            if is_submodule:
                print(f"[PyangLoader] Detected submodule: {module_file}")
                parent_name, _defs = self._parse_submodule_outline(candidate_path)
                if parent_name:
                    parent_module_name = parent_name
                    print(f"[PyangLoader] Submodule belongs to parent: {parent_name}")
                    # NOTE: We DO NOT switch to parent module - we parse the submodule directly
                    # This ensures paths are consistent when comparing module vs submodule
            
            # Setup pyang context with search directories.
            # IMPORTANT: Only use the module's own directory as the search path.
            # Adding parent directories risks finding a different (wrong) revision of
            # an imported module.  For example, when comparing openconfig-mpls-te.yang
            # from new/release/models/mpls/, adding new/release/models/ to the search
            # path would cause pyang to find network-instance/openconfig-mpls-rsvp.yang
            # (revision 2023-02-06) instead of mpls/openconfig-mpls-rsvp.yang
            # (revision 2018-06-05), leading to GROUPING_NOT_FOUND errors and missing
            # i_children after uses-expansion.
            search_dirs = [modules_dir]
            
            # Initialize pyang context
            self.pyang_ctx = create_pyang_context(search_dirs)
            
            print(f"[PyangLoader] Search directories: {search_dirs}")
            
            # Load the module using pyang
            module_stmt, ctx = load_yang_module(candidate_path, search_dirs)
            
            if module_stmt is None:
                parse_errors = getattr(ctx, "last_error_messages", []) or []
                if parse_errors:
                    preview = "; ".join(parse_errors[:3])
                    raise ValueError(f"Failed to parse module: {module_file}. pyang errors: {preview}")
                raise ValueError(f"Failed to parse module: {module_file}")
            
            print(f"[PyangLoader] Successfully parsed module: {module_name}")
            
            # Use the centralized expansion handler to apply recursive expansions
            # (uses, augment, deviation) based on XML configuration
            expansion_handler = PyangExpansionHandler()
            expansion_handler.expand_module(ctx.ctx, module_stmt)
            
            # Load typedef definitions for expansion during comparison
            # Pass both the module statement and the pyang context
            self.typedef_handler.load_typedef_definitions(module_stmt, ctx, modules_dir)
            
            # Traverse the module's top-level statements using substmts (not i_children)
            # substmts contains ALL module-level definitions (groupings, typedefs, etc.)
            # i_children would only contain expanded data tree nodes, missing definitions
            for sub_stmt in self.helper.get_substmts(module_stmt):
                self._traverse_statement(sub_stmt, module_name)

            # Also traverse any submodules included by this module/submodule.
            # When a submodule includes another submodule (submodule-includes-submodule),
            # the included submodule is loaded into the pyang context but its statements
            # are NOT present in module_stmt.substmts — they must be pulled from ctx.modules.
            self._traverse_included_submodules(
                module_stmt, ctx, module_name, visited=set(), modules_dir=modules_dir
            )

            # Sort by path for consistent ordering
            self.nodes_cache.sort(key=lambda x: x["path"])
            print(f"[PyangLoader] Extracted {len(self.nodes_cache)} nodes from {module_name}")
            
            return self.nodes_cache
        
        except FileNotFoundError as e:
            print(f"[PyangLoader] File not found: {e}")
            raise
        except ValueError as e:
            print(f"[PyangLoader] Parsing error: {e}")
            raise
        except Exception as e:
            print(f"[PyangLoader] Unexpected error loading {module_file}: {e}")
            print(f"[PyangLoader] Error type: {type(e).__name__}")
            traceback.print_exc()
            raise
    
    def _traverse_included_submodules(self, module_stmt, ctx, module_name: str,
                                      visited: set, modules_dir: str = None):
        """Traverse statements in any submodules included by module_stmt.

        This handles the submodule-includes-submodule case: when a submodule (e.g.,
        openconfig-aft-ipv4) includes another submodule (e.g., openconfig-aft-common),
        the included submodule's definitions are NOT part of module_stmt.substmts.

        IMPORTANT — correct file resolution:
        The included submodule is resolved from *disk* (same ``modules_dir`` as the
        including file) rather than from the pyang context.  The pyang context may
        cache a different (e.g. newer) revision of the same submodule name when OLD
        and NEW share a process, which would contaminate the OLD comparison with NEW
        content.  By resolving the include to the physical file that lives in the
        same directory as the including submodule we guarantee version correctness.

        Only structural definition statements are traversed (grouping, typedef,
        list, container, leaf, etc.).  Module-level metadata keywords (belongs-to,
        revision, import, organization, contact, description, reference,
        yang-version, prefix, namespace) and extension calls (tuple keywords) are
        skipped to avoid duplicate/collision paths.

        ``include`` child statements of the included submodule are NOT skipped
        here — they trigger the recursive call below so nested includes (A includes
        B includes C) are handled correctly.  A ``visited`` set prevents loops.
        """
        # Module-level metadata: skip when pulling content from an included submodule
        _SKIP_FROM_INCLUDED = frozenset({
            'belongs-to', 'revision', 'import',
            'organization', 'contact', 'description', 'reference',
            'yang-version', 'prefix', 'namespace',
        })

        # Determine the directory to search for included submodule files.
        # Use the pos.ref of module_stmt when available, falling back to modules_dir.
        search_dir = modules_dir
        if search_dir is None and hasattr(module_stmt, 'pos') and module_stmt.pos and module_stmt.pos.ref:
            search_dir = os.path.dirname(module_stmt.pos.ref)

        for sub_stmt in self.helper.get_substmts(module_stmt):
            if self.helper.get_keyword(sub_stmt) != 'include':
                continue
            inc_name = self.helper.get_argument(sub_stmt)
            if not inc_name or inc_name in visited:
                continue
            visited.add(inc_name)

            # --- Resolve the included submodule from the correct physical file ---
            # Priority 1: look in the same directory as the including file (version-safe).
            # Priority 2: fall back to the pyang context (cross-directory includes).
            inc_module_stmt = None
            inc_file_path = None

            if search_dir:
                candidate = os.path.join(search_dir, f"{inc_name}.yang")
                if os.path.isfile(candidate):
                    inc_file_path = candidate

            if inc_file_path:
                # Load the included submodule from disk into the same pyang context.
                # ctx.load_module() is idempotent — it returns the cached statement
                # if the file was already loaded, avoiding duplicate work.
                inc_module_stmt = ctx.load_module(inc_file_path)
                if inc_module_stmt is None:
                    # Fallback: try the pyang context cache
                    for key, mod in ctx.ctx.modules.items():
                        if key[0] == inc_name:
                            inc_module_stmt = mod
                            break
            else:
                # No file found on disk — use whatever pyang already loaded.
                for key, mod in ctx.ctx.modules.items():
                    if key[0] == inc_name:
                        inc_module_stmt = mod
                        break

            if inc_module_stmt is None:
                print(f"[PyangLoader] Warning: included submodule '{inc_name}' not found")
                continue

            # Determine the directory for this included submodule (for nested recursion)
            inc_dir = search_dir  # default: same directory
            if hasattr(inc_module_stmt, 'pos') and inc_module_stmt.pos and inc_module_stmt.pos.ref:
                resolved_dir = os.path.dirname(inc_module_stmt.pos.ref)
                if resolved_dir:
                    inc_dir = resolved_dir

            print(f"[PyangLoader] Traversing included submodule: {inc_name} "
                  f"(from {inc_dir or 'context'})")

            for child_stmt in self.helper.get_substmts(inc_module_stmt):
                kw = self.helper.get_keyword(child_stmt)
                # Skip module-level metadata and extension statements (tuple keywords).
                # NOTE: 'include' IS skipped here — nested includes are handled by the
                # recursive call below, not by traversing them as YANG nodes.
                if kw in _SKIP_FROM_INCLUDED or kw == 'include' or isinstance(kw, tuple):
                    continue
                self._traverse_statement(child_stmt, module_name)

            # Recurse: the included submodule may itself include more submodules.
            # Pass inc_dir so nested resolution stays pinned to the correct directory.
            self._traverse_included_submodules(
                inc_module_stmt, ctx, module_name, visited, modules_dir=inc_dir
            )

    def _get_expanded_children(self, statement):
        """Get expanded children (i_children) if available, otherwise fallback to substmts."""
        # Always use substmts for module-level children, i_children only for expanded uses
        if hasattr(statement, 'i_children') and statement.i_children:
            return statement.i_children
        return self.helper.get_substmts(statement)
    
    def _traverse_statement(self, statement, current_path: str = ""):
        """Recursively traverse pyang statements and build normalized nodes.
        
        For union member types and other cases where children have duplicate names,
        append an index [N] to make paths unique.
        """
        # Use dynamic _constants.RULE_STRUCTURAL_KEYWORDS; fallback to DEFAULT_STRUCTURAL_KEYWORDS if empty.
        structural = _constants.RULE_STRUCTURAL_KEYWORDS if _constants.RULE_STRUCTURAL_KEYWORDS else DEFAULT_STRUCTURAL_KEYWORDS
        keyword = self.helper.get_keyword(statement)
        raw_keyword = getattr(statement, 'keyword', None)
        is_extension_stmt = isinstance(raw_keyword, tuple)
        stmt_has_children = bool(self._get_expanded_children(statement))

        # Treat uncovered scalar extension statements (no child block) as
        # wrapper attributes, not standalone structural nodes.
        if is_uncovered_scalar_extension_statement(is_extension_stmt, stmt_has_children):
            return

        if keyword in structural or is_extension_stmt:
            arg = self.helper.get_argument(statement)
            if current_path and arg:
                # Preserve absolute extension arguments (e.g., tailf:annotate "/a/b")
                # so they remain distinct from real schema-node paths after normalization.
                if is_extension_stmt and str(arg).startswith('/'):
                    normalized_arg = arg
                elif keyword == 'type' and not current_path.endswith('/union'):
                    # For non-union type statements, use the keyword 'type' as the path
                    # segment instead of the type name (arg). This ensures that both old
                    # and new type nodes share the same path (e.g., 'leaf/type') so a
                    # type change (int32 -> int8) is detected as 'type changed' rather
                    # than separate 'type deleted' + 'type added' events.
                    # Union member types keep using arg to distinguish members.
                    normalized_arg = keyword
                else:
                    # Avoid generating module//node for regular structural keywords.
                    normalized_arg = arg.lstrip('/')
                path = f"{current_path}/{normalized_arg}"
            else:
                path = arg or current_path
            
            # Extract node data using pyang statement
            node_data = NodeNormalizer.extract_node_data(statement)
            node_data.update({
                "path": path,
                "type": keyword
            })
            # For 'type' keyword nodes, store the actual type name (e.g., 'leafref',
            # 'int32', 'string') in the node metadata. The path uses 'type' as the
            # segment (for stable matching), but the type name is needed by the
            # compatibility engine to determine the enclosing type context
            # (e.g., parent='leafref' for XPath path rules).
            # It is stored both at the top level and inside 'metadata' so that
            # _format_metadata can include it in the report line as:
            #   [file: foo.yang, old_line: 1, new_line: 2, type_name: leafref]
            if keyword == 'type' and arg:
                node_data['type_name'] = arg
                if 'metadata' in node_data and isinstance(node_data['metadata'], dict):
                    node_data['metadata']['type_name'] = arg

            # For enum/bit structural nodes, enrich with pyang's resolved value/position.
            # These are stored by pyang as i_value (enum) / i_position (bit) after validation
            # and may not have an explicit 'value'/'position' sub-statement in the YANG text.
            # Without this enrichment, auto-assigned enum values (e.g. 2→3 when a new enum
            # is inserted before an existing one) would not be detected as changes.
            if keyword == 'enum' and hasattr(statement, 'i_value') and statement.i_value is not None:
                node_data.setdefault('attributes', {})
                # Only set if not already present from an explicit 'value' sub-statement
                if 'value' not in node_data['attributes']:
                    node_data['attributes']['value'] = statement.i_value
            elif keyword == 'bit' and hasattr(statement, 'i_position') and statement.i_position is not None:
                node_data.setdefault('attributes', {})
                if 'position' not in node_data['attributes']:
                    node_data['attributes']['position'] = statement.i_position
            
            self.nodes_cache.append(node_data)
            
            # Recursively traverse expanded children (i_children includes expanded uses)
            # Track child names/args to detect duplicates (e.g., union members)
            children = list(self._get_expanded_children(statement))
            child_arg_counts = {}  # Track how many times we've seen each arg
            
            for sub_stmt in children:
                sub_keyword = self.helper.get_keyword(sub_stmt)
                sub_raw_keyword = getattr(sub_stmt, 'keyword', None)
                sub_is_extension_stmt = isinstance(sub_raw_keyword, tuple)
                sub_arg = self.helper.get_argument(sub_stmt)
                sub_has_children = bool(self._get_expanded_children(sub_stmt))
                should_traverse_sub = (
                    sub_keyword in structural
                    or (sub_is_extension_stmt and (sub_arg is not None or sub_has_children))
                )
                if should_traverse_sub:
                    
                    # Check if this creates a duplicate path (same parent, same keyword, same arg)
                    # This happens with union members: multiple 'type string' children
                    child_key = (sub_keyword, sub_arg)
                    
                    if child_key in child_arg_counts:
                        # This is a duplicate - pass index information to create unique path
                        child_arg_counts[child_key] += 1
                        idx = child_arg_counts[child_key]
                        # Traverse with index hint
                        self._traverse_statement_with_index(sub_stmt, path, idx)
                    else:
                        # First occurrence
                        child_arg_counts[child_key] = 0
                        # Check if there will be duplicates later
                        future_duplicates = sum(1 for s in children[children.index(sub_stmt)+1:]
                                              if self.helper.get_keyword(s) == sub_keyword and
                                                 self.helper.get_argument(s) == sub_arg)
                        if future_duplicates > 0:
                            # There will be duplicates, so add [0] to first one
                            self._traverse_statement_with_index(sub_stmt, path, 0)
                        else:
                            # No duplicates, traverse normally
                            self._traverse_statement(sub_stmt, path)
    
    def _traverse_statement_with_index(self, statement, current_path: str, index: int):
        """Traverse statement and append index to path for union members with duplicate names."""
        structural = _constants.RULE_STRUCTURAL_KEYWORDS if _constants.RULE_STRUCTURAL_KEYWORDS else DEFAULT_STRUCTURAL_KEYWORDS
        keyword = self.helper.get_keyword(statement)
        raw_keyword = getattr(statement, 'keyword', None)
        is_extension_stmt = isinstance(raw_keyword, tuple)
        stmt_has_children = bool(self._get_expanded_children(statement))

        if is_uncovered_scalar_extension_statement(is_extension_stmt, stmt_has_children):
            return

        if keyword in structural or is_extension_stmt:
            arg = self.helper.get_argument(statement)
            # Append index to make path unique: e.g., "route-distinguisher/union/string[0]"
            if current_path and arg:
                if is_extension_stmt and str(arg).startswith('/'):
                    normalized_arg = arg
                else:
                    normalized_arg = arg.lstrip('/')
                base_path = f"{current_path}/{normalized_arg}"
            else:
                base_path = arg or current_path
            path = f"{base_path}[{index}]"
            
            # Extract node data
            node_data = NodeNormalizer.extract_node_data(statement)
            node_data.update({
                "path": path,
                "type": keyword
            })

            # Enrich enum/bit nodes with pyang's resolved value/position (same as _traverse_statement)
            if keyword == 'enum' and hasattr(statement, 'i_value') and statement.i_value is not None:
                node_data.setdefault('attributes', {})
                if 'value' not in node_data['attributes']:
                    node_data['attributes']['value'] = statement.i_value
            elif keyword == 'bit' and hasattr(statement, 'i_position') and statement.i_position is not None:
                node_data.setdefault('attributes', {})
                if 'position' not in node_data['attributes']:
                    node_data['attributes']['position'] = statement.i_position
            
            self.nodes_cache.append(node_data)
            
            # Recursively traverse children (no index needed for children of indexed nodes)
            for sub_stmt in self._get_expanded_children(statement):
                self._traverse_statement(sub_stmt, path)
    
    def compare_nodes(self, old_nodes: List[Dict], new_nodes: List[Dict], 
                     similarity_threshold: float = 50.0) -> Dict[str, List[ChangeRecord]]:
        """Compare two lists of nodes and return organized change records."""
        # Build maps with normalized paths for comparison
        old_map = {}
        new_map = {}
        
        # Track original paths for reporting
        old_path_mapping = {}  # normalized_path -> original_path
        new_path_mapping = {}  # normalized_path -> original_path
        
        for node in old_nodes:
            orig_path = node["path"]
            norm_path = self._normalize_path_for_comparison(orig_path)
            old_map[norm_path] = node
            old_path_mapping[norm_path] = orig_path
        
        for node in new_nodes:
            orig_path = node["path"]
            norm_path = self._normalize_path_for_comparison(orig_path)
            new_map[norm_path] = node
            new_path_mapping[norm_path] = orig_path
        
        all_paths = sorted(set(old_map.keys()) | set(new_map.keys()))
        results = defaultdict(list)
        processed = set()
        
        # Handle path-level changes first
        self._handle_path_changes(old_map, new_map, results, processed, similarity_threshold)
        
        # Handle remaining nodes (same normalized path, different content)
        for norm_path in all_paths:
            if norm_path in processed:
                continue

            # Use the new node's original path for reporting (preserves namespace prefixes).
            # Fall back to old path if the node was deleted, or norm_path if neither is available.
            report_path = (
                new_path_mapping.get(norm_path)
                or old_path_mapping.get(norm_path)
                or norm_path
            )

            if norm_path in old_map and norm_path in new_map:
                # Check if the YANG statement type (keyword) changed
                # If so, treat as delete + add instead of modification
                old_keyword = old_map[norm_path].get('type', '')  # "type" field stores the keyword
                new_keyword = new_map[norm_path].get('type', '')
                
                if old_keyword and new_keyword and old_keyword != new_keyword:
                    # Statement type changed (e.g., choice -> leaf)
                    # Treat as deletion + addition
                    old_node = old_map[norm_path]
                    new_node = new_map[norm_path]
                    
                    # Create deletion record (use old original path for deleted node)
                    old_report_path = old_path_mapping.get(norm_path, norm_path)
                    results[old_report_path].append(ChangeRecord(
                        path=old_report_path,
                        change_type=ChangeType.DELETED,
                        old_value=old_node,
                        new_value=None,
                        node_type=old_keyword,
                        details=[]
                    ))
                    
                    # Create addition record (use new original path for added node)
                    new_report_path = new_path_mapping.get(norm_path, norm_path)
                    results[new_report_path].append(ChangeRecord(
                        path=new_report_path,
                        change_type=ChangeType.ADDED,
                        old_value=None,
                        new_value=new_node,
                        node_type=new_keyword,
                        details=[]
                    ))
                    
                    processed.add(norm_path)
                    continue
                
                changes = self._compare_single_node(old_map[norm_path], new_map[norm_path])
                
                # Separate enum/bit ChangeRecords (which have their own paths) from parent node changes
                parent_changes = []
                for change in changes:
                    if change.is_meaningful():
                        # Any change carrying its own absolute path should be
                        # emitted under that path instead of the currently
                        # iterated normalized key.
                        if change.path != norm_path and "/" in str(change.path):
                            results[change.path].append(change)
                        else:
                            # This is a change belonging to the parent node.
                            # Update the change's path to the original (non-normalized) path
                            # so the report header shows the original path with prefixes.
                            if change.path == norm_path:
                                change.path = report_path
                            parent_changes.append(change)
                
                # Add parent node changes under the original (non-normalized) report path
                if parent_changes:
                    results[report_path].extend(parent_changes)
                processed.add(norm_path)
        
        return self._cleanup_results(dict(results))

    def _cleanup_results(self, results: Dict[str, List[ChangeRecord]]) -> Dict[str, List[ChangeRecord]]:
        """Remove redundant duplicate and add/delete records from raw comparator output."""
        flat: List[Tuple[str, ChangeRecord]] = []
        for bucket_path, changes in results.items():
            for ch in changes:
                flat.append((bucket_path, ch))

        def _path_score(path: str) -> Tuple[int, int, int, int]:
            p = str(path or "")
            parts = [s for s in p.split('/') if s]
            has_prefix = any(':' in s for s in parts)
            has_double_slash = '//' in p
            depth = len(parts)
            # Prefer:
            # 1) prefixed paths when available,
            # 2) cleaner paths without accidental double slashes,
            # 3) shorter canonical paths (avoid duplicated parent/name segments).
            return (int(has_prefix), int(not has_double_slash), -depth, -len(p))

        def _norm_val(v):
            if isinstance(v, dict):
                ignored = {'path', 'type'}
                return tuple(sorted((str(k), _norm_val(val)) for k, val in v.items() if k not in ignored))
            if isinstance(v, list):
                return tuple(_norm_val(x) for x in v)
            return str(v)

        def _details_sig(details: List[Dict]) -> Tuple:
            sig = []
            for d in details or []:
                row = []
                for k in sorted(d.keys()):
                    if k == 'path':
                        continue
                    row.append((k, _norm_val(d.get(k))))
                sig.append(tuple(row))
            # Detail ordering is not semantically meaningful for dedup.
            return tuple(sorted(sig, key=repr))

        def _name_transition(change_record: ChangeRecord) -> Optional[Tuple[str, str]]:
            """Return (old_name, new_name) for simple name-change transitions."""
            def _is_name_path(path_value: Any) -> bool:
                if path_value == "name":
                    return True
                if isinstance(path_value, str):
                    pv = path_value.strip()
                    if pv in {"name", "['name']", '["name"]'}:
                        return True
                    simplified = pv.replace('[', '').replace(']', '').replace("'", '').replace('"', '').strip()
                    return simplified == "name" or simplified.endswith('.name')
                if isinstance(path_value, (list, tuple)) and len(path_value) == 1:
                    return str(path_value[0]).strip(" '\"") == "name"
                if isinstance(path_value, (list, tuple)):
                    return any(str(p).strip(" '\"") == "name" for p in path_value)
                return False

            if change_record.change_type != ChangeType.CHANGED:
                return None
            for detail in change_record.details or []:
                if detail.get('type') != 'attribute_changed':
                    continue
                if not _is_name_path(detail.get('path')):
                    continue
                if detail.get('old') is None or detail.get('new') is None:
                    continue
                return (str(detail.get('old')), str(detail.get('new')))
            return None

        def _dedup_path_key(path: str, change_record: Optional[ChangeRecord] = None) -> str:
            """Canonicalize path for dedup without collapsing distinct leaves.

            We still normalize obvious path-format variants (namespace prefixes,
            duplicate leading module segment) so true duplicates can collapse.
            """
            p = str(path or "").strip()
            if not p:
                return ""

            # Keep hierarchy, drop empty segments from accidental double slashes.
            segs = [s for s in p.split('/') if s]

            # Strip namespace prefix per segment (e.g., oc-if:interfaces -> interfaces).
            segs = [s.split(':', 1)[1] if ':' in s else s for s in segs]

            # Collapse duplicated module prefix: module/module/... -> module/...
            if len(segs) >= 2 and segs[0] == segs[1]:
                segs = segs[1:]

            # Canonicalize wrapper-rename path variant:
            #   .../<old-name>/<wrapper-kw>/<new-name>
            # ->.../<wrapper-kw>/<new-name>
            # This tail form is a duplicate rendering of the same wrapper rename
            # that can also be emitted as .../<wrapper-kw>/<new-name>.
            if change_record is not None:
                name_tr = _name_transition(change_record)
                wrapper_kw = str(change_record.node_type or '')
                if name_tr and wrapper_kw and len(segs) >= 3:
                    old_name, new_name = name_tr
                    if (
                        segs[-1] == new_name
                        and segs[-2] == wrapper_kw
                        and segs[-3] == old_name
                    ):
                        del segs[-3]

                # Canonicalize wrapper-rename parent-path variant:
                #   .../<parent>/<old-name>
                # ->.../<parent>
                # For wrappers (e.g., alias), changed records can be anchored
                # either at the parent path or at the old instance name path.
                # Both represent the same semantic change once name transition
                # is already encoded in details.
                if name_tr and len(segs) >= 1:
                    old_name, new_name = name_tr
                    if segs[-1] == old_name:
                        segs = segs[:-1]

                    # Canonicalize bare wrapper parent path to the renamed
                    # instance path so parent and instance variants dedupe.
                    #   .../<wrapper-kw> -> .../<wrapper-kw>/<new-name>
                    if wrapper_kw and segs and segs[-1] == wrapper_kw and new_name:
                        segs.append(new_name)

                    # Canonicalize wrapper-owner parent path variant:
                    #   .../<owner> -> .../<owner>/<wrapper-kw>/<new-name>
                    # Some uncovered wrappers are anchored at the owner path,
                    # while sibling records are anchored at wrapper instance
                    # path. Normalize both to wrapper instance path.
                    if wrapper_kw and new_name and wrapper_kw not in segs:
                        segs.extend([wrapper_kw, new_name])

            return '/'.join(segs)

        # 1) Collapse exact semantic duplicates that differ only by reporting path.
        best_by_sig: Dict[Tuple, Tuple[str, ChangeRecord]] = {}
        for bucket_path, ch in flat:
            dedup_path = _dedup_path_key(str(ch.path or bucket_path), ch)
            sig = (
                dedup_path,
                str(ch.change_type),
                str(ch.node_type or ""),
                _norm_val(ch.old_value),
                _norm_val(ch.new_value),
                _details_sig(ch.details),
            )
            curr = best_by_sig.get(sig)
            curr_path = str(ch.path or bucket_path)
            if curr is None:
                best_by_sig[sig] = (bucket_path, ch)
                continue
            prev_bucket, prev = curr
            prev_path = str(prev.path or prev_bucket)
            if _path_score(curr_path) > _path_score(prev_path):
                best_by_sig[sig] = (bucket_path, ch)

        deduped = list(best_by_sig.values())

        def _changed_transition_sig(ch: ChangeRecord) -> Tuple:
            trans = []
            for d in ch.details or []:
                dt = d.get('type')
                if dt == 'attribute_changed':
                    trans.append(('a', str(d.get('path')), str(d.get('old')), str(d.get('new'))))
                elif dt == 'constraint_changed':
                    trans.append(('c', str(d.get('path')), str(d.get('old')), str(d.get('new'))))
            return tuple(sorted(trans))

        def _type_match_score(ch: ChangeRecord) -> int:
            score = 0
            for val in (ch.old_value, ch.new_value):
                if isinstance(val, dict) and str(val.get('type') or '') == str(ch.node_type or ''):
                    score += 1
            return score

        # 1.5) For duplicate CHANGED transitions, keep the record whose payload
        # best matches the declared node_type.
        keep_idx = set(range(len(deduped)))
        by_changed_sig: Dict[Tuple, List[int]] = defaultdict(list)
        for i, (_p, ch) in enumerate(deduped):
            if ch.change_type != ChangeType.CHANGED:
                continue
            sig = (
                _dedup_path_key(str(ch.path or _p), ch),
                str(ch.node_type or ''),
                _changed_transition_sig(ch),
            )
            if not sig[2]:
                continue
            by_changed_sig[sig].append(i)

        for _sig, idxs in by_changed_sig.items():
            if len(idxs) < 2:
                continue
            best_i = max(
                idxs,
                key=lambda i: (
                    _type_match_score(deduped[i][1]),
                    _path_score(str(deduped[i][1].path or deduped[i][0]))
                )
            )
            for i in idxs:
                if i != best_i:
                    keep_idx.discard(i)

        deduped = [pair for i, pair in enumerate(deduped) if i in keep_idx]

        # 2) Drop add/delete pairs already represented by a changed record.
        def _is_structured_payload(v) -> bool:
            if isinstance(v, dict):
                return any(k in v for k in ('attributes', 'constraints', 'children', 'metadata'))
            if isinstance(v, list):
                return any(_is_structured_payload(x) for x in v)
            return False

        # 1.75) Remove synthetic wrapper detail lines that duplicate separately
        # emitted child structural records.
        present_node_types = {str(ch.node_type or '') for (_p, ch) in deduped}

        def _structural_name_transitions_by_type() -> Dict[str, Set[Tuple[str, str]]]:
            transitions: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
            for _bp, rec in deduped:
                if rec.change_type != ChangeType.CHANGED:
                    continue
                node_kw = str(rec.node_type or '')
                if not node_kw:
                    continue

                # Prefer explicit detail transitions.
                for d in rec.details or []:
                    if d.get('type') == 'attribute_changed' and str(d.get('path') or '') == 'name':
                        old_n = d.get('old')
                        new_n = d.get('new')
                        if old_n is not None and new_n is not None:
                            transitions[node_kw].add((str(old_n), str(new_n)))

                # Fallback to old/new attribute names when details are absent.
                if isinstance(rec.old_value, dict) and isinstance(rec.new_value, dict):
                    old_attrs = rec.old_value.get('attributes') if isinstance(rec.old_value.get('attributes'), dict) else {}
                    new_attrs = rec.new_value.get('attributes') if isinstance(rec.new_value.get('attributes'), dict) else {}
                    old_n = old_attrs.get('name')
                    new_n = new_attrs.get('name')
                    if old_n is not None and new_n is not None and str(old_n) != str(new_n):
                        transitions[node_kw].add((str(old_n), str(new_n)))

            return transitions

        structural_name_transitions = _structural_name_transitions_by_type()

        for _p, ch in deduped:
            if ch.change_type != ChangeType.CHANGED or not ch.details:
                continue
            filtered_details = []
            for d in ch.details:
                d_type = str(d.get('type') or '')
                d_path = str(d.get('path') or '')
                is_attr_or_cons = d_type.startswith('attribute_') or d_type.startswith('constraint_')
                if not is_attr_or_cons:
                    filtered_details.append(d)
                    continue
                if d_path not in present_node_types:
                    filtered_details.append(d)
                    continue

                # If this parent detail is just a scalar name transition that is
                # already represented by a structural changed record for the same
                # keyword, drop it to avoid duplicate reporting.
                if d_type == 'attribute_changed':
                    old_v = d.get('old')
                    new_v = d.get('new')
                    if (
                        old_v is not None
                        and new_v is not None
                        and not isinstance(old_v, (dict, list))
                        and not isinstance(new_v, (dict, list))
                        and (str(old_v), str(new_v)) in structural_name_transitions.get(d_path, set())
                    ):
                        continue

                if _is_structured_payload(d.get('old')) or _is_structured_payload(d.get('new')):
                    # Drop this noisy aggregate detail; child structural records
                    # (added/deleted/changed) already represent it.
                    continue
                filtered_details.append(d)
            ch.details = filtered_details

        def _attrs_from_value(node_val) -> Dict[str, str]:
            if not isinstance(node_val, dict):
                return {}
            attrs = node_val.get('attributes')
            if not isinstance(attrs, dict):
                return {}
            return {str(k): str(v) for k, v in attrs.items()}

        def _meta_file_line(ch: ChangeRecord) -> Tuple[Optional[str], Optional[int]]:
            val = ch.new_value if isinstance(ch.new_value, dict) else ch.old_value
            if not isinstance(val, dict):
                return None, None
            md = val.get('metadata')
            if not isinstance(md, dict):
                return None, None
            f = md.get('file')
            ln = md.get('line')
            try:
                ln = int(ln) if ln is not None else None
            except Exception:
                ln = None
            return (str(f) if f is not None else None, ln)

        changed_idx = [i for i, (_p, ch) in enumerate(deduped) if ch.change_type == ChangeType.CHANGED]
        added_idx = [i for i, (_p, ch) in enumerate(deduped) if ch.change_type == ChangeType.ADDED]
        deleted_idx = [i for i, (_p, ch) in enumerate(deduped) if ch.change_type == ChangeType.DELETED]
        drop_idx = set()

        for ci in changed_idx:
            _cp, ch = deduped[ci]
            transitions: Dict[str, Tuple[str, str]] = {}
            for d in ch.details or []:
                if d.get('type') == 'attribute_changed':
                    key = str(d.get('path') or '')
                    if key and key != 'path' and 'old' in d and 'new' in d:
                        transitions[key] = (str(d.get('old')), str(d.get('new')))
            if not transitions:
                continue

            for di in deleted_idx:
                if di in drop_idx:
                    continue
                _dp, drec = deduped[di]
                if drec.node_type != ch.node_type:
                    continue
                old_attrs = _attrs_from_value(drec.old_value)

                for ai in added_idx:
                    if ai in drop_idx:
                        continue
                    _ap, arec = deduped[ai]
                    if arec.node_type != ch.node_type:
                        continue
                    new_attrs = _attrs_from_value(arec.new_value)

                    if all(old_attrs.get(k) == ov and new_attrs.get(k) == nv for k, (ov, nv) in transitions.items()):
                        drop_idx.add(di)
                        drop_idx.add(ai)
                        break

                # Loose wrapper heuristic: if same keyword has changed+deleted+added at
                # the same source file/line, and changed includes a name transition that
                # matches deleted/added names, keep changed and drop add/delete noise.
                if di in drop_idx:
                    continue
                ch_file, ch_line = _meta_file_line(ch)
                if ch_file is None or ch_line is None:
                    continue
                name_tr = transitions.get('name')
                if not name_tr:
                    continue
                old_name, new_name = name_tr
                for ai in added_idx:
                    if ai in drop_idx:
                        continue
                    _ap, arec = deduped[ai]
                    if arec.node_type != ch.node_type:
                        continue
                    del_file, del_line = _meta_file_line(drec)
                    add_file, add_line = _meta_file_line(arec)
                    if del_file != ch_file or add_file != ch_file:
                        continue
                    if del_line != ch_line or add_line != ch_line:
                        continue
                    d_name = _attrs_from_value(drec.old_value).get('name')
                    a_name = _attrs_from_value(arec.new_value).get('name')
                    if d_name == old_name and a_name == new_name:
                        drop_idx.add(di)
                        drop_idx.add(ai)
                        break

        cleaned_pairs = [pair for i, pair in enumerate(deduped) if i not in drop_idx]
        cleaned_pairs = drop_redundant_wrapper_name_echoes(cleaned_pairs, _meta_file_line)

        def _canonical_output_path(path: str, change_record: ChangeRecord) -> str:
            p = str(path or "").strip()
            if not p:
                return p
            name_tr = _name_transition(change_record)
            if not name_tr:
                return p
            old_name, new_name = name_tr
            segs = [s for s in p.split('/') if s]
            if segs and segs[-1] == old_name:
                segs = segs[:-1]
            wrapper_kw = str(change_record.node_type or "")
            if wrapper_kw and segs and segs[-1] == wrapper_kw and new_name:
                segs.append(new_name)
            if wrapper_kw and new_name and wrapper_kw not in segs:
                segs.extend([wrapper_kw, new_name])
            return '/'.join(segs)

        cleaned = defaultdict(list)
        for bucket_path, ch in cleaned_pairs:
            out_path = _canonical_output_path(str(ch.path or bucket_path), ch)
            ch.path = out_path
            cleaned[out_path].append(ch)

        return dict(cleaned)
    
    def _handle_path_changes(self, old_map: Dict, new_map: Dict, results: Dict,
                           processed: Set, similarity_threshold: float):
        """Handle nodes that were added, removed, or moved between paths."""
        deleted_paths = [p for p in old_map.keys() if p not in new_map.keys()]
        added_paths = [p for p in new_map.keys() if p not in old_map.keys()]

        # -----------------------------------------------------------------------
        # Pre-compute a lookup: (node_type, node_name) → list of new paths in
        # added_paths that share that (type, name) combination.
        #
        # "node_name" is the last path segment — the YANG identifier of the node
        # (container name, leaf name, grouping name, etc.).
        #
        # Gating rule applied BEFORE similarity scoring:
        #
        #   If the old node's (type, name) appears in added_paths, restrict the
        #   similarity candidates for that old node to ONLY those same-name new
        #   paths.  Similarity is still computed to confirm the match (the name
        #   could be a coincidence), but unrelated nodes with different names are
        #   excluded from consideration entirely.
        #
        #   If the old node's name does NOT appear in added_paths at all, fall
        #   back to the existing behaviour: compare against all added_paths
        #   (rename / structural-move detection).
        #
        # Rationale: Without this gate, two containers that only have 'name' and
        # 'description' attributes (both excluded from similarity scoring) both
        # produce empty filtered-attribute sets → 100 % similarity → false
        # pairings such as old 'container config' ↔ new 'container auto-bandwidth'.
        # -----------------------------------------------------------------------
        added_name_type_to_paths: Dict = defaultdict(list)
        for np in added_paths:
            np_name = np.split('/')[-1] if '/' in np else np
            np_type = new_map[np].get("type", "")
            added_name_type_to_paths[(np_type, np_name)].append(np)

        # Import SequenceMatcher once for name-similarity scoring in the fallback pool.
        from difflib import SequenceMatcher as _SequenceMatcher

        # Find similarity-based pairings
        # Note: 'processed' is checked here too so that children of already-paired
        # renamed nodes are not re-processed as separate deleted/added entries.
        similarity_pairs = []

        def _af_marker(path: str):
            parts = path.split('/')
            has_v4 = any('ipv4' in p for p in parts)
            has_v6 = any('ipv6' in p for p in parts)
            if has_v4 and not has_v6:
                return 'ipv4'
            if has_v6 and not has_v4:
                return 'ipv6'
            return None

        def _rib_view_marker(path: str):
            parts = set(path.split('/'))
            for marker in (
                'adj-rib-in-post',
                'adj-rib-in-pre',
                'adj-rib-out-post',
                'adj-rib-out-pre',
                'loc-rib',
            ):
                if marker in parts:
                    return marker
            return None

        def _canonical_identifier(name: str) -> str:
            """Normalize identifier spelling style (case and -/_ separators)."""
            if not name:
                return ""
            return re.sub(r"[-_]+", "", str(name)).lower()

        def _path_affinity_adjust(old_path: str, new_path: str) -> float:
            """Return small score adjustment for known structural refactor patterns."""
            adjust = 0.0

            # In historical OpenConfig RIB refactors, legacy ipv[46]-routes maps
            # most closely to loc-rib branches, not adj-rib branches.
            legacy_routes = ('/ipv4-routes/' in old_path) or ('/ipv6-routes/' in old_path)
            if legacy_routes:
                if '/loc-rib/' in new_path:
                    adjust += 15.0
                if '/adj-rib-' in new_path:
                    adjust -= 20.0

            # Prefer canonical bgp-rib-top wrapper mapping when present.
            if '/bgp-rib/afi-safis/' in old_path and '/bgp-rib-top/bgp-rib/afi-safis/' in new_path:
                adjust += 10.0

            return adjust

        for old_path in deleted_paths:
            if old_path in processed:
                continue
            old_node = old_map[old_path]
            old_node_type = old_node.get("type", "")
            old_node_name = old_path.split('/')[-1] if '/' in old_path else old_path

            # Determine the candidate pool for this old node:
            # - If same-name candidates exist in added_paths → restrict to them.
            # - Otherwise → use all added_paths (rename detection).
            same_name_candidates = added_name_type_to_paths.get((old_node_type, old_node_name), [])
            in_fallback_pool = not same_name_candidates
            candidate_pool = same_name_candidates if same_name_candidates else added_paths

            for new_path in candidate_pool:
                new_node = new_map[new_path]

                # Only pair nodes of the same type (already guaranteed for
                # same_name_candidates, but re-check for the fallback pool)
                if old_node_type != new_node.get("type", ""):
                    continue

                # Guard against common false pairings in repeated schema branches
                # (e.g., ipv4 vs ipv6, adj-rib-in-post vs adj-rib-out-pre).
                old_af = _af_marker(old_path)
                new_af = _af_marker(new_path)
                if old_af and new_af and old_af != new_af:
                    continue
                old_view = _rib_view_marker(old_path)
                new_view = _rib_view_marker(new_path)
                if old_view and new_view and old_view != new_view:
                    continue

                # Special handling for type nodes - allow reasonable path changes and type name changes
                if old_node_type == "type":
                    old_segments = old_path.split('/')
                    new_segments = new_path.split('/')
                    
                    # Must have same number of path segments
                    if len(old_segments) != len(new_segments):
                        continue
                    
                    old_parent_segments = old_segments[:-1]  # All except type name
                    new_parent_segments = new_segments[:-1]  # All except type name
                    old_type_name = old_segments[-1]
                    new_type_name = new_segments[-1]
                    
                    # Check if this is a reasonable type pairing
                    # Case 1: Same type name, potentially different parent path (path change)
                    # Case 2: Same parent path, different type name (type name change)
                    # Case 3: Similar parent path with different type name (both path and type change)
                    
                    parent_paths_identical = old_parent_segments == new_parent_segments
                    type_names_identical = old_type_name == new_type_name
                    
                    if parent_paths_identical and type_names_identical:
                        # This shouldn't happen since paths would be identical
                        continue
                    elif parent_paths_identical and not type_names_identical:
                        # Case 2: Type name change with same parent (e.g., int8 -> int16)
                        # Always allow this - it's a clear type change
                        pass
                    elif not parent_paths_identical and type_names_identical:
                        # Case 1: Path change with same type name (e.g., config/my-leaf/uint8 -> state/my-leaf/uint8)
                        # This should only be allowed for very minor path changes
                        # such as moving a type within the same leaf between config/state containers
                        
                        different_segments = sum(1 for old_seg, new_seg in zip(old_parent_segments, new_parent_segments) if old_seg != new_seg)
                        
                        # Be very strict: only allow if paths differ in exactly ONE segment
                        # AND that segment is likely a config/state container difference
                        # This prevents false matches like:
                        #   state/traffic-class/uint8 -> config/ttl-value/uint8 (WRONG - different leaves!)
                        # But allows:
                        #   config/my-leaf/uint8 -> state/my-leaf/uint8 (OK - same leaf, different container)
                        
                        if different_segments > 1:
                            # More than 1 segment differs - definitely not the same type
                            continue
                        
                        # Even with 1 difference, check if it's a reasonable pairing
                        # The differing segment should be at a container level (config/state),
                        # not at the leaf name level (last segment before type name)
                        if different_segments == 1:
                            # Find which segment differs
                            diff_index = next(i for i, (old_seg, new_seg) in enumerate(zip(old_parent_segments, new_parent_segments)) if old_seg != new_seg)
                            
                            # If the difference is in the last parent segment (leaf name), reject it
                            # This is a different leaf, not a container move
                            if diff_index == len(old_parent_segments) - 1:
                                continue
                            
                            # If the differing segment is not a config/state-like change, be cautious
                            # Allow common container pairs like config/state, running/candidate, etc.
                            common_container_pairs = {
                                ('config', 'state'), ('state', 'config'),
                                ('running', 'candidate'), ('candidate', 'running'),
                                ('operational', 'intended'), ('intended', 'operational')
                            }
                            old_diff_seg = old_parent_segments[diff_index]
                            new_diff_seg = new_parent_segments[diff_index]
                            
                            if (old_diff_seg, new_diff_seg) not in common_container_pairs:
                                # Not a recognized container pairing - reject to be safe
                                continue
                    else:
                        # Case 3: Both path and type name changed
                        # This is almost always a false match - one type was deleted and another was added
                        # Only allow if the paths are EXTREMELY similar (e.g., just a typedef expansion like uint8 -> oc-types:uint8)
                        different_segments = sum(1 for old_seg, new_seg in zip(old_parent_segments, new_parent_segments) if old_seg != new_seg)
                        
                        # If ANY parent path segment differs, reject the pairing
                        # This prevents false matches like:
                        #   mpls-header-config/traffic-class/uint8 -> mpls-header-config/end-label-value/oc-mpls:mpls-label
                        if different_segments > 0:
                            continue
                        
                        # Even if parent paths are identical, be very strict about type name changes
                        # Only allow if types are clearly related (e.g., typedef expansion or base type change)
                        # This would handle: config/my-value/uint8 -> config/my-value/oc-types:uint8
                        # But reject: config/traffic-class/uint8 -> config/traffic-class/oc-mpls:mpls-tc (different semantics)
                        # For now, we're being conservative and rejecting this case too
                        # (If needed, can add smarter typedef resolution logic here)
                        continue
                
                # Compute attribute similarity.
                # Key insight: 'description' is included here (NOT excluded) because it
                # is the most discriminating field for rename detection.
                # - serial_no and serial-no have the SAME description → high similarity
                # - serial_no and part-no have DIFFERENT descriptions → lower similarity
                # Excluding description (as was done before) caused false pairings because
                # after excluding name+description, all simple leaves (type=string, no
                # constraints) had empty attribute sets → 100% similarity for all pairs.
                #
                # 'name' is still excluded from the attribute similarity because it's the
                # field that changes during a rename — we handle it separately via fuzzy
                # matching below.
                attr_similarity = SimilarityCalculator.calculate_similarity(
                    old_node.get("attributes", {}),
                    new_node.get("attributes", {}),
                    # Exclude only identity fields and condition keywords.
                    # 'description' is intentionally kept to discriminate between nodes
                    # that have the same type but different semantic purposes.
                    {"name", "path", "when", "must", "if-feature"}
                )

                # Add fuzzy name similarity as a small additional signal (20% weight).
                # The name attribute is the field that changes during a rename, so it
                # should have lower weight than the content (type, description, constraints).
                # The fuzzy component helps break ties when two nodes have identical
                # content but different names.
                old_name = old_path.split('/')[-1] if '/' in old_path else old_path
                new_name = new_path.split('/')[-1] if '/' in new_path else new_path
                name_ratio = _SequenceMatcher(None, old_name, new_name).ratio()
                name_similarity = name_ratio * 100.0  # 0-100

                # Path-context similarity helps disambiguate repeated structural
                # nodes (e.g., many `routes` containers with identical attributes).
                # Without this tie-breaker, greedy matching can pair the wrong
                # sibling branch and leave a true counterpart as added/deleted.
                path_ratio = _SequenceMatcher(None, old_path, new_path).ratio()
                path_similarity = path_ratio * 100.0  # 0-100

                # Constraint-key similarity is a strong signal for structurals
                # with repeated shapes where description text can collide.
                old_cons = old_node.get("constraints", {}) if isinstance(old_node.get("constraints"), dict) else {}
                new_cons = new_node.get("constraints", {}) if isinstance(new_node.get("constraints"), dict) else {}
                old_cons_keys = set(old_cons.keys())
                new_cons_keys = set(new_cons.keys())
                if old_cons_keys or new_cons_keys:
                    cons_intersection = len(old_cons_keys & new_cons_keys)
                    cons_union = len(old_cons_keys | new_cons_keys)
                    constraint_similarity = (100.0 * cons_intersection / cons_union) if cons_union else 100.0
                else:
                    constraint_similarity = 100.0

                if in_fallback_pool:
                    # In the fallback pool: content dominates, with path/name as
                    # tie-breakers for repeated nodes and mild renames.
                    # The name is the field that changed (rename), so content dominates.
                    similarity = (
                        0.60 * attr_similarity
                        + 0.20 * constraint_similarity
                        + 0.15 * name_similarity
                        + 0.05 * path_similarity
                    )
                else:
                    # In the same-name pool, include path-context to avoid arbitrary
                    # pairing among many identical same-name siblings.
                    similarity = (
                        0.75 * attr_similarity
                        + 0.15 * constraint_similarity
                        + 0.10 * path_similarity
                    )

                similarity += _path_affinity_adjust(old_path, new_path)
                if similarity > 100.0:
                    similarity = 100.0
                elif similarity < 0.0:
                    similarity = 0.0

                similarity_pairs.append((
                    similarity,
                    old_path,
                    new_path,
                    in_fallback_pool,
                    path_similarity,
                ))
        
        # Sort by similarity and greedily pair.
        similarity_pairs.sort(reverse=True, key=lambda x: x[0])

        paired_old = set()
        paired_new = set()
        paired_map = {}

        # 1) Primary pairing by similarity threshold.
        for similarity, old_path, new_path, in_fallback_pool, path_similarity in similarity_pairs:
            # For same-name pools, allow pairing below the global similarity threshold
            # when path-context is very close. This avoids false added/deleted for
            # existing nodes whose schema details changed significantly (e.g., key
            # expansion on an existing list).
            allow_low_similarity_same_name = (not in_fallback_pool and path_similarity >= 70.0)

            # For fallback (rename) pools, allow low-sim pairing only for strong
            # same-parent rename candidates. This preserves CHANGED classification
            # when scalar attributes legitimately changed (e.g., alias name + oid),
            # while avoiding broad cross-branch false matches.
            old_parent = old_path.rsplit('/', 1)[0] if '/' in old_path else ''
            new_parent = new_path.rsplit('/', 1)[0] if '/' in new_path else ''
            old_name = old_path.split('/')[-1] if '/' in old_path else old_path
            new_name = new_path.split('/')[-1] if '/' in new_path else new_path
            name_ratio = _SequenceMatcher(None, old_name, new_name).ratio() * 100.0
            canonical_name_match = (
                _canonical_identifier(old_name)
                and _canonical_identifier(old_name) == _canonical_identifier(new_name)
            )
            allow_low_similarity_fallback_rename = (
                in_fallback_pool
                and old_parent == new_parent
                and (
                    (path_similarity >= 90.0 and name_ratio >= 45.0)
                    or canonical_name_match
                )
            )

            if (
                similarity < similarity_threshold
                and not allow_low_similarity_same_name
                and not allow_low_similarity_fallback_rename
            ):
                continue
            if old_path in processed or new_path in processed:
                continue
            if old_path in paired_old or new_path in paired_new:
                continue
            paired_old.add(old_path)
            paired_new.add(new_path)
            paired_map[old_path] = new_path

        # 2) Descendant propagation for renamed/moved parents.
        # If parent old->new is paired, try pairing children by suffix mapping:
        #   old_parent/.../child  <->  new_parent/.../child
        # This preserves nested diff visibility even when parent names changed.
        parent_pairs = sorted(paired_map.items(), key=lambda item: item[0].count('/'))
        for old_parent, new_parent in parent_pairs:
            old_prefix = old_parent + '/'
            for old_path in deleted_paths:
                if old_path in paired_old:
                    continue
                if not old_path.startswith(old_prefix):
                    continue
                suffix = old_path[len(old_parent):]
                new_path = new_parent + suffix
                if new_path not in new_map:
                    # Fallback: if exact suffix mapping misses (common for type arg
                    # rename like .../string -> .../leafref), try pairing against
                    # a unique same-kind sibling under the mapped new parent.
                    old_parent_path = old_path.rsplit('/', 1)[0] if '/' in old_path else ''
                    if not old_parent_path.startswith(old_prefix):
                        continue
                    parent_suffix = old_parent_path[len(old_parent):]
                    mapped_new_parent = new_parent + parent_suffix
                    old_node_type = old_map[old_path].get("type", "")
                    candidates = [
                        cand for cand in added_paths
                        if cand not in paired_new
                        and cand in new_map
                        and new_map[cand].get("type", "") == old_node_type
                        and ('/' in cand and cand.rsplit('/', 1)[0] == mapped_new_parent)
                    ]
                    if len(candidates) != 1:
                        continue
                    new_path = candidates[0]
                if new_path in paired_new:
                    continue
                if new_path not in added_paths:
                    continue
                if old_map[old_path].get("type", "") != new_map[new_path].get("type", ""):
                    continue
                paired_old.add(old_path)
                paired_new.add(new_path)
                paired_map[old_path] = new_path

        # 3) Compare all paired nodes (including propagated descendant pairs).
        for old_path in sorted(paired_map.keys()):
            new_path = paired_map[old_path]
            if old_path in processed or new_path in processed:
                continue
            processed.add(old_path)
            processed.add(new_path)

            # Compare the paired nodes
            changes = self._compare_single_node(old_map[old_path], new_map[new_path])

            # Decide whether to surface a path change: only if any segment before the last differs.
            def _has_significant_path_change(old_path_str: str, new_path_str: str) -> bool:
                """Return True if structural (ancestor) part of path changed.

                A change only in the final segment (e.g. type name int32 -> uint32)
                is suppressed per requirement; ancestor segment change (e.g. container rename)
                is considered significant.
                """
                if old_path_str == new_path_str:
                    return False
                old_segments = old_path_str.split('/')
                new_segments = new_path_str.split('/')
                if not old_segments or not new_segments:
                    return old_path_str != new_path_str
                if min(len(old_segments), len(new_segments)) < 2:
                    # Single segment path rename counts as significant
                    return True
                old_ancestors = old_segments[:-1]
                new_ancestors = new_segments[:-1]
                if len(old_ancestors) != len(new_ancestors):
                    return True
                if any(old_seg != new_seg for old_seg, new_seg in zip(old_ancestors, new_ancestors)):
                    return True
                return False  # only leaf segment changed

            significant_path_change = _has_significant_path_change(old_path, new_path)
            path_detail = None
            if significant_path_change:
                path_detail = {
                    "type": "attribute_changed",
                    # Use 'node-path' to distinguish the node's schema location change
                    # from the inner XPath expressions (leafref path, augment target, etc.)
                    # which are reported as 'path'. This prevents false XPath semantic
                    # analysis on structural renames (grouping/node relocation).
                    "path": "node-path",
                    "old": old_path,
                    "new": new_path
                }

            if significant_path_change:
                if changes:
                    # Attach old/new path metadata to each existing record and prepend path detail
                    for ch in changes:
                        ch.old_path = old_path
                        ch.new_path = new_path
                    if path_detail:
                        if changes[0].details is None:
                            changes[0].details = []
                        changes[0].details.insert(0, path_detail)
                        changes[0].change_type = ChangeType.CHANGED
                else:
                    if path_detail:
                        changes.append(ChangeRecord(
                            change_type=ChangeType.CHANGED,
                            path=old_path,
                            node_type=old_map[old_path].get('type', 'node'),
                            old_path=old_path,
                            new_path=new_path,
                            old_value=old_map[old_path],
                            new_value=new_map[new_path],
                            details=[path_detail]
                        ))
            else:
                # Same path; if no differences still surface unchanged? Keep existing behaviour
                if not changes:
                    changes.append(ChangeRecord(
                        change_type=ChangeType.CHANGED,
                        path=old_path,
                        node_type=old_map[old_path].get('type', 'node'),
                        old_value=old_map[old_path],
                        new_value=new_map[new_path]
                    ))
            results[old_path].extend(changes)

        def _node_signature(node: Dict, path: str):
            """Best-effort signature to detect alias duplicates after expansion."""
            attrs = node.get('attributes', {}) or {}
            name_obj = attrs.get('name') if isinstance(attrs, dict) else None
            if isinstance(name_obj, dict):
                name_value = name_obj.get('value')
                src_file = name_obj.get('file')
                src_line = name_obj.get('line')
            else:
                name_value = path.split('/')[-1] if path else ''
                src_file = None
                src_line = None
            return (
                node.get('type', ''),
                name_value or '',
                src_file or '',
                int(src_line) if isinstance(src_line, int) else -1,
            )

        matched_new_signatures = {
            _node_signature(new_map[p], p)
            for p in paired_new
            if p in new_map
        }
        
        # Handle remaining unpaired deletions and additions.
        # Skip paths that were suppressed by the child-suppression pre-pass
        # (they are already in 'processed' but not in 'paired_old'/'paired_new').
        for old_path in deleted_paths:
            if old_path not in paired_old and old_path not in processed:
                results[old_path].append(ChangeRecord(
                    change_type=ChangeType.DELETED,
                    path=old_path,
                    node_type=old_map[old_path].get('type', 'node'),
                    old_value=old_map[old_path]
                ))
                processed.add(old_path)

        for new_path in added_paths:
            if new_path not in paired_new and new_path not in processed:
                new_node = new_map[new_path]

                # Skip alias duplicates: if this exact source node is already
                # represented via a paired path-change record, do not report it
                # again as a pure added node.
                if _node_signature(new_node, new_path) in matched_new_signatures:
                    processed.add(new_path)
                    continue

                added_record = ChangeRecord(
                    change_type=ChangeType.ADDED,
                    path=new_path,
                    node_type=new_node.get('type', 'node'),
                    new_value=new_node
                )
                results[new_path].append(added_record)
                
                # Check if the added node has a type that references a typedef
                # If the typedef exists in both old and new versions and changed,
                # add typedef expansion details to show enum/bit changes
                new_attrs = new_node.get("attributes", {})
                new_type = new_attrs.get("type", {})
                
                if isinstance(new_type, dict) and new_type.get("name"):
                    type_name = new_type.get("name")
                    # Check if this is a typedef reference (has prefix or exists in new cache)
                    is_typedef = isinstance(type_name, str) and (':' in type_name or type_name in self.typedef_handler.new_typedef_cache)
                    
                    if is_typedef:
                        # Check if this typedef also existed in the old version
                        old_typedef_exists = type_name in self.typedef_handler.old_typedef_cache
                        if not old_typedef_exists and ':' in type_name:
                            simple_name = type_name.split(':')[1]
                            old_typedef_exists = simple_name in self.typedef_handler.old_typedef_cache
                        
                        if old_typedef_exists:
                            # Typedef exists in both versions - compare them
                            # Create a dummy old type with the same typedef name to trigger comparison
                            old_type_for_comparison = {"name": type_name}
                            
                            typedef_changes = self.typedef_handler.compare_typedef_references(
                                old_type_for_comparison, new_type, new_path
                            )
                            
                            if typedef_changes:
                                # Typedef definition changed - append these as details
                                results[new_path].extend(typedef_changes)
                
                processed.add(new_path)
    
    def _compare_single_node(self, old_node: Dict, new_node: Dict) -> List[ChangeRecord]:
        """Compare two individual nodes and return change records."""
        changes = []
        
        old_attrs = old_node.get("attributes", {})
        new_attrs = new_node.get("attributes", {})
        
        # Check if type references a typedef and expand it
        old_type = old_attrs.get("type", {})
        new_type = new_attrs.get("type", {})
        
        # Expand typedef references to show enum/bit details
        typedef_changes = self.typedef_handler.compare_typedef_references(
            old_type, new_type, old_node.get("path", "")
        )
        if typedef_changes:
            changes.extend(typedef_changes)
        
        # Handle symbolic types (enum, bit) and union types in a unified way
        if isinstance(old_type, dict) and isinstance(new_type, dict):
            # Debug for duplex-mode
            if 'duplex' in old_node.get("path", "").lower():
                print(f"\n[DEBUG] Processing duplex-mode type comparison")
                print(f"  old_type keys: {old_type.keys()}")
                print(f"  new_type keys: {new_type.keys()}")
            
            # Import symbolic keywords from constants
            from .constants import SYMBOLIC_KEYWORDS

            # Determine whether the inline symbolic entries (enum/bit) inside this type
            # are ALSO represented as dedicated structural path nodes in the comparison
            # tree.  This happens when the type name is a base symbolic type keyword
            # ("enumeration" or "bits") — pyang traversal creates child nodes at paths
            # like  <parent>/enumeration/<ENTRY_NAME>  which are compared independently.
            # Emitting ChangeRecords here as well would produce duplicates; worse, the
            # attribute-dict comparison incorrectly reports 'status' as *changed* (was
            # None → deprecated) instead of *added*, because the old entry dict lacks
            # the 'status' key while the new one has it, which the dict-diff reads as
            # an attribute change rather than a constraint addition.
            # Skip inline symbolic comparison for these base types so that the accurate
            # structural-path records (with "enumeration"/"bits" in the path) are the
            # sole source of truth.
            _INLINE_SYMBOLIC_TYPE_NAMES = frozenset({'enumeration', 'bits'})
            old_type_name = old_type.get('name', '')
            new_type_name_val = new_type.get('name', '')
            _skip_inline_symbolic = (
                old_type_name in _INLINE_SYMBOLIC_TYPE_NAMES or
                new_type_name_val in _INLINE_SYMBOLIC_TYPE_NAMES
            )

            # Process all symbolic types (enum, bit) using unified logic
            for symbolic_key in SYMBOLIC_KEYWORDS:
                old_symbolic = old_type.get(symbolic_key, [])
                new_symbolic = new_type.get(symbolic_key, [])
                
                # Debug for duplex-mode
                if 'duplex' in old_node.get("path", "").lower():
                    print(f"\n[DEBUG] Checking {symbolic_key}: old={len(old_symbolic)}, new={len(new_symbolic)}")
                
                if old_symbolic or new_symbolic:
                    # Skip inline enum/bit comparison when the type uses a base symbolic
                    # type name ('enumeration' / 'bits') — those entries are reported more
                    # accurately via dedicated structural path nodes (e.g. .../enumeration/ENTRY).
                    if _skip_inline_symbolic:
                        continue
                    symbolic_changes = self._compare_symbolic_entries(
                        old_symbolic, new_symbolic, symbolic_key, old_node, new_node
                    )
                    # symbolic_changes is now a List[ChangeRecord], use extend instead of append
                    if symbolic_changes:
                        changes.extend(symbolic_changes)
            
            # Handle union member types (nested 'type' list within type definition)
            # Union types have a list of type children, e.g., union { type string; type int32; }
            old_union_types = old_type.get("type", [])
            new_union_types = new_type.get("type", [])
            
            if old_union_types or new_union_types:
                union_changes = self._compare_union_types(
                    old_union_types, new_union_types, old_node, new_node
                )
                if union_changes:
                    changes.append(union_changes)
        
        # Compare other attributes (excluding enum/bit-containing types)
        filtered_old_attrs = self._filter_special_type_attrs(old_attrs)
        filtered_new_attrs = self._filter_special_type_attrs(new_attrs)

        # Collect all detail entries from attributes and constraints so they appear together
        all_change_details: List[Dict] = []

        if filtered_old_attrs != filtered_new_attrs:
            attr_strategy = AttributeComparisonStrategy()
            attr_changes = attr_strategy.compare(filtered_old_attrs, filtered_new_attrs)
            
            if attr_changes:
                # Convert attribute changes to the expected format
                for attr_change in attr_changes:
                    kind = attr_change.node_type or 'attribute'
                    all_change_details.append({
                        "type": f"{kind}_{attr_change.change_type.value}",
                        "path": attr_change.path,
                        "old": attr_change.old_value,
                        "new": attr_change.new_value
                    })

        # Also compare constraint dictionaries (they were stored separately in NodeNormalizer)
        old_constraints = old_node.get("constraints", {}) or {}
        new_constraints = new_node.get("constraints", {}) or {}
        if old_constraints != new_constraints:
            constraint_strategy = AttributeComparisonStrategy()
            constraint_changes = constraint_strategy.compare(old_constraints, new_constraints)
            if constraint_changes:
                for c_change in constraint_changes:
                    kind = c_change.node_type or 'constraint'
                    all_change_details.append({
                        "type": f"{kind}_{c_change.change_type.value}",
                        "path": c_change.path,
                        "old": c_change.old_value,
                        "new": c_change.new_value
                    })

        if all_change_details:
            # Promote uncovered structural-like keyword deltas (e.g. default-value 30 -> 35)
            # from a parent node attribute diff into their own structural change shape:
            #   (default-value changed)
            #     attribute changed: ['name'] -> 35 (was 30)
            promoted_node_type = old_node.get("type", "node")
            promoted_details = all_change_details
            promoted_old_value = old_node
            promoted_new_value = new_node
            promoted_parent_path = old_node.get("path", "")
            collapse_to_wrapper_path = False
            wrapper_old_name = None
            wrapper_new_name = None

            structural_set = (_constants.RULE_STRUCTURAL_KEYWORDS
                              if _constants.RULE_STRUCTURAL_KEYWORDS
                              else DEFAULT_STRUCTURAL_KEYWORDS)
            constraint_set = (_constants.CONSTRAINT_KEYWORDS
                              if _constants.CONSTRAINT_KEYWORDS
                              else DEFAULT_CONSTRAINT_KEYWORDS)
            attribute_set = (_constants.RULE_ATTRIBUTES
                             if _constants.RULE_ATTRIBUTES
                             else DEFAULT_ATTRIBUTE_KEYWORDS)
            # Keep structural keywords eligible for promotion. Only filter out
            # known attribute/constraint keys; otherwise declared structural
            # keywords (e.g., default-value from XML rules) would regress back
            # to flat attribute diffs.
            non_structural_keys = set(constraint_set) | set(attribute_set)

            candidate_paths = {
                str(d.get("path", ""))
                for d in all_change_details
                if str(d.get("path", ""))
                and str(d.get("path", "")) not in non_structural_keys
                and str(d.get("type", "")).startswith("attribute_")
            }

            def _norm_kw(token: str) -> str:
                if isinstance(token, tuple) and token:
                    return str(token[-1])
                t = str(token or "")
                return t.split(":", 1)[1] if ":" in t else t

            # Infer structural keyword from nested payload details
            # (e.g. attribute added ['children'] -> {"type": "default-value", ...}).
            inferred_structurals = set()
            for detail in all_change_details:
                for side in ("old", "new"):
                    val = detail.get(side)
                    if isinstance(val, dict):
                        vtype = val.get("type")
                        if isinstance(vtype, str) and vtype and _norm_kw(vtype) not in non_structural_keys:
                            inferred_structurals.add(_norm_kw(vtype))

            # Generic wrapper inference: classify wrapper attribute entries with
            # explicit, deterministic rules so uncovered extension handling
            # remains stable:
            # 1) known attribute/constraint keys are never structural
            # 2) dict/list statement-shaped payloads are structural children
            # 3) scalar wrapper entries are attributes (no child block semantics)
            def _extract_wrapper_child_nodes(wrapper_val: Dict):
                out = []
                if not isinstance(wrapper_val, dict):
                    return out

                def _is_statement_payload(val) -> bool:
                    return (
                        isinstance(val, dict)
                        and (
                            isinstance(val.get("attributes"), dict)
                            or isinstance(val.get("constraints"), dict)
                            or isinstance(val.get("children"), list)
                        )
                    )

                def _classify_wrapper_entry(entry_kw: str, entry_val):
                    """Return (is_structural_child, normalized_payload_dict_or_none)."""
                    if entry_kw in non_structural_keys:
                        return False, None

                    if isinstance(entry_val, list):
                        # Any statement-like item means this key carries structural payload.
                        for item in entry_val:
                            if _is_statement_payload(item):
                                return True, item
                        return False, None

                    if _is_statement_payload(entry_val):
                        return True, entry_val

                    return False, None

                # Shape A: explicit child nodes in `children`
                children = wrapper_val.get("children")
                if isinstance(children, list):
                    for c in children:
                        if isinstance(c, dict) and isinstance(c.get("type"), str):
                            out.append((_norm_kw(c.get("type")), c))

                # Shape B: child statements under `attributes.<structural-key>`
                attrs_obj = wrapper_val.get("attributes")
                if isinstance(attrs_obj, dict):
                    for ak, av in attrs_obj.items():
                        ak_norm = _norm_kw(ak)
                        is_structural_child, payload = _classify_wrapper_entry(ak_norm, av)
                        if is_structural_child and isinstance(payload, dict):
                            out.append((ak_norm, payload))
                return out

            def _infer_child_structural_type(wrapper_key: str):
                wanted = _norm_kw(wrapper_key)
                for attrs in (old_attrs, new_attrs):
                    if not isinstance(attrs, dict):
                        continue

                    wrapper_val = attrs.get(wrapper_key)
                    if not isinstance(wrapper_val, dict):
                        wrapper_val = attrs.get(wanted)
                    if not isinstance(wrapper_val, dict):
                        for k, v in attrs.items():
                            if _norm_kw(str(k)) == wanted and isinstance(v, dict):
                                wrapper_val = v
                                break
                    if not isinstance(wrapper_val, dict):
                        continue
                    child_types = {
                        ctype
                        for ctype, _node in _extract_wrapper_child_nodes(wrapper_val)
                        if ctype not in non_structural_keys
                    }
                    if len(child_types) == 1:
                        return next(iter(child_types))
                return None

            inferred_from_wrappers = set()
            for cp in candidate_paths:
                inferred = _infer_child_structural_type(cp)
                if inferred:
                    inferred_from_wrappers.add(inferred)

            inferred_structurals |= inferred_from_wrappers

            target_kw = None
            # Prefer inferred inner structural type over outer wrapper path.
            if len(inferred_structurals) == 1:
                target_kw = next(iter(inferred_structurals))
            elif len(candidate_paths) == 1:
                single_candidate = _norm_kw(next(iter(candidate_paths)))
                if single_candidate in structural_set:
                    target_kw = single_candidate
            elif len(candidate_paths) > 1:
                # Mixed candidate paths can appear when wrapper attributes and
                # child structural arguments both change (e.g. dynamic-default
                # identifier plus default-value items). Prefer the unique path
                # that is a declared structural keyword.
                structural_candidates = [_norm_kw(p) for p in candidate_paths if _norm_kw(p) in structural_set]
                if len(structural_candidates) == 1:
                    target_kw = structural_candidates[0]

                if target_kw is None:
                    # Wrapper-aware fallback: if one candidate key is a dict wrapper
                    # on the leaf attributes and another is a scalar child token,
                    # prefer the scalar child token.
                    wrappers = set()
                    for p in candidate_paths:
                        for attrs in (old_attrs, new_attrs):
                            if isinstance(attrs, dict) and isinstance(attrs.get(p), dict):
                                wrappers.add(p)
                                break
                    non_wrappers = [_norm_kw(p) for p in candidate_paths if p not in wrappers]
                    if len(set(non_wrappers)) == 1:
                        chosen = non_wrappers[0]
                        # Only promote scalar child tokens when they are declared
                        # structural keywords (e.g., default-value). Unknown
                        # uncovered scalar entries stay attributes.
                        if chosen in structural_set:
                            target_kw = chosen
                        elif len(wrappers) == 1:
                            target_kw = _norm_kw(next(iter(wrappers)))

                    # If multiple non-wrapper scalar changes exist under one
                    # unique wrapper extension block (e.g., actionpoint name +
                    # external/internal flag deltas), anchor reporting on the
                    # wrapper keyword itself.
                    if target_kw is None and len(wrappers) == 1:
                        target_kw = _norm_kw(next(iter(wrappers)))

            # Generic fallback: if changed scalar fields are owned by exactly one
            # wrapper block, anchor reporting on that wrapper keyword.
            if target_kw is None and candidate_paths:
                target_kw = find_single_wrapper_owner(old_attrs, new_attrs, candidate_paths, _norm_kw)

            if target_kw:

                def _collect_target_child_names() -> set:
                    names = set()
                    for attrs in (old_attrs, new_attrs):
                        if not isinstance(attrs, dict):
                            continue
                        for _k, _v in attrs.items():
                            if not isinstance(_v, dict):
                                continue
                            for ctype, ch in _extract_wrapper_child_nodes(_v):
                                if ctype != target_kw:
                                    continue
                                ch_attrs = ch.get("attributes") if isinstance(ch.get("attributes"), dict) else {}
                                ch_name = ch_attrs.get("name")
                                if ch_name is not None:
                                    names.add(str(ch_name))
                    return names

                target_child_names = _collect_target_child_names()

                def _attrs_get_by_norm(attrs: Dict, wanted_kw: str):
                    if not isinstance(attrs, dict):
                        return None
                    if wanted_kw in attrs:
                        return attrs.get(wanted_kw)
                    for k, v in attrs.items():
                        if _norm_kw(k) == wanted_kw:
                            return v
                    return None

                # Determine wrapper keyword and wrapper name change directly from
                # old/new attribute trees to avoid path-heuristic ambiguity.
                wrapper_kw = None
                wrapper_old_name = None
                wrapper_new_name = None
                wrapper_old_meta = None
                wrapper_new_meta = None
                wrapper_candidates = []
                for k in set(list(old_attrs.keys()) + list(new_attrs.keys())):
                    k_norm = _norm_kw(k)
                    old_v = _attrs_get_by_norm(old_attrs, k_norm)
                    new_v = _attrs_get_by_norm(new_attrs, k_norm)
                    if not isinstance(old_v, dict) and not isinstance(new_v, dict):
                        continue
                    old_children = old_v.get("children") if isinstance(old_v, dict) else None
                    new_children = new_v.get("children") if isinstance(new_v, dict) else None
                    old_has = isinstance(old_v, dict) and any(
                        ctype == target_kw for ctype, _ in _extract_wrapper_child_nodes(old_v)
                    )
                    new_has = isinstance(new_v, dict) and any(
                        ctype == target_kw for ctype, _ in _extract_wrapper_child_nodes(new_v)
                    )
                    if old_has or new_has:
                        old_a = old_v.get("attributes") if isinstance(old_v, dict) and isinstance(old_v.get("attributes"), dict) else {}
                        new_a = new_v.get("attributes") if isinstance(new_v, dict) and isinstance(new_v.get("attributes"), dict) else {}
                        old_m = old_v.get("metadata") if isinstance(old_v, dict) and isinstance(old_v.get("metadata"), dict) else None
                        new_m = new_v.get("metadata") if isinstance(new_v, dict) and isinstance(new_v.get("metadata"), dict) else None
                        wrapper_candidates.append((k_norm, old_a.get("name"), new_a.get("name"), old_m, new_m))

                if len(wrapper_candidates) == 1:
                    wrapper_kw, wrapper_old_name, wrapper_new_name, wrapper_old_meta, wrapper_new_meta = wrapper_candidates[0]
                    # If heuristic selected a scalar child key that is not an
                    # explicit structural keyword (e.g., oid), anchor promotion
                    # on the owning wrapper keyword instead (e.g., alias).
                    if target_kw and target_kw not in structural_set:
                        target_kw = wrapper_kw

                # If there is a single wrapper key that owns children of target_kw,
                # preserve it in emitted structural paths (e.g., dynamic-default).
                promoted_parent_path = old_node.get("path", "")
                # If we are already comparing a node of the promoted type
                # (e.g., default-value old vs new), build paths from its parent
                # to avoid duplicating '/<type>/' segments.
                if str(old_node.get("type", "")) == str(target_kw):
                    marker = f"/{target_kw}/"
                    if marker in str(promoted_parent_path):
                        promoted_parent_path = str(promoted_parent_path).rsplit(marker, 1)[0]
                if wrapper_kw:
                    promoted_parent_path = f"{promoted_parent_path}/{wrapper_kw}"
                    if isinstance(wrapper_old_meta, dict) or isinstance(wrapper_new_meta, dict):
                        promoted_old_value = {
                            "attributes": {"name": wrapper_old_name},
                            "metadata": wrapper_old_meta or {},
                        }
                        promoted_new_value = {
                            "attributes": {"name": wrapper_new_name},
                            "metadata": wrapper_new_meta or {},
                        }

                def _extract_name_value(v):
                    if isinstance(v, dict):
                        attrs = v.get("attributes") if isinstance(v.get("attributes"), dict) else {}
                        if "name" in attrs:
                            return attrs.get("name")
                    return v

                rewritten = []
                wrapper_name_changes = []

                def _is_name_field_path(path_value) -> bool:
                    if path_value == "name":
                        return True
                    if isinstance(path_value, (list, tuple)):
                        if len(path_value) == 1:
                            return str(path_value[0]).strip(" '\"") == "name"
                        return any(str(p).strip(" '\"") == "name" for p in path_value)
                    if isinstance(path_value, str):
                        pv = path_value.strip()
                        if pv in ("name", "['name']", '["name"]'):
                            return True
                        simplified = pv.replace("[", "").replace("]", "").replace("'", "").replace('"', "").strip()
                        return simplified == "name" or simplified.endswith(".name")
                    return False

                for detail in all_change_details:
                    d_path = str(detail.get("path", ""))
                    d_path_norm = _norm_kw(d_path)
                    d_type = str(detail.get("type", ""))
                    # Accept direct structural path matches and nested-children
                    # wrappers carrying a structural payload.
                    is_children_structural = False
                    if d_path == "children":
                        for side in ("old", "new"):
                            val = detail.get(side)
                            if isinstance(val, dict) and _norm_kw(val.get("type")) == target_kw:
                                is_children_structural = True
                                break

                    # Wrapper key changes (e.g., dynamic-default identifier) are
                    # not child structural-instance renames and should not be
                    # promoted as target_kw/name changes.
                    is_wrapper_of_target = False
                    if d_path and d_path != "children":
                        inferred_wrapper_type = _infer_child_structural_type(d_path)
                        if inferred_wrapper_type and inferred_wrapper_type == target_kw:
                            is_wrapper_of_target = True

                    is_unknown_structural_scalar = (
                        d_type == "attribute_changed"
                        and d_path_norm not in non_structural_keys
                        and d_path_norm != target_kw
                        and not is_children_structural
                        and not is_wrapper_of_target
                        and not isinstance(detail.get("old"), (dict, list))
                        and not isinstance(detail.get("new"), (dict, list))
                    )

                    # Wrapper identifier changes belong to the wrapper structural
                    # keyword (e.g., dynamic-default), not child target_kw items.
                    if d_type == "attribute_changed" and is_wrapper_of_target:
                        old_name = _extract_name_value(detail.get("old"))
                        new_name = _extract_name_value(detail.get("new"))
                        if old_name is not None or new_name is not None:
                            wrapper_name_changes.append({
                                "wrapper_kw": d_path_norm,
                                "old": old_name,
                                "new": new_name,
                                "old_metadata": wrapper_old_meta,
                                "new_metadata": wrapper_new_meta,
                            })
                        continue

                    if (
                        d_path_norm != target_kw
                        and not is_children_structural
                        and not is_unknown_structural_scalar
                    ) or not d_type.startswith("attribute_"):
                        rewritten.append(detail)
                        continue

                    # Even when the path already equals target_kw, guard against
                    # wrapper identifier changes that were aliased to target_kw by
                    # path extraction. Keep such changes as passthrough.
                    if (
                        d_type == "attribute_changed"
                        and d_path_norm == target_kw
                        and not isinstance(detail.get("old"), (dict, list))
                        and not isinstance(detail.get("new"), (dict, list))
                        and target_child_names
                    ):
                        old_name = str(detail.get("old")) if detail.get("old") is not None else ""
                        new_name = str(detail.get("new")) if detail.get("new") is not None else ""
                        if old_name not in target_child_names and new_name not in target_child_names:
                            if wrapper_kw and d_path_norm == wrapper_kw:
                                wrapper_name_changes.append({
                                    "wrapper_kw": wrapper_kw,
                                    "old": _extract_name_value(detail.get("old")),
                                    "new": _extract_name_value(detail.get("new")),
                                    "old_metadata": wrapper_old_meta,
                                    "new_metadata": wrapper_new_meta,
                                })
                            else:
                                rewritten.append(detail)
                            continue

                    # Unknown scalar promotions must correspond to real child
                    # structural instance names; otherwise keep them as wrapper
                    # attribute changes (e.g., dynamic-default identifier).
                    if is_unknown_structural_scalar and target_child_names:
                        old_s = detail.get("old")
                        new_s = detail.get("new")
                        old_name = str(old_s) if old_s is not None else ""
                        new_name = str(new_s) if new_s is not None else ""
                        if old_name not in target_child_names and new_name not in target_child_names:
                            rewritten.append(detail)
                            continue

                    if d_type == "attribute_changed":
                        old_val = detail.get("old")
                        new_val = detail.get("new")
                        if is_children_structural or d_path_norm == target_kw:
                            if isinstance(old_val, dict):
                                old_val = _extract_name_value(old_val)
                            if isinstance(new_val, dict):
                                new_val = _extract_name_value(new_val)
                            rewritten.append({
                                "type": "attribute_changed",
                                "path": "name",
                                "old": _extract_name_value(old_val),
                                "new": _extract_name_value(new_val),
                            })
                        else:
                            rewritten.append(detail)
                    elif d_type == "attribute_added":
                        new_val = detail.get("new")
                        if (is_children_structural or d_path_norm == target_kw) and isinstance(new_val, dict):
                            new_val = _extract_name_value(new_val)
                        if is_children_structural or d_path_norm == target_kw:
                            rewritten.append({
                                "type": "attribute_added",
                                "path": "name",
                                "new": _extract_name_value(new_val),
                                "_raw_new": detail.get("new"),
                            })
                        else:
                            rewritten.append(detail)
                    elif d_type in ("attribute_deleted", "attribute_removed"):
                        old_val = detail.get("old")
                        if (is_children_structural or d_path_norm == target_kw) and isinstance(old_val, dict):
                            old_val = _extract_name_value(old_val)
                        if is_children_structural or d_path_norm == target_kw:
                            rewritten.append({
                                "type": "attribute_deleted",
                                "path": "name",
                                "old": _extract_name_value(old_val),
                                "_raw_old": detail.get("old"),
                            })
                        else:
                            rewritten.append(detail)
                    else:
                        rewritten.append(detail)

                promoted_node_type = target_kw
                collapse_to_wrapper_path = bool(wrapper_kw and promoted_node_type == wrapper_kw)
                # Split structural argument deltas into native structural records:
                # - changed name -> CHANGED detail
                # - added/deleted name -> separate ADDED/DELETED node records
                changed_details = []
                passthrough_details = []
                added_details = []
                deleted_details = []

                for d in rewritten:
                    t = str(d.get("type", ""))
                    if t == "attribute_changed" and _is_name_field_path(d.get("path")):
                        changed_details.append(d)
                    elif t == "attribute_added" and _is_name_field_path(d.get("path")):
                        added_details.append(d)
                    elif t in ("attribute_deleted", "attribute_removed") and _is_name_field_path(d.get("path")):
                        deleted_details.append(d)
                    else:
                        passthrough_details.append(d)

                # Move wrapper-name scalar changes out of child structural groups.
                if wrapper_kw:
                    kept_passthrough = []
                    for d in passthrough_details:
                        if (
                            str(d.get("type", "")) == "attribute_changed"
                            and _norm_kw(d.get("path")) == wrapper_kw
                            and not isinstance(d.get("old"), (dict, list))
                            and not isinstance(d.get("new"), (dict, list))
                        ):
                            wrapper_name_changes.append({
                                "wrapper_kw": wrapper_kw,
                                "old": _extract_name_value(d.get("old")),
                                "new": _extract_name_value(d.get("new")),
                                "old_metadata": wrapper_old_meta,
                                "new_metadata": wrapper_new_meta,
                            })
                            continue
                        kept_passthrough.append(d)
                    passthrough_details = kept_passthrough

                # Add wrapper identifier change from direct wrapper AST (if any).
                if wrapper_kw and wrapper_old_name != wrapper_new_name:
                    wrapper_name_changes.append({
                        "wrapper_kw": wrapper_kw,
                        "old": wrapper_old_name,
                        "new": wrapper_new_name,
                        "old_metadata": wrapper_old_meta,
                        "new_metadata": wrapper_new_meta,
                    })

                # Wrapper rename can be discovered by multiple heuristics; keep
                # one stable record per (wrapper keyword, old name, new name).
                if wrapper_name_changes:
                    deduped_wrapper_changes = []
                    seen_wrapper_changes = set()
                    for wc in wrapper_name_changes:
                        key = (
                            str(wc.get("wrapper_kw") or wrapper_kw or "wrapper"),
                            str(wc.get("old")),
                            str(wc.get("new")),
                        )
                        if key in seen_wrapper_changes:
                            continue
                        seen_wrapper_changes.add(key)
                        deduped_wrapper_changes.append(wc)
                    wrapper_name_changes = deduped_wrapper_changes

                # When promotion is anchored on the wrapper keyword itself
                # (e.g. alias), the wrapper name delta is already represented in
                # changed_details and should not emit a second standalone record.
                if collapse_to_wrapper_path:
                    wrapper_name_changes = []

                # If there are structural add/delete entries, emit them as separate
                # structural records to mirror leaf/list style reporting.
                if added_details or deleted_details:
                    if changed_details or passthrough_details:
                        def _collect_children_map(attrs: Dict, wanted_kw: str, preferred_wrapper_kw: str = None) -> Dict[str, Dict]:
                            cmap = {}
                            if not isinstance(attrs, dict):
                                return cmap

                            wrappers = []
                            if preferred_wrapper_kw:
                                pv = _attrs_get_by_norm(attrs, preferred_wrapper_kw)
                                if isinstance(pv, dict):
                                    wrappers.append(pv)
                            if not wrappers:
                                wrappers = [v for v in attrs.values() if isinstance(v, dict)]

                            for wv in wrappers:
                                for ctype, ch in _extract_wrapper_child_nodes(wv):
                                    if ctype != wanted_kw or not isinstance(ch, dict):
                                        continue
                                    ch_attrs = ch.get("attributes") if isinstance(ch.get("attributes"), dict) else {}
                                    ch_name = ch_attrs.get("name")
                                    if ch_name is not None:
                                        cmap[str(ch_name)] = ch
                            return cmap

                        grouped_records = []
                        rebuilt_records = []
                        can_rebuild = bool(changed_details)

                        if can_rebuild:
                            old_children_map = _collect_children_map(old_attrs, promoted_node_type, wrapper_kw)
                            new_children_map = _collect_children_map(new_attrs, promoted_node_type, wrapper_kw)

                            for cd in changed_details:
                                old_name = cd.get("old")
                                new_name = cd.get("new")
                                group = [{
                                    "type": "attribute_changed",
                                    "path": "name",
                                    "old": old_name,
                                    "new": new_name,
                                }]

                                old_child = old_children_map.get(str(old_name)) if old_name is not None else None
                                new_child = new_children_map.get(str(new_name)) if new_name is not None else None
                                if not isinstance(old_child, dict) or not isinstance(new_child, dict):
                                    can_rebuild = False
                                    break

                                old_child_attrs = old_child.get("attributes") if isinstance(old_child.get("attributes"), dict) else {}
                                new_child_attrs = new_child.get("attributes") if isinstance(new_child.get("attributes"), dict) else {}
                                old_child_attrs = {k: v for k, v in old_child_attrs.items() if k not in ("name", "metadata")}
                                new_child_attrs = {k: v for k, v in new_child_attrs.items() if k not in ("name", "metadata")}

                                child_attr_changes = AttributeComparisonStrategy().compare(old_child_attrs, new_child_attrs)
                                for c in child_attr_changes:
                                    c_kind = c.node_type or "attribute"
                                    group.append({
                                        "type": f"{c_kind}_{c.change_type.value}",
                                        "path": c.path,
                                        "old": c.old_value,
                                        "new": c.new_value,
                                    })

                                old_cons = old_child.get("constraints") if isinstance(old_child.get("constraints"), dict) else {}
                                new_cons = new_child.get("constraints") if isinstance(new_child.get("constraints"), dict) else {}
                                child_cons_changes = AttributeComparisonStrategy().compare(old_cons, new_cons)
                                for c in child_cons_changes:
                                    c_kind = c.node_type or "constraint"
                                    group.append({
                                        "type": f"{c_kind}_{c.change_type.value}",
                                        "path": c.path,
                                        "old": c.old_value,
                                        "new": c.new_value,
                                    })

                                rebuilt_records.append({
                                    "details": group,
                                    "old_value": old_child,
                                    "new_value": new_child,
                                })

                        if can_rebuild and rebuilt_records:
                            grouped_records = rebuilt_records
                        else:
                            # Fallback: group by ordered name-change anchors.
                            changed_stream = []
                            for d in rewritten:
                                t = str(d.get("type", ""))
                                p = d.get("path")
                                is_added_name = (t == "attribute_added" and p == "name")
                                is_deleted_name = (t in ("attribute_deleted", "attribute_removed") and p == "name")
                                if is_added_name or is_deleted_name:
                                    continue
                                changed_stream.append(d)

                            current_group = []
                            preamble = []
                            seen_name = False
                            for d in changed_stream:
                                is_name_changed = (
                                    str(d.get("type", "")) == "attribute_changed"
                                    and _is_name_field_path(d.get("path"))
                                )
                                if is_name_changed:
                                    if seen_name and current_group:
                                        grouped_records.append({
                                            "details": current_group,
                                            "old_value": old_node,
                                            "new_value": new_node,
                                        })
                                        current_group = []
                                    if not seen_name and preamble:
                                        current_group.extend(preamble)
                                        preamble = []
                                    seen_name = True
                                    current_group.append(d)
                                else:
                                    if seen_name:
                                        current_group.append(d)
                                    else:
                                        preamble.append(d)

                            if current_group:
                                grouped_records.append({
                                    "details": current_group,
                                    "old_value": old_node,
                                    "new_value": new_node,
                                })
                            if not grouped_records and changed_stream:
                                grouped_records = [{
                                    "details": changed_stream,
                                    "old_value": old_node,
                                    "new_value": new_node,
                                }]

                        for rec in grouped_records:
                            group = rec.get("details") or []
                            group_path = promoted_parent_path
                            if not collapse_to_wrapper_path:
                                for gd in group:
                                    if (
                                        isinstance(gd, dict)
                                        and str(gd.get("type", "")) == "attribute_changed"
                                        and _is_name_field_path(gd.get("path"))
                                        and (gd.get("old") is not None or gd.get("new") is not None)
                                    ):
                                        path_name = gd.get("old") if gd.get("old") is not None else gd.get("new")
                                        group_path = f"{promoted_parent_path}/{promoted_node_type}/{path_name}"
                                        break
                            changes.append(ChangeRecord(
                                change_type=ChangeType.CHANGED,
                                path=group_path,
                                node_type=promoted_node_type,
                                old_value=rec.get("old_value", old_node),
                                new_value=rec.get("new_value", new_node),
                                details=group,
                            ))

                    for d in added_details:
                        raw_new = d.get("_raw_new")
                        if isinstance(raw_new, dict):
                            new_payload = raw_new
                        else:
                            new_name = d.get("new")
                            new_payload = {"attributes": {"name": new_name}}
                        changes.append(ChangeRecord(
                            change_type=ChangeType.ADDED,
                            path=promoted_parent_path,
                            node_type=promoted_node_type,
                            new_value=new_payload,
                        ))

                    for d in deleted_details:
                        raw_old = d.get("_raw_old")
                        if isinstance(raw_old, dict):
                            old_payload = raw_old
                        else:
                            old_payload = {"attributes": {"name": d.get("old")}}
                        changes.append(ChangeRecord(
                            change_type=ChangeType.DELETED,
                            path=promoted_parent_path,
                            node_type=promoted_node_type,
                            old_value=old_payload,
                        ))

                    for wc in wrapper_name_changes:
                        wrapper_kw_name = wc.get("wrapper_kw") or wrapper_kw or "wrapper"
                        wrapper_old = wc.get("old")
                        wrapper_new = wc.get("new")
                        wrapper_path = f"{old_node.get('path', '')}/{wrapper_kw_name}"
                        if wrapper_old is not None:
                            wrapper_path = f"{wrapper_path}/{wrapper_old}"
                        old_payload = old_node
                        new_payload = new_node
                        if isinstance(wc.get("old_metadata"), dict) or isinstance(wc.get("new_metadata"), dict):
                            old_payload = {
                                "attributes": {"name": wrapper_old},
                                "metadata": wc.get("old_metadata") or {},
                            }
                            new_payload = {
                                "attributes": {"name": wrapper_new},
                                "metadata": wc.get("new_metadata") or {},
                            }
                        changes.append(ChangeRecord(
                            change_type=ChangeType.CHANGED,
                            path=wrapper_path,
                            node_type=wrapper_kw_name,
                            old_value=old_payload,
                            new_value=new_payload,
                            details=[{
                                "type": "attribute_changed",
                                "path": "name",
                                "old": wrapper_old,
                                "new": wrapper_new,
                            }],
                        ))

                    return changes

                # If multiple structural 'name changed' entries exist, emit one
                # CHANGED record per structural instance instead of collapsing all
                # under a single path section.
                if len(changed_details) > 1:
                    grouped_details = []
                    current_group = []
                    preamble = []
                    seen_name = False

                    for d in rewritten:
                        is_name_changed = (
                            str(d.get("type", "")) == "attribute_changed"
                            and _is_name_field_path(d.get("path"))
                        )
                        if is_name_changed:
                            if seen_name and current_group:
                                grouped_details.append(current_group)
                                current_group = []
                            if not seen_name and preamble:
                                current_group.extend(preamble)
                                preamble = []
                            seen_name = True
                            current_group.append(d)
                        else:
                            if seen_name:
                                current_group.append(d)
                            else:
                                preamble.append(d)

                    if current_group:
                        grouped_details.append(current_group)

                    if not grouped_details and rewritten:
                        grouped_details = [rewritten]

                    for group in grouped_details:
                        group_path = promoted_parent_path
                        if not collapse_to_wrapper_path:
                            for gd in group:
                                if (
                                    isinstance(gd, dict)
                                    and str(gd.get("type", "")) == "attribute_changed"
                                    and _is_name_field_path(gd.get("path"))
                                    and (gd.get("old") is not None or gd.get("new") is not None)
                                ):
                                    path_name = gd.get("old") if gd.get("old") is not None else gd.get("new")
                                    group_path = f"{promoted_parent_path}/{promoted_node_type}/{path_name}"
                                    break
                        changes.append(ChangeRecord(
                            change_type=ChangeType.CHANGED,
                            path=group_path,
                            node_type=promoted_node_type,
                            old_value=old_node,
                            new_value=new_node,
                            details=group,
                        ))

                    for wc in wrapper_name_changes:
                        wrapper_kw_name = wc.get("wrapper_kw") or wrapper_kw or "wrapper"
                        wrapper_old = wc.get("old")
                        wrapper_new = wc.get("new")
                        wrapper_path = f"{old_node.get('path', '')}/{wrapper_kw_name}"
                        if wrapper_old is not None:
                            wrapper_path = f"{wrapper_path}/{wrapper_old}"
                        old_payload = old_node
                        new_payload = new_node
                        if isinstance(wc.get("old_metadata"), dict) or isinstance(wc.get("new_metadata"), dict):
                            old_payload = {
                                "attributes": {"name": wrapper_old},
                                "metadata": wc.get("old_metadata") or {},
                            }
                            new_payload = {
                                "attributes": {"name": wrapper_new},
                                "metadata": wc.get("new_metadata") or {},
                            }
                        changes.append(ChangeRecord(
                            change_type=ChangeType.CHANGED,
                            path=wrapper_path,
                            node_type=wrapper_kw_name,
                            old_value=old_payload,
                            new_value=new_payload,
                            details=[{
                                "type": "attribute_changed",
                                "path": "name",
                                "old": wrapper_old,
                                "new": wrapper_new,
                            }],
                        ))
                    return changes

                for wc in wrapper_name_changes:
                    wrapper_kw_name = wc.get("wrapper_kw") or wrapper_kw or "wrapper"
                    wrapper_old = wc.get("old")
                    wrapper_new = wc.get("new")
                    wrapper_path = f"{old_node.get('path', '')}/{wrapper_kw_name}"
                    if wrapper_old is not None:
                        wrapper_path = f"{wrapper_path}/{wrapper_old}"
                    old_payload = old_node
                    new_payload = new_node
                    if isinstance(wc.get("old_metadata"), dict) or isinstance(wc.get("new_metadata"), dict):
                        old_payload = {
                            "attributes": {"name": wrapper_old},
                            "metadata": wc.get("old_metadata") or {},
                        }
                        new_payload = {
                            "attributes": {"name": wrapper_new},
                            "metadata": wc.get("new_metadata") or {},
                        }
                    changes.append(ChangeRecord(
                        change_type=ChangeType.CHANGED,
                        path=wrapper_path,
                        node_type=wrapper_kw_name,
                        old_value=old_payload,
                        new_value=new_payload,
                        details=[{
                            "type": "attribute_changed",
                            "path": "name",
                            "old": wrapper_old,
                            "new": wrapper_new,
                        }],
                    ))

                promoted_details = rewritten

            # Fallback metadata refinement for wrapper-driven diffs that remain on
            # the parent node (no promoted target_kw): if exactly one wrapper dict
            # owns the changed scalar keys, use wrapper metadata for report line info.
            if promoted_old_value is old_node and promoted_new_value is new_node:
                wrapper_meta_candidates = []
                all_attrs = set()
                if isinstance(old_attrs, dict):
                    all_attrs.update(old_attrs.keys())
                if isinstance(new_attrs, dict):
                    all_attrs.update(new_attrs.keys())

                for wk in all_attrs:
                    wk_norm = _norm_kw(wk)
                    old_w = None
                    new_w = None
                    if isinstance(old_attrs, dict):
                        if wk_norm in old_attrs:
                            old_w = old_attrs.get(wk_norm)
                        else:
                            for ok, ov in old_attrs.items():
                                if _norm_kw(ok) == wk_norm:
                                    old_w = ov
                                    break
                    if isinstance(new_attrs, dict):
                        if wk_norm in new_attrs:
                            new_w = new_attrs.get(wk_norm)
                        else:
                            for nk, nv in new_attrs.items():
                                if _norm_kw(nk) == wk_norm:
                                    new_w = nv
                                    break
                    if not isinstance(old_w, dict) and not isinstance(new_w, dict):
                        continue
                    old_meta = old_w.get("metadata") if isinstance(old_w, dict) and isinstance(old_w.get("metadata"), dict) else None
                    new_meta = new_w.get("metadata") if isinstance(new_w, dict) and isinstance(new_w.get("metadata"), dict) else None
                    old_child_keys = set(old_w.get("attributes", {}).keys()) if isinstance(old_w, dict) and isinstance(old_w.get("attributes"), dict) else set()
                    new_child_keys = set(new_w.get("attributes", {}).keys()) if isinstance(new_w, dict) and isinstance(new_w.get("attributes"), dict) else set()
                    child_keys = old_child_keys | new_child_keys
                    if child_keys & candidate_paths:
                        wrapper_meta_candidates.append((old_meta, new_meta))

                if len(wrapper_meta_candidates) == 1:
                    old_meta, new_meta = wrapper_meta_candidates[0]
                    promoted_old_value = {
                        "attributes": {"name": old_node.get("attributes", {}).get("name") if isinstance(old_node.get("attributes"), dict) else None},
                        "metadata": old_meta or {},
                    }
                    promoted_new_value = {
                        "attributes": {"name": new_node.get("attributes", {}).get("name") if isinstance(new_node.get("attributes"), dict) else None},
                        "metadata": new_meta or {},
                    }

            if collapse_to_wrapper_path:
                has_name_detail = any(
                    isinstance(d, dict)
                    and str(d.get("type", "")) == "attribute_changed"
                    and str(d.get("path", "")) == "name"
                    for d in (promoted_details or [])
                )
                if not has_name_detail and wrapper_old_name != wrapper_new_name:
                    promoted_details = [{
                        "type": "attribute_changed",
                        "path": "name",
                        "old": wrapper_old_name,
                        "new": wrapper_new_name,
                    }] + list(promoted_details or [])

            output_path = promoted_parent_path if collapse_to_wrapper_path else old_node.get("path", "")

            changes.append(ChangeRecord(
                change_type=ChangeType.CHANGED,
                path=output_path,
                node_type=promoted_node_type,
                old_value=promoted_old_value,
                new_value=promoted_new_value,
                details=promoted_details
            ))
        return changes

    def _compare_symbolic_entries(self, old_symbolic: List, new_symbolic: List,
                                  symbolic_key: str, old_node: Dict, new_node: Dict) -> List[ChangeRecord]:
        """
        Compare symbolic entries (enum, bit, etc.) using the dedicated symbolic comparator.

        Returns a list of ChangeRecords, one per symbolic entry with changes.

        Args:
            old_symbolic: List of old symbolic entries
            new_symbolic: List of new symbolic entries
            symbolic_key: The symbolic type key ('enum', 'bit', etc.)
            old_node: The old node dictionary
            new_node: The new node dictionary

        Returns:
            List of ChangeRecords for symbolic entries
        """
        return self.symbolic_comparator.compare_symbolic_entries(
            old_symbolic=old_symbolic,
            new_symbolic=new_symbolic,
            symbolic_key=symbolic_key,
            parent_path=old_node.get("path", ""),
            parent_node_type=old_node.get("type", "node")
        )

    def _compare_union_types(self, old_union_types: List, new_union_types: List,
                            old_node: Dict, new_node: Dict) -> Optional[ChangeRecord]:
        """
        Compare union member types.

        Args:
            old_union_types: List of old union member types
            new_union_types: List of new union member types
            old_node: The old node dictionary
            new_node: The new node dictionary

        Returns:
            ChangeRecord if there are meaningful changes, None otherwise
        """
        list_strategy = ListComparisonStrategy()
        union_type_changes = list_strategy.compare(old_union_types, new_union_types)

        # Group union type changes for better reporting
        union_type_change_details = []
        for change in union_type_changes:
            if change.is_meaningful():
                if change.change_type in [ChangeType.ADDED, ChangeType.DELETED]:
                    union_type_change_details.append({
                        "type": f"union_type_{change.change_type.value}",
                        "path": change.path,
                        "old": change.old_value,
                        "new": change.new_value,
                        "details": change.details
                    })
                else:
                    # Union member changed
                    union_type_change_details.append({
                        "type": "union_type_changed",
                        "path": change.path,
                        "old": change.old_value,
                        "new": change.new_value,
                        "details": change.details
                    })

        if union_type_change_details:
            return ChangeRecord(
                change_type=ChangeType.CHANGED,
                path=old_node.get("path", ""),
                node_type=old_node.get("type", "node"),
                old_value=old_node,
                new_value=new_node,
                details=union_type_change_details
            )
        return None
    
    def _filter_special_type_attrs(self, attrs: Dict) -> Dict:
        """
        Filter out type attributes to avoid double processing.
        
        Type changes are reported as separate structural elements (e.g., path/typename),
        so they should NOT be included in the parent node's attribute changes to avoid duplication.
        
        For example, when a leaf's type changes from uint8 to uint32:
        - WRONG: Report under leaf: "4.2 attribute changed: ['name'] -> uint32 (was uint8)"
        - RIGHT: Report separately: "5. (type changed) ... 5.1 attribute changed: ['name'] -> uint32"
        """
        from .constants import SYMBOLIC_KEYWORDS
        
        filtered = dict(attrs)
        
        # Remove 'type' attribute entirely to avoid duplication with separate type reporting
        # Type changes will be reported as their own ChangeRecord with a separate path
        if "type" in filtered:
            del filtered["type"]
        
        return filtered
