#!/usr/bin/env python3
"""
Typedef handling for YANG comparison.

This module manages typedef definitions and comparison logic.
Extracts enum/bit definitions from typedefs to enable detailed
comparison of typedef references.
"""

from typing import Dict, List, Any, Optional
from ..helper.pyang_utils import PyangStatementHelper
from .node_normalizer import NodeNormalizer
from .comparison_strategies import ListComparisonStrategy
from .symbolic_comparator import SymbolicComparator
from .data_classes import ChangeRecord
from .constants import ChangeType


class TypedefHandler:
    """
    Handles typedef definitions and comparisons.
    
    Manages a cache of typedef definitions from modules and their imports,
    and provides logic for comparing typedef references to detect changes
    in the typedef definitions themselves.
    """
    
    def __init__(self):
        """Initialize typedef handler with empty caches for old and new versions."""
        self.typedef_cache: Dict[str, Dict[str, Any]] = {}  # Current/active cache
        self.old_typedef_cache: Dict[str, Dict[str, Any]] = {}  # Cache for old version
        self.new_typedef_cache: Dict[str, Dict[str, Any]] = {}  # Cache for new version
        self.helper = PyangStatementHelper()
        self.symbolic_comparator = SymbolicComparator()
        self._loading_version: Optional[str] = None  # Track which version we're loading
    
    def set_loading_version(self, version: str):
        """Set which version (old/new) is being loaded to cache typedefs separately."""
        self._loading_version = version
        if version == "old":
            self.typedef_cache = self.old_typedef_cache
        elif version == "new":
            self.typedef_cache = self.new_typedef_cache
        else:
            raise ValueError(f"Invalid version: {version}. Must be 'old' or 'new'")
    
    def load_typedef_definitions(self, module_stmt, pyang_ctx, modules_dir: str):
        """
        Load typedef definitions from module and its imports.
        
        This extracts enum/bit definitions from typedefs so we can expand
        them when comparing typedef references.
        
        Args:
            module_stmt: The main module statement
            pyang_ctx: The PyangContext wrapper (has ctx attribute for pyang Context)
            modules_dir: Directory containing modules (for searching imports)
        """
        if not module_stmt:
            print("[TypedefLoader] WARNING: module_stmt is None")
            return
        
        print(f"[TypedefLoader] Extracting typedef definitions from module and imports...")
        
        # Get the module name
        module_name = self.helper.get_argument(module_stmt)
        print(f"[TypedefLoader] Processing main module: {module_name}")
        
        # Extract typedefs from the main module
        self._extract_typedefs_from_stmt(module_stmt, module_name)
        
        # Get the actual pyang context
        ctx = pyang_ctx.ctx if hasattr(pyang_ctx, 'ctx') else pyang_ctx
        
        # Process imported modules and included submodules
        processed_modules = set()  # Track processed modules to avoid duplicates
        modules_to_process = []
        
        # Add imports from main module
        import_stmts = self.helper.search_substmts(module_stmt, 'import')
        print(f"[TypedefLoader] Found {len(import_stmts)} import statements in main module")
        for import_stmt in import_stmts:
            import_name = self.helper.get_argument(import_stmt)
            if import_name not in processed_modules:
                modules_to_process.append(import_name)
                processed_modules.add(import_name)
        
        # Add includes (submodules) from main module
        include_stmts = self.helper.search_substmts(module_stmt, 'include')
        print(f"[TypedefLoader] Found {len(include_stmts)} include statements in main module")
        for include_stmt in include_stmts:
            include_name = self.helper.get_argument(include_stmt)
            if include_name not in processed_modules:
                modules_to_process.append(include_name)
                processed_modules.add(include_name)
        
        # Process all modules/submodules recursively
        while modules_to_process:
            module_to_load = modules_to_process.pop(0)
            print(f"[TypedefLoader]   Processing module/submodule: {module_to_load}")
            
            # Look up the module in the pyang context
            loaded_module_stmt = None
            if hasattr(ctx, 'modules'):
                # Search for module by name (ignoring revision)
                for module_key, module_val in ctx.modules.items():
                    # module_key can be a tuple (name, revision) or just a name
                    key_name = module_key[0] if isinstance(module_key, tuple) else module_key
                    if key_name == module_to_load:
                        loaded_module_stmt = module_val
                        print(f"[TypedefLoader]     Found in context: {module_to_load} (key: {module_key})")
                        break
            
            if loaded_module_stmt:
                # Extract typedefs from this module/submodule
                self._extract_typedefs_from_stmt(loaded_module_stmt, module_to_load)
                
                # Add imports from this module/submodule
                sub_imports = self.helper.search_substmts(loaded_module_stmt, 'import')
                print(f"[TypedefLoader]     Found {len(sub_imports)} imports in {module_to_load}")
                for import_stmt in sub_imports:
                    import_name = self.helper.get_argument(import_stmt)
                    if import_name not in processed_modules:
                        print(f"[TypedefLoader]     Adding import: {import_name}")
                        modules_to_process.append(import_name)
                        processed_modules.add(import_name)
                
                # Add includes from this module/submodule (submodules can include other submodules)
                sub_includes = self.helper.search_substmts(loaded_module_stmt, 'include')
                print(f"[TypedefLoader]     Found {len(sub_includes)} includes in {module_to_load}")
                for include_stmt in sub_includes:
                    include_name = self.helper.get_argument(include_stmt)
                    if include_name not in processed_modules:
                        print(f"[TypedefLoader]     Adding include: {include_name}")
                        modules_to_process.append(include_name)
                        processed_modules.add(include_name)
            else:
                print(f"[TypedefLoader]     WARNING: {module_to_load} not found in context.modules")
        
        typedef_count = len(self.typedef_cache)
        print(f"[TypedefLoader] Cached {typedef_count} typedef definitions from {len(processed_modules)} modules")
    
    def _extract_typedefs_from_stmt(self, stmt, module_name: str):
        """
        Recursively extract typedef definitions from a statement.
        
        Args:
            stmt: The pyang statement to process
            module_name: Name of the module being processed
        """
        keyword = self.helper.get_keyword(stmt)
        
        if keyword == 'typedef':
            typedef_name = self.helper.get_argument(stmt)
            if not typedef_name:
                return
            
            print(f"[TypedefLoader]     Found typedef statement: {typedef_name}")
            
            # Create fully qualified name (module:typedef)
            qualified_name = f"{module_name}:{typedef_name}"
            
            # Find type statement within typedef
            type_stmt = self.helper.find_substmt(stmt, 'type')
            if type_stmt:
                print(f"[TypedefLoader]     Parsing type for typedef: {typedef_name}")
                # Parse the type to get enum/bit definitions
                type_data = NodeNormalizer.parse_type_statement(type_stmt)
                
                print(f"[TypedefLoader]     Type data keys: {type_data.keys()}")
                
                # Cache both qualified and unqualified names
                self.typedef_cache[typedef_name] = type_data
                self.typedef_cache[qualified_name] = type_data
                
                # Debug output - check for any symbolic entries
                from .constants import SYMBOLIC_KEYWORDS
                symbolic_counts = {key: len(type_data.get(key, [])) 
                                 for key in SYMBOLIC_KEYWORDS 
                                 if type_data.get(key)}
                if symbolic_counts:
                    for symbolic_key, count in symbolic_counts.items():
                        print(f"[TypedefLoader]   Found typedef {qualified_name} with {count} {symbolic_key}s")
            else:
                print(f"[TypedefLoader]     WARNING: No type statement found in typedef {typedef_name}")
        
        # Recursively process child statements
        for sub_stmt in self.helper.get_substmts(stmt):
            self._extract_typedefs_from_stmt(sub_stmt, module_name)
    
    def compare_typedef_references(self, old_type: Dict, new_type: Dict, 
                                   node_path: str) -> List[ChangeRecord]:
        """
        Compare typedef references and expand to show enum/bit changes.
        
        When a type references a typedef (e.g., oc-bgp-types:community-type),
        look up the typedef definition and compare enum/bit details.
        
        This is called even when the type name hasn't changed, to detect changes
        in the typedef definition itself (e.g., enum added to the typedef).
        
        Args:
            old_type: Old type definition dictionary
            new_type: New type definition dictionary
            node_path: Path to the node being compared
            
        Returns:
            List of ChangeRecord objects for typedef changes
        """
        changes = []
        
        if not isinstance(old_type, dict) or not isinstance(new_type, dict):
            return changes
        
        old_type_name = old_type.get("name")
        new_type_name = new_type.get("name")
        
        # Check if either type name references a typedef (contains ':' for prefix)
        old_is_typedef = isinstance(old_type_name, str) and ':' in old_type_name
        new_is_typedef = isinstance(new_type_name, str) and ':' in new_type_name
        
        # Also check if type name is a simple typedef (no prefix) that exists in cache
        if not old_is_typedef and isinstance(old_type_name, str):
            old_is_typedef = old_type_name in self.typedef_cache
        if not new_is_typedef and isinstance(new_type_name, str):
            new_is_typedef = new_type_name in self.typedef_cache
        
        if not (old_is_typedef or new_is_typedef):
            return changes
        
        # Look up typedef definitions from separate caches
        old_typedef = None
        new_typedef = None
        
        if old_is_typedef:
            # Try with module prefix first, then without - from OLD cache
            old_typedef = self.old_typedef_cache.get(old_type_name)
            if not old_typedef and ':' in str(old_type_name):
                # Try without prefix
                simple_name = old_type_name.split(':')[1]
                old_typedef = self.old_typedef_cache.get(simple_name)
            if old_typedef:
                print(f"[TypedefExpansion] Found old typedef: {old_type_name}")
        
        if new_is_typedef:
            # Try with module prefix first, then without - from NEW cache
            new_typedef = self.new_typedef_cache.get(new_type_name)
            if not new_typedef and ':' in str(new_type_name):
                # Try without prefix
                simple_name = new_type_name.split(':')[1]
                new_typedef = self.new_typedef_cache.get(simple_name)
            if new_typedef:
                print(f"[TypedefExpansion] Found new typedef: {new_type_name}")
        
        # If we found typedef definitions, compare their symbolic entries
        if old_typedef or new_typedef:
            from .constants import SYMBOLIC_KEYWORDS
            
            # Compare all symbolic types (enum, bit, etc.) using unified logic
            for symbolic_key in SYMBOLIC_KEYWORDS:
                old_symbolic = old_typedef.get(symbolic_key, []) if old_typedef else []
                new_symbolic = new_typedef.get(symbolic_key, []) if new_typedef else []
                
                if old_symbolic or new_symbolic:
                    print(f"[TypedefExpansion] Comparing {symbolic_key}s: {len(old_symbolic)} old vs {len(new_symbolic)} new")
                    
                    symbolic_changes = self._compare_symbolic_in_typedef(
                        old_symbolic, new_symbolic, symbolic_key,
                        old_type_name, new_type_name, node_path
                    )
                    if symbolic_changes:
                        # symbolic_changes is now a list of ChangeRecords
                        changes.extend(symbolic_changes)
        
        return changes
    
    def _compare_symbolic_in_typedef(self, old_symbolic: List[Dict], new_symbolic: List[Dict],
                                    symbolic_key: str, old_type_name: str, 
                                    new_type_name: str, node_path: str) -> List[ChangeRecord]:
        """
        Compare symbolic entries (enum, bit, etc.) within typedef definitions.
        
        Args:
            old_symbolic: List of old symbolic entries
            new_symbolic: List of new symbolic entries
            symbolic_key: The symbolic type key ('enum', 'bit', etc.)
            old_type_name: Old typedef name
            new_type_name: New typedef name
            node_path: Path to the node
            
        Returns:
            List of ChangeRecords, one per enum/bit change
        """
        return self.symbolic_comparator.compare_symbolic_in_typedef(
            old_symbolic=old_symbolic,
            new_symbolic=new_symbolic,
            symbolic_key=symbolic_key,
            old_type_name=old_type_name,
            new_type_name=new_type_name,
            node_path=node_path
        )
