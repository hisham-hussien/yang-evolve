#!/usr/bin/env python3
"""
pyang_utils.py - Common pyang utilities for YANG parsing

Shared utilities for working with pyang's AST across multiple tools.
This module provides reusable functions for:
- Creating pyang contexts and repositories
- Loading YANG modules and submodules
- Traversing pyang statement trees
- Extracting attributes, constraints, and type information

Used by:
- refactored_yang_comparator.py (comparison tool)
- ../code/parsing/pyang_extractor.py (extraction tool)
"""

import os
import sys
import re
from typing import Dict, List, Any, Optional, Tuple, Set
from pathlib import Path

try:
    from pyang import context, repository, statements
    from pyang.error import error_codes
except ImportError:
    print("ERROR: pyang not installed. Install with: pip install pyang")
    sys.exit(1)


class PyangContext:
    """Wrapper for pyang context creation and module loading."""
    
    def __init__(self, search_dirs: List[str] = None):
        """
        Initialize pyang context.
        
        Args:
            search_dirs: List of directories to search for YANG modules
        """
        # Create repository with search directories
        if search_dirs:
            self.repos = repository.FileRepository(path=":".join(search_dirs))
        else:
            self.repos = repository.FileRepository()
        
        # Create options object (needed for Context)
        class Options:
            def __init__(self):
                self.lint_namespace_prefixes = []
                self.lint_modulename_prefixes = []
                self.verbose = False
                self.max_line_len = None
                self.max_identifier_len = None
        
        self.opts = Options()
        self.ctx = context.Context(self.repos)
        self.ctx.opts = self.opts
        self.last_error_messages: List[str] = []
        
        # Track loaded modules
        self.loaded_modules = {}  # module_name -> pyang module statement

    def _collect_error_messages(self) -> List[str]:
        """Collect readable pyang parser/validation errors from context."""
        messages: List[str] = []
        err_formatter = getattr(self.ctx, "err_to_str", None)

        for err in getattr(self.ctx, "errors", []) or []:
            if not isinstance(err, tuple) or len(err) != 3:
                messages.append(str(err))
                continue

            pos, tag, args = err
            if callable(err_formatter):
                try:
                    rendered = err_formatter(tag, args)  # type: ignore[operator]
                except Exception:
                    rendered = f"{tag}: {args}"
            else:
                rendered = f"{tag}: {args}"

            location = ""
            if pos is not None:
                ref = getattr(pos, "ref", None)
                line = getattr(pos, "line", None)
                if ref and line:
                    location = f"{os.path.basename(ref)}:{line}: "
                elif ref:
                    location = f"{os.path.basename(ref)}: "

            messages.append(f"{location}{rendered}")

        return messages
    
    def load_module(self, module_path: str) -> Optional[statements.Statement]:
        """
        Load a YANG module file using pyang.
        
        Args:
            module_path: Path to the YANG module file
            
        Returns:
            Pyang module statement or None if loading failed
        """
        try:
            self.last_error_messages = []
            # Read module file
            with open(module_path, 'r', encoding='utf-8') as f:
                text = f.read()
            
            # Parse module
            module = self.ctx.add_module(module_path, text)
            self.last_error_messages = self._collect_error_messages()
            
            if module is None:
                print(f"[PyangUtils] Failed to parse module: {module_path}")
                for msg in self.last_error_messages[:8]:
                    print(f"  - {msg}")
                return None
            
            # Validate module
            self.ctx.validate()
            self.last_error_messages = self._collect_error_messages()
            
            # Check for errors
            if self.last_error_messages:
                print(f"[PyangUtils] Errors in {module_path}:")
                for msg in self.last_error_messages[:8]:
                    print(f"  - {msg}")
                # Continue anyway - partial parsing may still work
            
            # Cache module
            module_name = module.arg
            self.loaded_modules[module_name] = module
            
            return module
            
        except Exception as e:
            self.last_error_messages = [str(e)]
            print(f"[PyangUtils] Exception loading {module_path}: {e}")
            return None
    
    def get_module(self, module_name: str) -> Optional[statements.Statement]:
        """Get a loaded module by name."""
        return self.loaded_modules.get(module_name)


class PyangStatementHelper:
    """Helper functions for working with pyang statements."""
    
    @staticmethod
    def get_keyword(stmt: statements.Statement) -> str:
        """
        Get the keyword (node type) of a statement.
        
        Args:
            stmt: Pyang statement
            
        Returns:
            Keyword string (e.g., 'leaf', 'container', 'type')
            For extensions, returns only the extension name without prefix
        """
        if hasattr(stmt, 'keyword'):
            keyword = stmt.keyword
            # Handle extension keywords (tuple format: (prefix, keyword))
            # Return only the keyword part without the prefix for cleaner output
            if isinstance(keyword, tuple):
                return str(keyword[1])
            return str(keyword)
        return "unknown"
    
    @staticmethod
    def get_argument(stmt: statements.Statement) -> Optional[str]:
        """
        Get the argument of a statement.
        
        Args:
            stmt: Pyang statement
            
        Returns:
            Argument string or None
        """
        return stmt.arg if hasattr(stmt, 'arg') else None
    
    @staticmethod
    def get_substmts(stmt: statements.Statement) -> List[statements.Statement]:
        """
        Get child statements (substmts) of a statement.
        
        Args:
            stmt: Pyang statement
            
        Returns:
            List of child statements
        """
        return stmt.substmts if hasattr(stmt, 'substmts') else []
    
    @staticmethod
    def get_position_info(stmt: statements.Statement) -> Tuple[Optional[int], Optional[str]]:
        """
        Extract line number and file path from a pyang statement.
        
        Args:
            stmt: Pyang statement
            
        Returns:
            Tuple of (line_number, file_path). Both may be None if not available.
        """
        line_number = None
        file_path = None
        
        if hasattr(stmt, 'pos') and stmt.pos:
            if hasattr(stmt.pos, 'line'):
                line_number = stmt.pos.line
            if hasattr(stmt.pos, 'ref'):
                file_path = stmt.pos.ref
                # Extract just the filename (not full path) for cleaner output
                if file_path:
                    file_path = os.path.basename(file_path)
        
        return line_number, file_path
    
    @staticmethod
    def find_substmt(stmt: statements.Statement, keyword: str) -> Optional[statements.Statement]:
        """
        Find first child statement with given keyword.
        
        Args:
            stmt: Parent statement
            keyword: Keyword to search for
            
        Returns:
            First matching child statement or None
        """
        for sub in PyangStatementHelper.get_substmts(stmt):
            if PyangStatementHelper.get_keyword(sub) == keyword:
                return sub
        return None
    
    @staticmethod
    def search_substmts(stmt: statements.Statement, keyword: str) -> List[statements.Statement]:
        """
        Find all child statements with given keyword.
        
        Args:
            stmt: Parent statement
            keyword: Keyword to search for
            
        Returns:
            List of matching child statements
        """
        results = []
        for sub in PyangStatementHelper.get_substmts(stmt):
            if PyangStatementHelper.get_keyword(sub) == keyword:
                results.append(sub)
        return results
    
    @staticmethod
    def find_all_substmts(stmt: statements.Statement, keyword: str) -> List[statements.Statement]:
        """
        Find all child statements with given keyword.
        
        Args:
            stmt: Parent statement
            keyword: Keyword to search for
            
        Returns:
            List of matching child statements
        """
        results = []
        for sub in PyangStatementHelper.get_substmts(stmt):
            if PyangStatementHelper.get_keyword(sub) == keyword:
                results.append(sub)
        return results
    
    @staticmethod
    def extract_attributes(stmt: statements.Statement, attribute_keywords: Set[str] = None) -> Dict[str, Any]:
        """
        Extract attributes from a statement.
        
        Default attributes: description, reference, units, default, status, config, mandatory
        
        Args:
            stmt: Pyang statement
            attribute_keywords: Set of attribute keywords to extract (optional)
            
        Returns:
            Dictionary of attribute name -> value
        """
        if attribute_keywords is None:
            attribute_keywords = {
                'description', 'reference', 'units', 'default', 'status',
                'config', 'mandatory', 'base', 'namespace', 'prefix',
                'yang-version', 'contact', 'organization', 'revision-date',
                'value', 'position', 'error-message', 'error-app-tag',
                'path', 'key', 'unique', 'ordered-by', 'presence'
            }
        
        attributes = {}
        
        for sub in PyangStatementHelper.get_substmts(stmt):
            keyword = PyangStatementHelper.get_keyword(sub)
            if keyword in attribute_keywords:
                arg = PyangStatementHelper.get_argument(sub)
                if arg is not None:
                    attributes[keyword] = arg
        
        return attributes
    
    @staticmethod
    def extract_constraints(stmt: statements.Statement, constraint_keywords: Set[str] = None) -> Dict[str, Any]:
        """
        Extract constraints from a statement.
        
        Default constraints: range, length, pattern, must, when, etc.
        
        Args:
            stmt: Pyang statement
            constraint_keywords: Set of constraint keywords to extract (optional)
            
        Returns:
            Dictionary of constraint name -> value
        """
        if constraint_keywords is None:
            constraint_keywords = {
                'range', 'length', 'pattern', 'must', 'when', 'unique',
                'min-elements', 'max-elements', 'fraction-digits',
                'if-feature', 'require-instance', 'modifier'
            }
        
        constraints = {}
        
        for sub in PyangStatementHelper.get_substmts(stmt):
            keyword = PyangStatementHelper.get_keyword(sub)
            if keyword in constraint_keywords:
                arg = PyangStatementHelper.get_argument(sub)
                if arg is not None:
                    # Handle multiple constraints of same type (e.g., multiple 'must')
                    if keyword in constraints:
                        # Convert to list if not already
                        if not isinstance(constraints[keyword], list):
                            constraints[keyword] = [constraints[keyword]]
                        constraints[keyword].append(arg)
                    else:
                        constraints[keyword] = arg
        
        return constraints
    
    @staticmethod
    def extract_type_info(stmt: statements.Statement) -> Optional[Dict[str, Any]]:
        """
        Extract type information from a type statement.
        
        Args:
            stmt: Pyang type statement
            
        Returns:
            Dictionary with type information or None
        """
        keyword = PyangStatementHelper.get_keyword(stmt)
        if keyword != 'type':
            return None
        
        type_name = PyangStatementHelper.get_argument(stmt)
        if not type_name:
            return None
        
        type_info = {'name': type_name}
        
        # Extract type-specific details
        for sub in PyangStatementHelper.get_substmts(stmt):
            sub_keyword = PyangStatementHelper.get_keyword(sub)
            sub_arg = PyangStatementHelper.get_argument(sub)
            
            if sub_keyword == 'range':
                type_info['range'] = sub_arg
            elif sub_keyword == 'length':
                type_info['length'] = sub_arg
            elif sub_keyword == 'pattern':
                if 'pattern' not in type_info:
                    type_info['pattern'] = []
                type_info['pattern'].append(sub_arg)
            elif sub_keyword == 'enum':
                if 'enum' not in type_info:
                    type_info['enum'] = []
                enum_data = {'name': sub_arg}
                # Extract enum attributes
                for enum_sub in PyangStatementHelper.get_substmts(sub):
                    enum_keyword = PyangStatementHelper.get_keyword(enum_sub)
                    enum_arg = PyangStatementHelper.get_argument(enum_sub)
                    if enum_arg:
                        enum_data[enum_keyword] = enum_arg
                type_info['enum'].append(enum_data)
            elif sub_keyword == 'bit':
                if 'bit' not in type_info:
                    type_info['bit'] = []
                bit_data = {'name': sub_arg}
                # Extract bit attributes
                for bit_sub in PyangStatementHelper.get_substmts(sub):
                    bit_keyword = PyangStatementHelper.get_keyword(bit_sub)
                    bit_arg = PyangStatementHelper.get_argument(bit_sub)
                    if bit_arg:
                        bit_data[bit_keyword] = bit_arg
                type_info['bit'].append(bit_data)
            elif sub_keyword == 'path':
                type_info['path'] = sub_arg
            elif sub_keyword == 'base':
                if 'base' not in type_info:
                    type_info['base'] = []
                type_info['base'].append(sub_arg)
            elif sub_keyword == 'require-instance':
                type_info['require-instance'] = sub_arg
            elif sub_keyword == 'fraction-digits':
                type_info['fraction-digits'] = sub_arg
            elif sub_arg:
                # Capture other type properties
                type_info[sub_keyword] = sub_arg
        
        return type_info
    
    @staticmethod
    def build_path(stmt: statements.Statement, include_module: bool = True) -> str:
        """
        Build XPath-like path for a statement.
        
        Args:
            stmt: Pyang statement
            include_module: Include module name at start of path
            
        Returns:
            Path string (e.g., 'module-name/container/leaf')
        """
        parts = []
        current = stmt
        
        while current is not None:
            keyword = PyangStatementHelper.get_keyword(current)
            arg = PyangStatementHelper.get_argument(current)
            
            # For structural keywords, use argument as path component
            if keyword in {'module', 'submodule', 'container', 'leaf', 'list',
                          'leaf-list', 'choice', 'case', 'grouping', 'typedef',
                          'rpc', 'notification', 'action', 'identity', 'extension',
                          'feature', 'input', 'output'}:
                if arg:
                    parts.insert(0, arg)
                elif keyword in {'input', 'output'}:
                    parts.insert(0, keyword)
            
            # For type statements, add type keyword
            elif keyword == 'type':
                if arg:
                    parts.insert(0, arg)
            
            # Move to parent
            current = current.parent if hasattr(current, 'parent') else None
        
        # If we want just the path without module, and we have a module at start
        if not include_module and len(parts) > 0:
            # Check if first part is a module/submodule
            if parts and stmt.top and stmt.top.arg == parts[0]:
                parts = parts[1:]
        
        return '/'.join(parts) if parts else ''
    
    @staticmethod
    def is_submodule(stmt: statements.Statement) -> bool:
        """Check if statement is a submodule."""
        return PyangStatementHelper.get_keyword(stmt) == 'submodule'
    
    @staticmethod
    def get_module_name(stmt: statements.Statement) -> Optional[str]:
        """Get the module/submodule name from a statement."""
        # Find top-level module/submodule
        current = stmt
        while current is not None:
            keyword = PyangStatementHelper.get_keyword(current)
            if keyword in {'module', 'submodule'}:
                return PyangStatementHelper.get_argument(current)
            current = current.parent if hasattr(current, 'parent') else None
        return None
    
    @staticmethod
    def get_parent_keyword(stmt: statements.Statement) -> Optional[str]:
        """Get the keyword of the parent statement."""
        if hasattr(stmt, 'parent') and stmt.parent:
            return PyangStatementHelper.get_keyword(stmt.parent)
        return None


def create_pyang_context(search_dirs: List[str] = None) -> PyangContext:
    """
    Create a pyang context for parsing YANG modules.
    
    Args:
        search_dirs: List of directories to search for YANG modules
        
    Returns:
        PyangContext instance
    """
    return PyangContext(search_dirs)


def load_yang_module(module_path: str, search_dirs: List[str] = None) -> Tuple[Optional[statements.Statement], PyangContext]:
    """
    Load a YANG module file.
    
    Args:
        module_path: Path to YANG module file
        search_dirs: Additional directories to search for imports
        
    Returns:
        Tuple of (module statement, context) or (None, context) on failure
    """
    # Ensure module directory is in search path
    module_dir = os.path.dirname(os.path.abspath(module_path))
    if search_dirs is None:
        search_dirs = [module_dir]
    elif module_dir not in search_dirs:
        search_dirs = [module_dir] + list(search_dirs)
    
    ctx = create_pyang_context(search_dirs)
    module = ctx.load_module(module_path)
    
    return module, ctx


def parse_yang_with_pyang(yang_file: str, search_dir: str) -> Optional[Dict]:
    """
    Parse a YANG file using pyang and convert to dictionary format for recursive expansion.
    
    Args:
        yang_file: Path to YANG file or just the filename
        search_dir: Directory containing the YANG file and dependencies
        
    Returns:
        Dictionary representation of the YANG module suitable for recursive expansion,
        or None if parsing fails
    """
    try:
        # Build full path if needed
        if not os.path.isabs(yang_file):
            yang_path = os.path.join(search_dir, yang_file)
        else:
            yang_path = yang_file
        
        if not os.path.exists(yang_path):
            print(f"ERROR: YANG file not found: {yang_path}")
            return None
        
        # Load the module
        module, ctx = load_yang_module(yang_path, [search_dir])
        
        if not module:
            print(f"ERROR: Could not load YANG module: {yang_path}")
            return None
        
        # Convert pyang AST to dictionary
        result = {
            'module': _statement_to_dict(module)
        }
        
        return result
    
    except Exception as e:
        print(f"ERROR parsing YANG file {yang_file}: {e}")
        import traceback
        traceback.print_exc()
        return None


def _statement_to_dict(stmt: statements.Statement) -> Dict:
    """
    Convert a pyang statement to a dictionary recursively.
    
    Args:
        stmt: Pyang statement
        
    Returns:
        Dictionary representation
    """
    result = {}
    
    # Add name if present
    if hasattr(stmt, 'arg') and stmt.arg:
        result['name'] = stmt.arg
    
    # Process substatementsgrouped by keyword
    keyword_groups = {}
    for substmt in stmt.substmts:
        kw = PyangStatementHelper.get_keyword(substmt)
        
        if kw not in keyword_groups:
            keyword_groups[kw] = []
        keyword_groups[kw].append(substmt)
    
    # Convert each group
    for kw, stmts in keyword_groups.items():
        if len(stmts) == 1:
            # Single statement - convert to dict
            sub_dict = _statement_to_dict(stmts[0])
            result[kw] = sub_dict
        else:
            # Multiple statements - convert to list of dicts
            result[kw] = [_statement_to_dict(s) for s in stmts]
    
    return result


# Tags that indicate a broken must/when XPath condition (node not found in schema
# or path is structurally invalid).
_XPATH_BROKEN_TAGS: Set[str] = {
    'XPATH_NODE_NOT_FOUND1',
    'XPATH_NODE_NOT_FOUND2',
    'XPATH_FUNCTION_NOT_FOUND',
    'XPATH_VARIABLE_NOT_FOUND',
    # Path navigates more levels up than the context depth allows.
    # This is a genuine structural error — the XPath is always-broken regardless
    # of data values.  The fallback tail resolver may still find a node by
    # searching the schema tree, but pyang's authoritative signal takes precedence.
    'XPATH_PATH_TOO_MANY_UP',
}

# Tags that indicate a missing reference (identity, typedef, grouping, feature,
# extension) that pyang could not resolve.  These are authoritative signals that
# a symbol referenced in the OLD file does not exist in the module it was
# declared to come from — i.e. the old YANG file is already broken/invalid.
_MISSING_REF_TAGS: Set[str] = {
    'IDENTITY_NOT_FOUND',
    'TYPEDEF_NOT_FOUND',
    'GROUPING_NOT_FOUND',
    'FEATURE_NOT_FOUND',
    'EXTENSION_NOT_FOUND',
    # Cyclic identity/typedef dependencies are also unresolvable references.
    'CIRCULAR_DEPENDENCY',
}

# Process-level cache for get_pyang_xpath_broken_lines.
# Key: (abs_yang_file_path, tuple(sorted_search_dirs))
# Value: Set[int] of broken line numbers
_xpath_broken_lines_cache: Dict[tuple, Set[int]] = {}

# Process-level cache for get_pyang_missing_ref_lines.
# Key: (abs_yang_file_path, tuple(sorted_search_dirs))
# Value: Set[int] of line numbers with missing-reference errors
_missing_ref_lines_cache: Dict[tuple, Set[int]] = {}


def get_pyang_xpath_broken_lines(
    yang_file: str,
    search_dirs: Optional[List[str]] = None,
) -> Set[int]:
    """
    Load a YANG file with pyang and return the set of line numbers (in that file)
    where pyang reports broken XPath conditions (XPATH_NODE_NOT_FOUND* errors).

    These line numbers correspond to ``must`` or ``when`` statements whose XPath
    expression references a node that does not exist in the schema tree — i.e. the
    condition is always-FALSE.

    Only errors whose ``pos.ref`` resolves to the same file as *yang_file* are
    included; errors in imported/included modules are ignored.

    Results are cached in a process-level dict so that pyang is invoked at most
    once per (file, search_dirs) pair per process.

    Args:
        yang_file: Absolute or relative path to the YANG file to validate.
        search_dirs: Additional directories to search for imported modules.

    Returns:
        Set of 1-based line numbers in *yang_file* where broken XPath conditions
        are reported.  Returns an empty set on any loading failure.
    """
    yang_file_abs = os.path.abspath(yang_file)
    file_dir = os.path.dirname(yang_file_abs)

    if search_dirs is None:
        effective_dirs = [file_dir]
    else:
        effective_dirs = list(search_dirs)
        if file_dir not in effective_dirs:
            effective_dirs.insert(0, file_dir)

    cache_key = (yang_file_abs, tuple(sorted(effective_dirs)))
    if cache_key in _xpath_broken_lines_cache:
        return _xpath_broken_lines_cache[cache_key]

    try:
        ctx = create_pyang_context(effective_dirs)
        ctx.load_module(yang_file_abs)

        broken_lines: Set[int] = set()
        for pos, tag, _args in ctx.ctx.errors:
            if tag not in _XPATH_BROKEN_TAGS:
                continue
            try:
                err_file_abs = os.path.abspath(pos.ref)
            except Exception:
                continue
            if err_file_abs != yang_file_abs:
                continue
            broken_lines.add(pos.line)

        _xpath_broken_lines_cache[cache_key] = broken_lines
        return broken_lines

    except Exception:
        _xpath_broken_lines_cache[cache_key] = set()
        return set()


def get_pyang_missing_ref_lines(
    yang_file: str,
    search_dirs: Optional[List[str]] = None,
) -> Set[int]:
    """
    Load a YANG file with pyang and return the set of line numbers (in that file)
    where pyang reports missing-reference errors (IDENTITY_NOT_FOUND,
    TYPEDEF_NOT_FOUND, GROUPING_NOT_FOUND, FEATURE_NOT_FOUND,
    EXTENSION_NOT_FOUND, CIRCULAR_DEPENDENCY).

    These line numbers correspond to statements that reference a symbol
    (identity base, typedef, grouping, feature, extension) that pyang could
    not resolve in the declared module — i.e. the reference is broken in the
    OLD YANG file.

    Only errors whose ``pos.ref`` resolves to the same file as *yang_file* are
    included; errors in imported/included modules are ignored.

    Results are cached in a process-level dict so that pyang is invoked at most
    once per (file, search_dirs) pair per process.

    Args:
        yang_file: Absolute or relative path to the YANG file to validate.
        search_dirs: Additional directories to search for imported modules.

    Returns:
        Set of 1-based line numbers in *yang_file* where missing-reference
        errors are reported.  Returns an empty set on any loading failure.
    """
    yang_file_abs = os.path.abspath(yang_file)
    file_dir = os.path.dirname(yang_file_abs)

    if search_dirs is None:
        effective_dirs = [file_dir]
    else:
        effective_dirs = list(search_dirs)
        if file_dir not in effective_dirs:
            effective_dirs.insert(0, file_dir)

    cache_key = (yang_file_abs, tuple(sorted(effective_dirs)))
    if cache_key in _missing_ref_lines_cache:
        return _missing_ref_lines_cache[cache_key]

    try:
        ctx = create_pyang_context(effective_dirs)
        ctx.load_module(yang_file_abs)

        missing_lines: Set[int] = set()
        for pos, tag, _args in ctx.ctx.errors:
            if tag not in _MISSING_REF_TAGS:
                continue
            try:
                err_file_abs = os.path.abspath(pos.ref)
            except Exception:
                continue
            if err_file_abs != yang_file_abs:
                continue
            missing_lines.add(pos.line)

        _missing_ref_lines_cache[cache_key] = missing_lines
        return missing_lines

    except Exception:
        _missing_ref_lines_cache[cache_key] = set()
        return set()


# Export main classes and functions
__all__ = [
    'PyangContext',
    'PyangStatementHelper',
    'create_pyang_context',
    'load_yang_module',
    'parse_yang_with_pyang',
    'get_pyang_xpath_broken_lines',
    'get_pyang_missing_ref_lines',
]
