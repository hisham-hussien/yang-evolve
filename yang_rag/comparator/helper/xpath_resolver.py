#!/usr/bin/env python3
"""
xpath_resolver.py - XPath resolution for YANG XPath-bearing statements

This module resolves relative XPath expressions in YANG XPath-bearing statements
to absolute schema paths, enabling semantic comparison of path changes.

Key functionality:
1. Parse YANG modules using pyang to build schema tree
2. Locate context node where the XPath statement is attached
3. Resolve relative XPath (../, ../../, etc) from context to absolute path
4. Compare resolved paths to determine if they reference the same node

Example:
    Old: when "../../../config/type = 'value'"  (at container tacacs)
    New: when "../../config/type = 'value'"     (at container tacacs)
    
    Resolution determines if these point to same or different nodes.
"""

import re
from typing import Optional, Tuple, List, Dict
from pathlib import Path
from dataclasses import dataclass

try:
    from pyang import statements
except ImportError:
    print("ERROR: pyang not installed. Install with: pip install pyang")
    import sys
    sys.exit(1)

try:
    from yang_rag.comparator.pyang_utils import PyangContext, PyangStatementHelper
except ImportError:
    try:
        from .pyang_utils import PyangContext, PyangStatementHelper
    except ImportError:
        from pyang_utils import PyangContext, PyangStatementHelper

try:
    from yang_rag.comparator.helper.xpath_function_resolver import XPathFunctionParser
except ImportError:
    try:
        from .xpath_function_resolver import XPathFunctionParser
    except ImportError:  # pragma: no cover
        XPathFunctionParser = None  # type: ignore


# ── Process-level XPathResolver cache ────────────────────────────────────────
# Key: (abs_yang_file_path, tuple(sorted_search_dirs))
# Value: XPathResolver instance
#
# A single comparison run analyses hundreds of when/must conditions from the
# same pair of YANG files.  Without caching, each ConditionAnalyzer instance
# creates a new XPathResolver which re-parses the YANG file with pyang, rebuilds
# the node cache, and rebuilds the expanded node cache — all identical work.
# This cache ensures pyang is invoked at most once per (file, search_dirs) pair
# per process.
_xpath_resolver_cache: Dict[tuple, 'XPathResolver'] = {}


def get_cached_xpath_resolver(
    yang_file_path: str,
    search_dirs: Optional[List[str]] = None,
) -> 'XPathResolver':
    """
    Return a cached ``XPathResolver`` for *yang_file_path*.

    The resolver is built once per (absolute file path, search dirs) combination
    and reused for all subsequent calls within the same process.  This avoids
    re-parsing the same YANG file with pyang for every condition that is analysed.

    Args:
        yang_file_path: Path to the YANG module file.
        search_dirs: Additional directories to search for imports/includes.

    Returns:
        A shared ``XPathResolver`` instance.

    Raises:
        ValueError: If the YANG module cannot be loaded (propagated from
            ``XPathResolver.__init__``).
    """
    abs_path = str(Path(yang_file_path).resolve())
    key = (abs_path, tuple(sorted(search_dirs or [])))
    if key not in _xpath_resolver_cache:
        _xpath_resolver_cache[key] = XPathResolver(yang_file_path, search_dirs)
    return _xpath_resolver_cache[key]


@dataclass
class ResolvedPath:
    """Represents a resolved XPath expression."""
    absolute_path: str          # Absolute schema path (e.g., /module/container/leaf)
    context_path: str           # Path of the context node where XPath starts
    relative_xpath: str         # Original relative XPath
    target_node: Optional[statements.Statement] = None  # Resolved pyang node
    success: bool = True        # Whether resolution succeeded
    error: Optional[str] = None # Error message if resolution failed


class XPathResolver:
    """
    Resolves relative XPath expressions in YANG XPath-bearing statements.
    
    This class handles:
    - Loading YANG modules and building schema tree
    - Finding context nodes in the tree
    - Resolving relative paths (../, ../../, etc)
    - Comparing resolved paths across old/new versions
    """
    
    def __init__(self, yang_file_path: str, search_dirs: Optional[List[str]] = None):
        """
        Initialize resolver with a YANG file.

        If the file is a YANG submodule (contains a ``belongs-to`` statement),
        the resolver transparently loads the parent module instead.  Pyang
        expands ``include`` statements when loading the parent, so the full
        merged schema tree — including nodes defined in the parent module and
        all sibling submodules — is available for XPath resolution.  This
        allows relative paths that navigate above the submodule root (e.g.
        leafrefs pointing to nodes in the parent module) to be resolved
        correctly.

        Args:
            yang_file_path: Path to the YANG module or submodule file
            search_dirs: Additional directories to search for imports/includes
        """
        self.yang_file_path = yang_file_path

        # Build search directories
        if search_dirs is None:
            search_dirs = []

        # Add directory containing the YANG file
        file_dir = str(Path(yang_file_path).parent)
        if file_dir not in search_dirs:
            search_dirs.insert(0, file_dir)

        # Create pyang context and load module
        self.pyang_ctx = PyangContext(search_dirs=search_dirs)
        self.module = self.pyang_ctx.load_module(yang_file_path)

        if self.module is None:
            raise ValueError(f"Failed to load YANG module: {yang_file_path}")

        # ── Submodule promotion ───────────────────────────────────────────────
        # If the loaded file is a submodule, try to load the parent module
        # instead so that the full merged schema tree is available.  Pyang
        # expands `include` statements when loading the parent, giving us
        # access to nodes defined in the parent and all sibling submodules.
        #
        # We also record the original submodule name so that context paths
        # starting with the submodule name (e.g. /openconfig-mpls-te/...)
        # can be correctly mapped to the parent module's schema tree.
        self._submodule_name: Optional[str] = None
        if PyangStatementHelper.get_keyword(self.module) == 'submodule':
            self._submodule_name = PyangStatementHelper.get_argument(self.module)
        parent_module = self._try_load_parent_module(search_dirs)
        if parent_module is not None:
            self.module = parent_module

        # Build a set of (file_path, line_number) pairs for leafref path
        # statements that pyang flagged as LEAFREF_IDENTIFIER_NOT_FOUND.
        # This is the authoritative invalidity signal from pyang's own
        # validation — used by _resolve_leafref_via_pyang() to determine
        # whether a leafref path is broken without relying on i_target_node
        # (which pyang only sets for leafrefs instantiated outside groupings).
        self._leafref_error_positions: set = self._collect_leafref_error_positions()

        # Map from leafref path string value → pyang Statement for the 'path'
        # sub-statement.  Built after node_cache is populated so we can scan
        # all leafref nodes.  Used by resolve_xpath() to find the authoritative
        # pyang 'path' statement for a given xpath string without needing to
        # locate the exact context node (which may use a different grouping
        # prefix than what's in the cache).
        # Populated lazily in _build_leafref_path_stmt_map() after caches are ready.
        self._leafref_path_stmt_map: Dict[str, statements.Statement] = {}

        # Raw AST cache (fallback only).
        self.node_cache: Dict[str, statements.Statement] = {}
        self._build_node_cache(self.module)

        # Expanded schema cache based on pyang runtime expansion (i_children).
        self.expanded_node_cache: Dict[str, statements.Statement] = {}
        self._build_expanded_node_cache()

        # Build the leafref path-string → 'path' statement map now that
        # node_cache is populated.
        self._build_leafref_path_stmt_map()

        # Optional function-aware preprocessor for current(), predicates, etc.
        self._function_parser = XPathFunctionParser() if XPathFunctionParser else None

    def _try_load_parent_module(self, search_dirs: List[str]) -> Optional[statements.Statement]:
        """
        If ``self.module`` is a submodule, locate and load its parent module.

        The parent module name is read from the ``belongs-to`` statement.  The
        corresponding ``.yang`` file is searched for in ``search_dirs``.  If
        found and successfully loaded, the parent module statement is returned;
        otherwise ``None`` is returned and the caller continues with the
        submodule as-is.

        Args:
            search_dirs: Directories to search for the parent module file.

        Returns:
            Parent module pyang statement, or ``None`` if not applicable /
            not found.
        """
        keyword = PyangStatementHelper.get_keyword(self.module)
        if keyword != 'submodule':
            return None

        # Find the belongs-to statement to get the parent module name
        parent_name: Optional[str] = None
        for child in PyangStatementHelper.get_substmts(self.module):
            if PyangStatementHelper.get_keyword(child) == 'belongs-to':
                parent_name = PyangStatementHelper.get_argument(child)
                break

        if not parent_name:
            return None

        # Search for <parent-name>.yang in all search directories
        for search_dir in search_dirs:
            candidate = Path(search_dir) / f"{parent_name}.yang"
            if candidate.is_file():
                try:
                    parent_module = self.pyang_ctx.load_module(str(candidate))
                    if parent_module is not None:
                        return parent_module
                except Exception:
                    pass  # Fall back to submodule if parent load fails

        return None

    def _collect_leafref_error_positions(self) -> set:
        """
        Collect (file_path, line_number) pairs for all leafref ``path``
        statements that pyang flagged as ``LEAFREF_IDENTIFIER_NOT_FOUND``.

        Pyang stores validation errors in ``ctx.errors`` as
        ``(pos, tag, args)`` tuples.  The ``pos`` object carries the source
        file path (``pos.ref``) and line number (``pos.line``) of the
        offending statement.  We collect all such positions so that
        ``_resolve_leafref_via_pyang()`` can check whether a specific
        leafref ``path`` statement was flagged as invalid — without relying
        on ``i_target_node`` which pyang only sets for leafrefs instantiated
        outside groupings.

        Returns:
            Set of ``(ref, line)`` tuples for invalid leafref path statements.
        """
        invalid_positions: set = set()
        try:
            for err in getattr(self.pyang_ctx.ctx, 'errors', []) or []:
                if not isinstance(err, tuple) or len(err) != 3:
                    continue
                pos, tag, _args = err
                if tag != 'LEAFREF_IDENTIFIER_NOT_FOUND':
                    continue
                ref = getattr(pos, 'ref', None)
                line = getattr(pos, 'line', None)
                if ref is not None and line is not None:
                    invalid_positions.add((ref, line))
        except Exception:
            pass
        return invalid_positions

    def _leafref_path_stmt_is_invalid(self, path_stmt: statements.Statement) -> bool:
        """
        Return True if pyang flagged this specific leafref ``path`` statement
        as ``LEAFREF_IDENTIFIER_NOT_FOUND``.

        The check is based on the source position (file + line) of the
        ``path`` statement, matched against the set of invalid positions
        collected at module load time.

        Args:
            path_stmt: The pyang ``path`` sub-statement of a ``type leafref``.

        Returns:
            True when pyang reported this path as unresolvable.
        """
        try:
            pos = getattr(path_stmt, 'pos', None)
            if pos is None:
                return False
            ref = getattr(pos, 'ref', None)
            line = getattr(pos, 'line', None)
            return (ref, line) in self._leafref_error_positions
        except Exception:
            return False

    def _build_leafref_path_stmt_map(self):
        """
        Build ``_leafref_path_stmt_map``: a mapping from leafref path string
        value to the pyang ``path`` sub-statement.

        Scans the raw ``node_cache`` (which includes grouping-level nodes) for
        all leaf/leaf-list nodes that carry a ``type leafref`` sub-statement,
        and records the ``path`` statement keyed by its string value.

        When multiple leaves share the same leafref path string (e.g. both
        ``config/admin-group-name`` and ``state/admin-group-name`` use
        ``../config/admin-group-name``), the first one encountered is stored.
        This is sufficient because we only need the ``path`` statement to check
        its position against ``_leafref_error_positions`` — all occurrences of
        the same path string in the same file share the same invalidity status.
        """
        for _node_path, node in self.node_cache.items():
            if PyangStatementHelper.get_keyword(node) not in {'leaf', 'leaf-list'}:
                continue
            for sub in getattr(node, 'substmts', []):
                if getattr(sub, 'keyword', None) == 'type' and getattr(sub, 'arg', None) == 'leafref':
                    for tsub in getattr(sub, 'substmts', []):
                        if getattr(tsub, 'keyword', None) == 'path':
                            path_val = getattr(tsub, 'arg', None)
                            if path_val and path_val not in self._leafref_path_stmt_map:
                                self._leafref_path_stmt_map[path_val] = tsub
                    break

    def _is_malformed_absolute_path(self, absolute_path: str) -> bool:
        """Return True when an absolute path contains '.' or '..' segments."""
        parts = [p for p in absolute_path.split('/') if p]
        return any(part in {'.', '..'} for part in parts)

    def _extract_structural_path(self, context_path: str, xpath_expr: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract a structural path from a raw XPath expression.

        Returns:
            (path, error)
        """
        expr = xpath_expr.strip()

        # Remove outer quotes if present
        if (expr.startswith('"') and expr.endswith('"')) or \
           (expr.startswith("'") and expr.endswith("'")):
            expr = expr[1:-1].strip()

        # Prefer function-aware parsing so predicates/current() are handled
        # before path navigation logic.
        if self._function_parser is not None:
            try:
                parsed = self._function_parser.parse(expr, context_path)
                if parsed.error:
                    return None, parsed.error
                if parsed.no_path or not parsed.primary_path:
                    return None, 'XPath expression has no resolvable schema path'
                return parsed.primary_path.strip(), None
            except Exception:
                # Fall back to regex extractor below for robustness.
                pass

        # Legacy fallback: extract path before operators.
        path_match = re.match(r'^([^\s=!<>]+)', expr)
        if not path_match:
            return None, 'Could not extract path from XPath expression'

        return path_match.group(1).strip(), None

    def _normalize_identifier(self, identifier: str) -> str:
        """Normalize YANG identifier by stripping optional module prefix."""
        if ':' in identifier:
            return identifier.split(':', 1)[1]
        return identifier

    def _canonicalize_context_path(self, context_path: str) -> str:
        """
        Canonicalize a context path to an instantiated data-tree path when possible.

        Some reports provide grouping-level context paths (e.g.
        /module/my-grouping/container/list) where `my-grouping` is not part of the
        instantiated runtime schema tree. Relative path resolution should still use
        the runtime tree, so we try to drop non-instantiated leading segments after
        the module root until a resolvable context is found.

        When the resolver was promoted from a submodule to its parent module,
        context paths that start with the submodule name (e.g.
        /openconfig-mpls-te/...) are treated as aliases for the parent module
        root (e.g. /openconfig-mpls/...) so that the progressive-drop loop
        can find the correct instantiated path in the parent's schema tree.
        """
        if not context_path or not context_path.startswith('/'):
            return context_path

        canonical, target, _ = self._resolve_expanded_absolute_path(context_path)
        if canonical and target is not None:
            return canonical

        module_name = PyangStatementHelper.get_argument(self.module)
        if not module_name:
            return context_path

        raw_parts = [p for p in context_path.split('/') if p]
        if not raw_parts:
            return context_path

        first_local = self._normalize_identifier(raw_parts[0])
        if first_local == self._normalize_identifier(module_name):
            # Context path starts with the (parent) module name — strip it.
            tail = raw_parts[1:]
        elif self._submodule_name and first_local == self._normalize_identifier(self._submodule_name):
            # Context path starts with the submodule name — treat it as an
            # alias for the parent module root and strip it.
            tail = raw_parts[1:]
        else:
            tail = raw_parts

        normalized_tail = [self._normalize_identifier(p) for p in tail]

        # Try progressively dropping leading segments after module root, but
        # do not collapse to the bare module root. Returning '/<module>' here
        # causes relative paths to appear to have zero navigable depth and
        # triggers false "levels up but only 0 levels available" errors.
        for start in range(len(normalized_tail)):
            candidate_parts = normalized_tail[start:]
            candidate = '/' + module_name
            if candidate_parts:
                candidate += '/' + '/'.join(candidate_parts)

            can, tgt, _ = self._resolve_expanded_absolute_path(candidate)
            if can and tgt is not None:
                return can

        return context_path
    
    def _build_node_cache(self, stmt: statements.Statement, current_path: str = ""):
        """
        Recursively build cache of all nodes in the schema tree.
        
        Args:
            stmt: Current pyang statement
            current_path: Accumulated path to current node
        """
        keyword = PyangStatementHelper.get_keyword(stmt)
        arg = PyangStatementHelper.get_argument(stmt)
        
        # Data definition keywords that form the schema tree
        data_keywords = {
            'module', 'submodule', 'container', 'leaf', 'list', 'leaf-list',
            'anydata', 'anyxml', 'choice', 'case', 'grouping', 'uses',
            'augment', 'rpc', 'action', 'notification', 'input', 'output'
        }
        
        # Update path for data nodes
        if keyword in data_keywords:
            if keyword in {'module', 'submodule'}:
                current_path = f"/{arg}" if arg else ""
            elif keyword in {'input', 'output'}:
                current_path = f"{current_path}/{keyword}"
            elif keyword == 'uses':
                # `uses` itself is not a runtime path segment.
                pass
            elif arg:
                current_path = f"{current_path}/{arg}"
            
            # Cache this node
            if current_path:
                self.node_cache[current_path] = stmt
        
        # Recurse into children
        for child in PyangStatementHelper.get_substmts(stmt):
            self._build_node_cache(child, current_path)

    def _iter_expanded_children(self, stmt: statements.Statement) -> List[statements.Statement]:
        """Return runtime-expanded children when available, otherwise raw substatements."""
        expanded = getattr(stmt, 'i_children', None)
        if expanded:
            return list(expanded)
        return PyangStatementHelper.get_substmts(stmt)

    def _build_expanded_node_cache(self):
        """Build path cache using pyang-expanded schema tree (i_children)."""
        module_name = PyangStatementHelper.get_argument(self.module)
        if not module_name:
            return

        root_path = f"/{module_name}"
        self.expanded_node_cache[root_path] = self.module

        def _walk(parent: statements.Statement, parent_path: str):
            for child in self._iter_expanded_children(parent):
                keyword = PyangStatementHelper.get_keyword(child)
                arg = PyangStatementHelper.get_argument(child)

                if keyword in {'input', 'output'}:
                    child_path = f"{parent_path}/{keyword}"
                elif arg:
                    child_path = f"{parent_path}/{arg}"
                else:
                    continue

                self.expanded_node_cache[child_path] = child
                _walk(child, child_path)

        _walk(self.module, root_path)

    def _resolve_module_for_prefix(self, prefix: str) -> Optional[statements.Statement]:
        """
        Resolve a YANG module prefix to its module statement using the pyang context.

        When an absolute XPath starts with a prefixed segment (e.g. ``/ocif:interfaces``),
        the prefix ``ocif`` refers to an imported module (``openconfig-interfaces``).
        This method looks up the module statement for that prefix so that the resolver
        can navigate the correct module's schema tree.

        Args:
            prefix: The namespace prefix string (e.g. ``'ocif'``, ``'oc-if'``).

        Returns:
            The pyang module statement for the module that the prefix refers to,
            or ``None`` if the prefix cannot be resolved.
        """
        try:
            ctx = self.pyang_ctx.ctx
            # Search all import statements in the current module and its submodules
            # to find which module name the prefix maps to.
            stmts_to_search = [self.module]
            try:
                module_name = PyangStatementHelper.get_argument(self.module)
                for (_mod_name, _rev), mod_stmt in ctx.modules.items():
                    if PyangStatementHelper.get_keyword(mod_stmt) == 'submodule':
                        for child in PyangStatementHelper.get_substmts(mod_stmt):
                            if PyangStatementHelper.get_keyword(child) == 'belongs-to':
                                if PyangStatementHelper.get_argument(child) == module_name:
                                    stmts_to_search.append(mod_stmt)
                                    break
            except Exception:
                pass

            for stmt in stmts_to_search:
                # Check the module's own prefix
                own_pfx = stmt.search_one('prefix')
                if own_pfx and own_pfx.arg == prefix:
                    return self.module
                # Check import statements
                for sub in PyangStatementHelper.get_substmts(stmt):
                    if PyangStatementHelper.get_keyword(sub) == 'import':
                        pfx_stmt = sub.search_one('prefix')
                        if pfx_stmt and pfx_stmt.arg == prefix:
                            imported_name = PyangStatementHelper.get_argument(sub)
                            # Find the module in the pyang context
                            for (_mod_name, _rev), mod_stmt in ctx.modules.items():
                                if _mod_name == imported_name:
                                    return mod_stmt
        except Exception:
            pass
        return None

    def _resolve_expanded_absolute_path(self, absolute_path: str) -> Tuple[Optional[str], Optional[statements.Statement], Optional[str]]:
        """
        Resolve an absolute path using expanded schema navigation from module root.

        Handles prefixed absolute paths (e.g. ``/ocif:interfaces/ocif:interface/...``)
        by resolving the prefix of the first segment to the correct imported module
        and navigating that module's schema tree.

        Returns:
            (canonical_path, target_node, error)
        """
        module_name = PyangStatementHelper.get_argument(self.module)
        if not module_name:
            return None, None, 'Module name is unavailable for schema traversal'

        raw_parts = [p for p in absolute_path.split('/') if p]
        if not raw_parts:
            return None, None, 'Empty absolute XPath'

        # Determine the starting module and path parts.
        # If the first segment's local name matches the current module name → strip it.
        # If the first segment has a prefix that maps to a different module → use that module.
        first_local = self._normalize_identifier(raw_parts[0])
        if first_local == self._normalize_identifier(module_name):
            parts = raw_parts[1:]
            current = self.module
            canonical_parts = [module_name]
        elif ':' in raw_parts[0]:
            # Prefixed first segment — resolve the prefix to the correct module
            prefix = raw_parts[0].split(':', 1)[0]
            target_module = self._resolve_module_for_prefix(prefix)
            if target_module is not None:
                target_module_name = PyangStatementHelper.get_argument(target_module)
                # The first segment's local name is the first data node to navigate to
                parts = raw_parts  # keep all parts; navigation starts from target_module root
                current = target_module
                canonical_parts = [target_module_name]
            else:
                # Unknown prefix — fall through to normal navigation (will likely fail)
                parts = raw_parts
                current = self.module
                canonical_parts = [module_name]
        else:
            parts = raw_parts
            current = self.module
            canonical_parts = [module_name]

        for segment in parts:
            segment_local = self._normalize_identifier(segment)
            matched = None

            # If the segment has a foreign prefix, try switching to that module's
            # schema tree before searching children.  This handles paths like
            # /openconfig-if-ip/oc-if:interfaces/oc-if:interface/... where
            # oc-if:interfaces belongs to openconfig-interfaces, not the current module.
            if ':' in segment:
                seg_prefix = segment.split(':', 1)[0]
                seg_module = self._resolve_module_for_prefix(seg_prefix)
                if seg_module is not None and seg_module is not current:
                    # The segment belongs to a different module — search its children
                    for child in self._iter_expanded_children(seg_module):
                        child_name = PyangStatementHelper.get_argument(child)
                        if child_name and self._normalize_identifier(child_name) == segment_local:
                            matched = child
                            seg_module_name = PyangStatementHelper.get_argument(seg_module)
                            if seg_module_name and (not canonical_parts or canonical_parts[-1] != seg_module_name):
                                canonical_parts.append(seg_module_name)
                            canonical_parts.append(child_name)
                            break

            if matched is None:
                for child in self._iter_expanded_children(current):
                    child_name = PyangStatementHelper.get_argument(child)
                    if child_name and self._normalize_identifier(child_name) == segment_local:
                        matched = child
                        canonical_parts.append(child_name)
                        break

            if matched is None:
                parent_name = PyangStatementHelper.get_argument(current) or '<anonymous>'
                return None, None, f"Node '{segment}' not found under parent '{parent_name}'"

            current = matched

        canonical_path = '/' + '/'.join(canonical_parts)
        return canonical_path, current, None

    def _resolve_absolute_path_via_node_cache(self, absolute_path: str) -> Tuple[Optional[str], Optional[statements.Statement], Optional[str]]:
        """
        Resolve an absolute path using the raw node_cache as a fallback.

        This handles cases where the expanded schema tree (i_children) does not
        include nodes that are only reachable via top-level ``uses`` statements
        (e.g. ``uses terminal-device-top`` at the module level).  The raw AST
        cache built by ``_build_node_cache`` recurses into ``uses`` and therefore
        contains these nodes.

        The path is normalised (all namespace prefixes stripped) before lookup so
        that paths like ``/oc-opt-term:terminal-device/oc-opt-term:logical-channels/…``
        match cache keys like ``/openconfig-terminal-device/terminal-device/logical-channels/…``.

        Returns:
            (canonical_path, target_node, error)
        """
        module_name = PyangStatementHelper.get_argument(self.module)
        if not module_name:
            return None, None, 'Module name is unavailable'

        # Strip all namespace prefixes from the incoming path so we can compare
        # against the prefix-free keys stored in node_cache.
        raw_parts = [p for p in absolute_path.split('/') if p]
        if not raw_parts:
            return None, None, 'Empty absolute XPath'

        # Drop the first segment if it is the module root (with or without prefix).
        first_local = self._normalize_identifier(raw_parts[0])
        if first_local == module_name:
            local_parts = raw_parts[1:]
        else:
            # The first segment may be a prefixed data node (e.g. oc-opt-term:terminal-device).
            # Keep all segments; they will be stripped below.
            local_parts = raw_parts

        # Build the prefix-free lookup key: /module_name/seg1/seg2/…
        stripped = [self._normalize_identifier(p) for p in local_parts]
        lookup_key = '/' + module_name + ('/' + '/'.join(stripped) if stripped else '')

        node = self.node_cache.get(lookup_key)
        if node is not None:
            return lookup_key, node, None

        return None, None, f"Path '{lookup_key}' not found in raw node cache"

    def _resolve_relative_tail_globally(self, forward_path: str) -> Tuple[Optional[str], Optional[statements.Statement], Optional[str]]:
        """
        Resolve a relative tail (without '../' prefix) against expanded schema cache.

        This is a conservative fallback for grouping-like contexts where a relative
        XPath may navigate above the reported context depth. The method succeeds
        only when exactly one expanded schema path ends with the requested tail.

        Malformed cache entries — paths that contain ``..`` segments (produced
        when pyang stores leafref ``path`` statement values as pseudo-children
        in ``i_children``) — are excluded from the candidate set so they do not
        inflate the match count and cause spurious "ambiguous" failures.

        Returns:
            (canonical_path, target_node, error)
        """
        tail = '/'.join([self._normalize_identifier(p) for p in forward_path.split('/') if p])
        if not tail:
            return None, None, 'Relative XPath tail is empty'

        suffix = '/' + tail
        candidates = [
            (path, node)
            for path, node in self.expanded_node_cache.items()
            if path.endswith(suffix) and not self._is_malformed_absolute_path(path)
        ]

        if len(candidates) == 1:
            return candidates[0][0], candidates[0][1], None

        if not candidates:
            return None, None, f"No expanded schema node matches tail '{tail}'"

        return None, None, f"Ambiguous relative tail '{tail}' matched {len(candidates)} nodes"
    
    def find_grouping_instantiation_contexts(self, grouping_name: str) -> List[str]:
        """
        Find all instantiated schema paths where a grouping is used.

        When a `when`/`must` is inside a grouping, the context path extracted from
        the report is a grouping-level path (e.g. /module/my-grouping/container/leaf).
        Relative XPaths that navigate above the grouping root fail to resolve because
        the grouping itself has no parent in the schema tree.

        This method finds all ``uses my-grouping`` statements in the schema tree and
        returns the instantiated parent paths — i.e., the paths where the grouping
        content is actually placed.  It handles **nested grouping chains**: if
        ``uses my-grouping`` appears inside another ``grouping X``, the method
        recursively finds all instantiation points of ``grouping X`` and prepends
        the data-node path from the ``grouping X`` root to the ``uses`` site.  A
        visited set prevents infinite recursion for mutually-referencing groupings.

        Args:
            grouping_name: The grouping name (without module prefix)

        Returns:
            List of absolute schema paths where the grouping is instantiated
        """
        # Build a map: grouping_name → list of (parent_grouping_name_or_None, path_within_parent)
        # where path_within_parent is the data-node path from the grouping root to the uses site.
        # We do a single pass over the raw AST to collect all uses relationships.
        #
        # We also traverse included submodules: when the parent module was loaded via
        # submodule promotion, the submodule's raw `substmts` are not part of the parent
        # module's raw AST, but the submodule statements are accessible via the pyang
        # context's module registry.  We collect `uses` from all submodules that belong
        # to the current module so that groupings defined and used in submodules are
        # correctly resolved.

        # Map: grouping_name → list of (enclosing_grouping_name | None, data_path_to_uses_site)
        # enclosing_grouping_name is None when the uses is directly in the module (not in a grouping).
        uses_map: Dict[str, List[tuple]] = {}  # target_grouping → [(enclosing_grouping, path_prefix)]

        # Collect the set of statements to traverse: the main module + all included submodules.
        stmts_to_traverse: List[statements.Statement] = [self.module]
        try:
            ctx = self.pyang_ctx.ctx
            module_name = PyangStatementHelper.get_argument(self.module)
            for (_mod_name, _rev), mod_stmt in ctx.modules.items():
                if PyangStatementHelper.get_keyword(mod_stmt) == 'submodule':
                    # Check if this submodule belongs to our module via belongs-to
                    for child in PyangStatementHelper.get_substmts(mod_stmt):
                        if PyangStatementHelper.get_keyword(child) == 'belongs-to':
                            if PyangStatementHelper.get_argument(child) == module_name:
                                stmts_to_traverse.append(mod_stmt)
                                break
        except Exception:
            pass  # Fall back to main module only

        def _collect_uses(stmt: statements.Statement, current_path: str, enclosing_grouping: Optional[str]):
            keyword = PyangStatementHelper.get_keyword(stmt)
            arg = PyangStatementHelper.get_argument(stmt)

            if keyword in {'module', 'submodule'}:
                node_path = f'/{arg}' if arg else ''
                enc = enclosing_grouping
            elif keyword == 'grouping':
                # Enter a new grouping scope; reset path to empty (grouping-relative)
                node_path = ''
                enc = arg  # now inside this grouping
            elif keyword in {'container', 'list', 'leaf', 'leaf-list', 'choice', 'case',
                             'input', 'output', 'rpc', 'action', 'notification'}:
                node_path = f'{current_path}/{arg}' if arg else current_path
                enc = enclosing_grouping
            else:
                node_path = current_path
                enc = enclosing_grouping

            if keyword == 'uses' and arg:
                local_name = arg.split(':', 1)[-1] if ':' in arg else arg
                if local_name not in uses_map:
                    uses_map[local_name] = []
                uses_map[local_name].append((enclosing_grouping, current_path))

            for child in PyangStatementHelper.get_substmts(stmt):
                _collect_uses(child, node_path, enc)

        for stmt_root in stmts_to_traverse:
            _collect_uses(stmt_root, '', None)

        # Now resolve instantiation paths for grouping_name recursively.
        # resolved_paths accumulates the final absolute instantiated paths.
        resolved_paths: List[str] = []
        visited: set = set()
        module_name = PyangStatementHelper.get_argument(self.module) or ''

        def _resolve(gname: str, suffix: str):
            """Recursively resolve all instantiation paths for grouping gname,
            appending suffix (data-node path within the grouping) to each."""
            if gname in visited:
                return
            visited.add(gname)

            entries = uses_map.get(gname, [])
            if entries:
                for (enclosing, path_prefix) in entries:
                    # Full path from the enclosing grouping root to the uses site + suffix
                    combined = path_prefix + suffix if suffix else path_prefix

                    if enclosing is None:
                        # Direct instantiation in the module — combined is already absolute
                        resolved_paths.append(combined)
                    else:
                        # Nested: uses gname is inside grouping `enclosing`.
                        # Recursively find where `enclosing` is instantiated, appending combined.
                        _resolve(enclosing, combined)
            else:
                # The grouping is not used via `uses` anywhere in this module's raw AST.
                # This happens when the grouping is consumed by an external module (e.g.
                # openconfig-system uses openconfig-aaa:aaa-top).  Fall back to the
                # expanded node cache: pyang stores the grouping's content under
                # /<module>/<grouping-name>/... in i_children.
                #
                # The suffix already encodes the data-node path from the grouping root
                # to the target (e.g. '/aaa/server-groups/server-group' for aaa-top).
                # We simply prepend the grouping-level prefix to produce an absolute path.
                # This path is grouping-relative (not truly instantiated), but it is
                # sufficient for XPath resolution since the resolver's _canonicalize_context_path
                # will strip the grouping name and find the correct instantiated path.
                grouping_prefix = f'/{module_name}/{gname}'
                # Only emit one entry: the grouping root + suffix
                resolved_paths.append(grouping_prefix + suffix)

            visited.discard(gname)

        _resolve(grouping_name, '')
        return resolved_paths

    def resolve_xpath_from_grouping_context(
        self,
        grouping_context_path: str,
        relative_xpath: str
    ) -> List['ResolvedPath']:
        """
        Resolve a relative XPath from a grouping context by finding all instantiation
        points and resolving from each.

        When the context path is a grouping-level path (e.g.
        /module/my-grouping/container/leaf), relative XPaths that navigate above the
        grouping root cannot be resolved directly. This method:
        1. Extracts the grouping name from the context path
        2. Finds all `uses my-grouping` instantiation points
        3. Constructs instantiated context paths by replacing the grouping prefix
        4. Resolves the XPath from each instantiated context

        Args:
            grouping_context_path: Grouping-level context path from the report
            relative_xpath: Relative XPath to resolve

        Returns:
            List of ResolvedPath objects (one per instantiation point)
        """
        results: List[ResolvedPath] = []

        # Extract grouping name: second component of the path
        # e.g. /openconfig-ospfv2/ospfv2-lsdb-area-lsa-state/lsas/lsa/state
        #       → grouping = 'ospfv2-lsdb-area-lsa-state'
        #       → suffix = '/lsas/lsa/state'
        parts = [p for p in grouping_context_path.split('/') if p]
        if len(parts) < 2:
            return results

        # parts[0] = module name, parts[1] = grouping name (or first container)
        # Try each component as a potential grouping name
        for grouping_idx in range(1, len(parts)):
            grouping_name = parts[grouping_idx]
            suffix = '/' + '/'.join(parts[grouping_idx + 1:]) if grouping_idx + 1 < len(parts) else ''

            instantiation_paths = self.find_grouping_instantiation_contexts(grouping_name)
            if not instantiation_paths:
                continue

            for inst_path in instantiation_paths:
                # Construct instantiated context path
                instantiated_context = inst_path + suffix
                resolved = self.resolve_xpath(instantiated_context, relative_xpath)
                if resolved.success:
                    results.append(resolved)

            if results:
                break  # Found valid instantiation points

        return results

    def find_context_node(self, context_path: str) -> Optional[statements.Statement]:
        """
        Find the pyang statement for a given schema path.
        
        Args:
            context_path: Absolute schema path (e.g., /module/container/list)
            
        Returns:
            Pyang statement or None if not found
        """
        target = self.expanded_node_cache.get(context_path)
        if target is not None:
            return target

        canonical_path, target, _ = self._resolve_expanded_absolute_path(context_path)
        if target is not None and canonical_path is not None:
            self.expanded_node_cache[canonical_path] = target
            return target

        return self.node_cache.get(context_path)
    
    def resolve_xpath(self, context_path: str, relative_xpath: str, dummy_context: bool = False) -> ResolvedPath:
        """
        Resolve a relative or absolute XPath expression from a context node.
        
        Args:
            context_path: Absolute path to context node (where XPath statement is attached)
            relative_xpath: XPath expression — either relative (e.g., "../../config/type")
                            or absolute (e.g., "/mpls/lsps/constrained-path/tunnel/config/type")
            
        Returns:
            ResolvedPath object with resolution result
        """
        # ── Pyang-first resolution for leafref path statements ────────────────
        # Use pyang's own validation result for any XPath that matches a known
        # leafref 'path' value in this module.
        #
        # _leafref_path_stmt_map maps leafref path strings → their pyang 'path'
        # statement.  _leafref_error_positions holds (file, line) pairs for all
        # paths that pyang flagged as LEAFREF_IDENTIFIER_NOT_FOUND.
        #
        #   • If the xpath is in _leafref_path_stmt_map AND its 'path' statement
        #     position is in _leafref_error_positions
        #     → return success=False immediately (authoritative invalidity).
        #
        #   • If the xpath is in _leafref_path_stmt_map AND pyang has no error
        #     for it → the path is structurally valid.  Try to get i_target_node
        #     from the leaf node for a precise absolute path; if not available
        #     (grouping-defined leafref), fall through to normal navigation.
        #
        #   • If the xpath is NOT in _leafref_path_stmt_map → not a leafref path
        #     in this module; proceed with normal resolution below.
        xpath_stripped = relative_xpath.strip()
        path_stmt = self._leafref_path_stmt_map.get(xpath_stripped)
        if path_stmt is not None:
            if self._leafref_path_stmt_is_invalid(path_stmt):
                # pyang explicitly flagged this path as unresolvable
                return ResolvedPath(
                    absolute_path="",
                    context_path=context_path,
                    relative_xpath=relative_xpath,
                    success=False,
                    error=(
                        f"pyang reported LEAFREF_IDENTIFIER_NOT_FOUND for "
                        f"'{relative_xpath}' — path is invalid in this schema"
                    ),
                )
            # Path is valid per pyang.  Try to get i_target_node for a precise
            # absolute path (works for instantiated leafrefs).
            leaf_node = self.find_context_node(context_path)
            if leaf_node is None:
                leaf_node = self.node_cache.get(context_path)
            if leaf_node is not None:
                pyang_result = self._resolve_leafref_via_pyang(
                    leaf_node, context_path, relative_xpath
                )
                # None sentinel → grouping leafref without i_target_node;
                # fall through to normal schema-tree navigation below.
                if pyang_result is not None:
                    return pyang_result
            # No leaf node found or grouping leafref → fall through to normal
            # navigation which will resolve via the instantiated context.

        # Resolve from an instantiated data-tree context when possible.
        effective_context = self._canonicalize_context_path(context_path)

        # Parse the XPath to extract navigation and target
        # Pattern: (../)* followed by path components
        parent_steps = 0
        forward_path, parse_error = self._extract_structural_path(effective_context, relative_xpath)
        if not forward_path:
            return ResolvedPath(
                absolute_path="",
                context_path=context_path,
                relative_xpath=relative_xpath,
                success=False,
                error=parse_error or 'Could not extract path from XPath expression'
            )

        # Handle absolute XPath (starts with '/')
        if forward_path.startswith('/'):
            if self._is_malformed_absolute_path(forward_path):
                return ResolvedPath(
                    absolute_path=forward_path,
                    context_path=context_path,
                    relative_xpath=relative_xpath,
                    target_node=None,
                    success=False,
                    error='Absolute XPath contains invalid dot-segments ("." or "..")'
                )

            # Use the original xpath_stripped (with namespace prefixes intact) for
            # resolution so that _resolve_expanded_absolute_path can look up the
            # correct imported module when the first segment has a foreign prefix
            # (e.g. /ocif:interfaces/... where ocif refers to openconfig-interfaces).
            # _extract_structural_path strips prefixes, losing the module information.
            path_for_resolution = xpath_stripped if xpath_stripped.startswith('/') else forward_path
            # Strip XPath predicates (e.g. [name=current()/../../../../set-name]) from
            # the path before schema-tree navigation.  Predicates are irrelevant for
            # node lookup — only the structural node names matter.  We use the
            # XPathFunctionParser._strip_predicates() implementation which correctly
            # handles nested brackets and quoted strings.
            if self._function_parser is not None:
                path_for_resolution = self._function_parser._strip_predicates(path_for_resolution)
            canonical_path, target_node, error = self._resolve_expanded_absolute_path(path_for_resolution)
            if target_node is None or canonical_path is None:
                # Fallback: try the raw node_cache which includes nodes reachable only
                # via top-level `uses` statements that pyang may not expand in i_children.
                # This also handles namespace-prefixed absolute paths such as
                # /oc-opt-term:terminal-device/oc-opt-term:logical-channels/… where the
                # expanded tree traversal fails because the first segment's local name
                # (e.g. "terminal-device") does not match the module name
                # (e.g. "openconfig-terminal-device").
                cache_path, cache_node, cache_error = self._resolve_absolute_path_via_node_cache(forward_path)
                if cache_node is not None and cache_path is not None:
                    return ResolvedPath(
                        absolute_path=cache_path,
                        context_path=context_path,
                        relative_xpath=relative_xpath,
                        target_node=cache_node,
                        success=True
                    )
                return ResolvedPath(
                    absolute_path=forward_path,
                    context_path=context_path,
                    relative_xpath=relative_xpath,
                    target_node=None,
                    success=False,
                    error=error or 'Absolute XPath could not be resolved in expanded schema tree'
                )

            return ResolvedPath(
                absolute_path=canonical_path,
                context_path=context_path,
                relative_xpath=relative_xpath,
                target_node=target_node,
                success=True
            )
        
        # Count parent navigation steps (..)
        while forward_path.startswith('../'):
            parent_steps += 1
            forward_path = forward_path[3:]  # Remove '../'
        
        # Handle single dot (current context)
        if forward_path == '.':
            forward_path = ''
        elif forward_path.startswith('./'):
            forward_path = forward_path[2:]  # Remove './'

        # Rule 3 (RFC 7950, when under data definition): context is a dummy node
        # with no children. Child navigation from the context itself is invalid.
        # Parent navigation remains valid.
        if dummy_context and parent_steps == 0 and forward_path:
            return ResolvedPath(
                absolute_path="",
                context_path=context_path,
                relative_xpath=relative_xpath,
                success=False,
                error='Dummy context has no children; relative child navigation is invalid'
            )

        # We only support parent navigation as a leading '../' chain.
        # Any remaining '.' or '..' segment is structurally invalid here.
        if forward_path:
            forward_parts = [p for p in forward_path.split('/') if p]
            if any(seg in {'.', '..'} for seg in forward_parts):
                return ResolvedPath(
                    absolute_path="",
                    context_path=context_path,
                    relative_xpath=relative_xpath,
                    success=False,
                    error='Relative XPath contains invalid internal dot-segments'
                )
        
        # Start from context path and navigate up
        path_parts = [p for p in effective_context.split('/') if p]
        
        # Navigate up parent_steps times
        if parent_steps > len(path_parts) - 1:  # -1 because we keep module name
            # Fallback for grouping-like contexts where report path depth is
            # shallower than the runtime-instantiated depth. Resolve by unique
            # forward-tail match only; if ambiguous, remain unresolved.
            tail_path, tail_node, tail_error = self._resolve_relative_tail_globally(forward_path)
            if tail_path is not None and tail_node is not None:
                return ResolvedPath(
                    absolute_path=tail_path,
                    context_path=context_path,
                    relative_xpath=relative_xpath,
                    target_node=tail_node,
                    success=True
                )

            return ResolvedPath(
                absolute_path="",
                context_path=context_path,
                relative_xpath=relative_xpath,
                success=False,
                error=(
                    f"XPath navigates {parent_steps} levels up but only {len(path_parts)-1} levels available"
                    + (f"; fallback tail resolution failed: {tail_error}" if tail_error else '')
                )
            )
        
        # Remove parent_steps from end of path
        if parent_steps > 0:
            path_parts = path_parts[:-parent_steps]
        
        # Add forward path components
        if forward_path:
            path_parts.extend(forward_parts)
        
        # Build absolute path
        absolute_path = '/' + '/'.join(path_parts)

        canonical_path, target_node, error = self._resolve_expanded_absolute_path(absolute_path)
        if target_node is None or canonical_path is None:
            # Secondary fallback for relative paths in grouping-like contexts:
            # when direct context-based resolution misses, try unique global
            # suffix resolution of the forward tail.
            tail_path = None
            tail_node = None
            tail_error = None
            if parent_steps > 0 or (forward_path and not relative_xpath.strip().startswith('/')):
                tail_path, tail_node, tail_error = self._resolve_relative_tail_globally(forward_path)
                if tail_path is not None and tail_node is not None:
                    return ResolvedPath(
                        absolute_path=tail_path,
                        context_path=context_path,
                        relative_xpath=relative_xpath,
                        target_node=tail_node,
                        success=True
                    )

            cache_path, cache_node, cache_error = self._resolve_absolute_path_via_node_cache(absolute_path)
            if cache_node is not None and cache_path is not None:
                return ResolvedPath(
                    absolute_path=cache_path,
                    context_path=context_path,
                    relative_xpath=relative_xpath,
                    target_node=cache_node,
                    success=True
                )

            return ResolvedPath(
                absolute_path=absolute_path,
                context_path=context_path,
                relative_xpath=relative_xpath,
                target_node=None,
                success=False,
                error=error or cache_error or tail_error or 'Resolved path is not present in expanded schema tree'
            )

        return ResolvedPath(
            absolute_path=canonical_path,
            context_path=context_path,
            relative_xpath=relative_xpath,
            target_node=target_node,
            success=True
        )

    def resolve_xpath_in_data_context(self, context_path: str, xpath_expr: str) -> List[ResolvedPath]:
        """
        Resolve XPath from a possibly-grouping context to instantiated data paths.

        Returns a list of successful resolutions. If direct resolution succeeds,
        the list contains one item. If direct resolution fails, grouping
        instantiation contexts are tried.
        """
        direct = self.resolve_xpath(context_path, xpath_expr)
        if direct.success:
            return [direct]

        inst_results = self.resolve_xpath_from_grouping_context(context_path, xpath_expr)
        return [r for r in inst_results if r.success]
    
    def _resolve_leafref_via_pyang(
        self,
        leaf_node: statements.Statement,
        context_path: str,
        leafref_path_val: str,
    ) -> 'ResolvedPath':
        """
        Resolve a leafref path using pyang's own validation result.

        Strategy (purely pyang-cache-based, no heuristics):

        1. Find the ``type leafref`` sub-statement and its ``path`` child.
        2. Check if pyang flagged this ``path`` statement as
           ``LEAFREF_IDENTIFIER_NOT_FOUND`` (via ``_leafref_path_stmt_is_invalid``).
           If yes → return ``success=False`` immediately.
        3. If pyang set ``i_target_node`` on the ``i_type_spec`` (happens for
           leafrefs instantiated outside groupings) → use it directly.
        4. Otherwise (leafref inside a grouping — pyang resolves at instantiation
           time, not at grouping definition time) → the absence of ``i_target_node``
           does NOT mean the path is invalid.  Fall through to the normal
           schema-tree navigation so the caller can resolve it via the
           instantiated context.

        Args:
            leaf_node:        The pyang leaf statement that carries the leafref.
            context_path:     Schema path of the leaf (used in the result).
            leafref_path_val: The raw leafref ``path`` string (for the result).

        Returns:
            A :class:`ResolvedPath` with ``success=True`` when pyang resolved
            the target or the path is structurally valid (no pyang error),
            ``success=False`` when pyang explicitly flagged the path as broken,
            or ``None`` (sentinel) when the caller should fall through to normal
            resolution (grouping case without pyang error).
        """
        for sub in getattr(leaf_node, 'substmts', []):
            if getattr(sub, 'keyword', None) == 'type' and getattr(sub, 'arg', None) == 'leafref':
                # Find the 'path' sub-statement
                path_stmt = None
                for tsub in getattr(sub, 'substmts', []):
                    if getattr(tsub, 'keyword', None) == 'path':
                        path_stmt = tsub
                        break

                # Step 2: pyang explicitly flagged this path as invalid
                if path_stmt is not None and self._leafref_path_stmt_is_invalid(path_stmt):
                    return ResolvedPath(
                        absolute_path="",
                        context_path=context_path,
                        relative_xpath=leafref_path_val,
                        success=False,
                        error=(
                            f"pyang reported LEAFREF_IDENTIFIER_NOT_FOUND for "
                            f"'{leafref_path_val}' — path is invalid in this schema"
                        ),
                    )

                # Step 3: pyang resolved the target (instantiated leafref)
                type_spec = getattr(sub, 'i_type_spec', None)
                if type_spec is not None:
                    target_node = getattr(type_spec, 'i_target_node', None)
                    if target_node is not None:
                        # Build the absolute path from the resolved target node.
                        # build_path() returns 'module/a/b' without a leading '/';
                        # normalise to '/module/a/b' to match the rest of the resolver.
                        raw_path = PyangStatementHelper.build_path(target_node, include_module=True)
                        abs_path = ('/' + raw_path) if raw_path and not raw_path.startswith('/') else raw_path
                        return ResolvedPath(
                            absolute_path=abs_path or "",
                            context_path=context_path,
                            relative_xpath=leafref_path_val,
                            target_node=target_node,
                            success=bool(abs_path),
                            error=None if abs_path else "Could not build absolute path for resolved leafref target",
                        )

                # Step 4: no pyang error, no i_target_node → grouping-defined leafref.
                # Return None as a sentinel so resolve_xpath() falls through to
                # normal schema-tree navigation from the instantiated context.
                return None  # type: ignore[return-value]

        # No 'type leafref' sub-statement found on this node
        return ResolvedPath(
            absolute_path="",
            context_path=context_path,
            relative_xpath=leafref_path_val,
            success=False,
            error="Leaf node does not have a 'type leafref' sub-statement",
        )

    def get_leafref_target_type(self, leaf_context_path: str) -> Optional[str]:
        """
        Resolve the leafref 'path' statement for a leaf node and return the
        target node's type keyword (e.g., 'string', 'int8', 'uint32').

        Uses pyang's own validated result (``i_type_spec.i_target_node``) as
        the primary source.  For grouping-instantiated leafrefs, pyang only
        sets ``i_target_node`` on the grouping *definition* node, not on the
        expanded instantiation nodes.  When ``i_target_node`` is absent on an
        instantiated node, this method falls back to:

          1. Extracting the raw leafref ``path`` value from the node's
             sub-statements.
          2. Resolving that XPath from the leaf's context via
             ``resolve_xpath_in_data_context()``.
          3. Inspecting the resolved target node's ``type`` sub-statement.

        This ensures that the same physical leafref change (e.g. leafref →
        string) receives a consistent compatibility verdict regardless of
        whether the leaf is encountered at the grouping definition path or at
        one of its grouping-instantiated paths.

        IMPORTANT: The leafref 'path' XPath is evaluated from the LEAF's
        position, not from the type node. If the context path ends in '/type'
        (our internal type node path format), strip it to get the leaf path.

        Args:
            leaf_context_path: Schema path of the leaf or its type node
                               (e.g., '/module:leaf-name' or
                               '/module/grouping/container/leaf/type').

        Returns:
            The target node's type keyword (e.g., 'string', 'int8') or None
            if the leafref path cannot be resolved or the leaf is not a leafref.
        """
        try:
            # Strip type-related suffixes so we arrive at the leaf node path.
            #
            # The context_path passed from check_compatibility.py has the form:
            #   <leaf-path>/type/<type-name>   e.g. .../required-module/type/string
            # or occasionally just:
            #   <leaf-path>/type
            #
            # We need to strip everything from '/type' onward to get the leaf path.
            actual_leaf_path = leaf_context_path
            # Strip '/type/<typename>' or '/type' suffix
            type_idx = actual_leaf_path.rfind('/type')
            if type_idx != -1:
                actual_leaf_path = actual_leaf_path[:type_idx]
            # Ensure the path has a leading '/' — node_cache keys always start
            # with '/' but callers may omit it (e.g. when the path comes from
            # the report line without a leading slash).
            if actual_leaf_path and not actual_leaf_path.startswith('/'):
                actual_leaf_path = '/' + actual_leaf_path
            leaf_node = self.find_context_node(actual_leaf_path)
            if leaf_node is None:
                # The instantiated path is not in the node/expanded cache.
                # This happens for grouping-expanded paths (e.g.
                # /module/catalog-module-dependency-top/dependencies/required-module)
                # that pyang only stores under the grouping definition path
                # (e.g. /module/catalog-module-dependency-config/required-module).
                #
                # Fallback: search node_cache for nodes whose path ends with
                # the same leaf name AND that carry a 'type leafref' substmt.
                # Collect ALL candidates and resolve their target types.
                # Only use the result when all candidates agree on the same
                # target type — if they differ the match is ambiguous and we
                # return None (conservative) to avoid a false classification.
                leaf_name = actual_leaf_path.rstrip('/').rsplit('/', 1)[-1]
                if leaf_name:
                    suffix = '/' + leaf_name
                    candidate_nodes = []
                    for cached_path, cached_node in self.node_cache.items():
                        if not cached_path.endswith(suffix):
                            continue
                        for sub in getattr(cached_node, 'substmts', []):
                            if (getattr(sub, 'keyword', None) == 'type'
                                    and getattr(sub, 'arg', None) == 'leafref'):
                                candidate_nodes.append(cached_node)
                                break

                    if len(candidate_nodes) == 1:
                        # Unambiguous — use the single match.
                        leaf_node = candidate_nodes[0]
                    elif len(candidate_nodes) > 1:
                        # Multiple candidates: resolve each and check consensus.
                        # Helper to get target type from a candidate node.
                        def _target_type_from_node(n):
                            for s in getattr(n, 'substmts', []):
                                if getattr(s, 'keyword', None) == 'type' and getattr(s, 'arg', None) == 'leafref':
                                    ts = getattr(s, 'i_type_spec', None)
                                    if ts is None:
                                        return None
                                    tn = getattr(ts, 'i_target_node', None)
                                    if tn is None:
                                        return None
                                    for tts in getattr(tn, 'substmts', []):
                                        if getattr(tts, 'keyword', None) == 'type':
                                            return getattr(tts, 'arg', None)
                            return None

                        candidate_types = [_target_type_from_node(n) for n in candidate_nodes]
                        non_none = [t for t in candidate_types if t is not None]
                        if non_none and len(set(non_none)) == 1:
                            # All resolved candidates agree — safe to use.
                            leaf_node = candidate_nodes[candidate_types.index(non_none[0])]
                        # else: ambiguous or all unresolved — leave leaf_node as None

                if leaf_node is None:
                    return None

            # Use pyang's own validated result via i_type_spec.i_target_node.
            for sub in getattr(leaf_node, 'substmts', []):
                if getattr(sub, 'keyword', None) == 'type' and getattr(sub, 'arg', None) == 'leafref':
                    type_spec = getattr(sub, 'i_type_spec', None)
                    if type_spec is None:
                        return None
                    target_node = getattr(type_spec, 'i_target_node', None)
                    if target_node is not None:
                        # Primary path: pyang resolved the target directly
                        # (works for leafrefs outside groupings).
                        for tsub in getattr(target_node, 'substmts', []):
                            if getattr(tsub, 'keyword', None) == 'type':
                                return getattr(tsub, 'arg', None)
                        return None

                    # Fallback for grouping-instantiated leafrefs:
                    # pyang resolves leafrefs at grouping *definition* time and
                    # sets i_target_node only on the definition node, not on
                    # expanded instantiation nodes.  Extract the raw 'path'
                    # value and resolve it via the schema-tree navigator so
                    # that all instantiated paths get the same result as the
                    # grouping definition path.
                    leafref_path_val: Optional[str] = None
                    for tsub in getattr(sub, 'substmts', []):
                        if getattr(tsub, 'keyword', None) == 'path':
                            leafref_path_val = getattr(tsub, 'arg', None)
                            break

                    if leafref_path_val is None:
                        return None

                    # Check whether pyang explicitly flagged this path as
                    # invalid (LEAFREF_IDENTIFIER_NOT_FOUND).  If so, do not
                    # attempt resolution — the path is broken.
                    path_stmt = self._leafref_path_stmt_map.get(leafref_path_val.strip())
                    if path_stmt is not None and self._leafref_path_stmt_is_invalid(path_stmt):
                        return None

                    # Resolve the leafref XPath from the leaf's context.
                    # resolve_xpath_in_data_context() tries direct resolution
                    # first, then falls back to grouping instantiation contexts.
                    resolved_list = self.resolve_xpath_in_data_context(
                        actual_leaf_path, leafref_path_val
                    )
                    if not resolved_list:
                        return None

                    # Use the first successful resolution to find the target
                    # node's type.  All instantiation points should resolve to
                    # the same target type for a well-formed grouping.
                    resolved_target = resolved_list[0].target_node
                    if resolved_target is None:
                        # Try to look up the resolved absolute path in the
                        # expanded node cache as a last resort.
                        abs_path = resolved_list[0].absolute_path
                        if abs_path:
                            resolved_target = self.expanded_node_cache.get(abs_path)
                    if resolved_target is None:
                        return None

                    for tsub in getattr(resolved_target, 'substmts', []):
                        if getattr(tsub, 'keyword', None) == 'type':
                            return getattr(tsub, 'arg', None)
                    return None
            return None
        except Exception:
            return None

    def get_node_info(self, stmt: statements.Statement) -> Dict[str, str]:
        """
        Get human-readable info about a pyang statement node.
        
        Args:
            stmt: Pyang statement
            
        Returns:
            Dictionary with node information
        """
        return {
            'keyword': PyangStatementHelper.get_keyword(stmt),
            'name': PyangStatementHelper.get_argument(stmt) or '<anonymous>',
            'path': PyangStatementHelper.build_path(stmt, include_module=True)
        }


def compare_xpath_resolutions(
    old_yang_file: str,
    new_yang_file: str,
    context_path: str,
    old_xpath: str,
    new_xpath: str,
    search_dirs: Optional[List[str]] = None
) -> Tuple[ResolvedPath, ResolvedPath, bool, str]:
    """
    Compare two XPath expressions across old and new YANG versions.
    
    Args:
        old_yang_file: Path to old YANG file
        new_yang_file: Path to new YANG file
        context_path: Schema path where when/must is attached (e.g., /module/container/tacacs)
        old_xpath: Old XPath expression
        new_xpath: New XPath expression
        search_dirs: Additional search directories for imports/includes
        
    Returns:
        Tuple of (old_resolved, new_resolved, same_target, explanation)
    """
    try:
        # Resolve in old version
        old_resolver = XPathResolver(old_yang_file, search_dirs)
        old_resolved = old_resolver.resolve_xpath(context_path, old_xpath)
        
        # Resolve in new version
        new_resolver = XPathResolver(new_yang_file, search_dirs)
        new_resolved = new_resolver.resolve_xpath(context_path, new_xpath)
        
        # Check if both resolved successfully
        if not old_resolved.success:
            return old_resolved, new_resolved, False, f"Failed to resolve old XPath: {old_resolved.error}"
        
        if not new_resolved.success:
            return old_resolved, new_resolved, False, f"Failed to resolve new XPath: {new_resolved.error}"
        
        # Compare resolved paths
        same_target = old_resolved.absolute_path == new_resolved.absolute_path
        
        if same_target:
            explanation = f"Both XPaths resolve to same node: {old_resolved.absolute_path}"
        else:
            explanation = (
                f"XPaths resolve to DIFFERENT nodes:\n"
                f"  Old: {old_xpath} → {old_resolved.absolute_path}\n"
                f"  New: {new_xpath} → {new_resolved.absolute_path}\n"
                f"  This is a BREAKING CHANGE (different semantic meaning)"
            )
        
        return old_resolved, new_resolved, same_target, explanation
        
    except Exception as e:
        error_msg = f"Error during XPath resolution: {str(e)}"
        dummy_resolved = ResolvedPath(
            absolute_path="",
            context_path=context_path,
            relative_xpath="",
            success=False,
            error=error_msg
        )
        return dummy_resolved, dummy_resolved, False, error_msg


def analyze_xpath_change_in_report_line(
    report_line: str,
    old_yang_file: str,
    new_yang_file: str,
    search_dirs: Optional[List[str]] = None
) -> Optional[Tuple[bool, str]]:
    """
    Analyze an XPath change from a report line.
    
    Parses lines like:
        "5.4 constraint changed: when '../../config/type' -> '../../config/type' -> /aaa/server-groups/server-group/servers/server/tacacs"
    
    Args:
        report_line: Line from compatibility report
        old_yang_file: Path to old YANG file
        new_yang_file: Path to new YANG file
        search_dirs: Additional search directories
        
    Returns:
        Tuple of (same_target: bool, explanation: str) or None if line doesn't contain XPath change
    """
    xpath_statement_keywords = {'when', 'must', 'path', 'leafref'}
    pattern = r"constraint\s+(?:changed|added|deleted):\s+([a-zA-Z][a-zA-Z0-9_-]*)\s+'([^']+)'\s*->\s*'([^']+)'\s*->\s*(.+)"
    match = re.search(pattern, report_line)
    
    if not match:
        return None

    statement_type = match.group(1).strip().lower()
    if statement_type not in xpath_statement_keywords:
        return None

    old_xpath = match.group(2).strip()
    new_xpath = match.group(3).strip()
    context_path = match.group(4).strip()
    
    # Perform resolution
    _, _, same_target, explanation = compare_xpath_resolutions(
        old_yang_file=old_yang_file,
        new_yang_file=new_yang_file,
        context_path=context_path,
        old_xpath=old_xpath,
        new_xpath=new_xpath,
        search_dirs=search_dirs
    )
    
    return same_target, explanation


# Example usage and testing
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 6:
        print("Usage: python xpath_resolver.py <old_yang_file> <new_yang_file> <context_path> <old_xpath> <new_xpath>")
        print()
        print("Example:")
        print("  python xpath_resolver.py \\")
        print("    old/openconfig-aaa.yang \\")
        print("    new/openconfig-aaa.yang \\")
        print("    /openconfig-aaa/aaa/server-groups/server-group/servers/server/tacacs \\")
        print("    \"../../../config/type = 'oc-aaa:TACACS'\" \\")
        print("    \"../../config/type = 'oc-aaa:TACACS'\"")
        sys.exit(1)
    
    old_file = sys.argv[1]
    new_file = sys.argv[2]
    context = sys.argv[3]
    cli_old_xpath = sys.argv[4]
    cli_new_xpath = sys.argv[5]
    
    print("Resolving XPath expressions...")
    print(f"  Old file: {old_file}")
    print(f"  New file: {new_file}")
    print(f"  Context: {context}")
    print(f"  Old XPath: {cli_old_xpath}")
    print(f"  New XPath: {cli_new_xpath}")
    print()
    
    cli_old_resolved, cli_new_resolved, cli_same_target, cli_explanation = compare_xpath_resolutions(
        old_yang_file=old_file,
        new_yang_file=new_file,
        context_path=context,
        old_xpath=cli_old_xpath,
        new_xpath=cli_new_xpath
    )
    
    print("Resolution Results:")
    print(f"  Old: {cli_old_xpath}")
    print(f"    -> {cli_old_resolved.absolute_path}")
    print(f"    Success: {cli_old_resolved.success}")
    if cli_old_resolved.error:
        print(f"    Error: {cli_old_resolved.error}")
    print()
    
    print(f"  New: {cli_new_xpath}")
    print(f"    -> {cli_new_resolved.absolute_path}")
    print(f"    Success: {cli_new_resolved.success}")
    if cli_new_resolved.error:
        print(f"    Error: {cli_new_resolved.error}")
    print()
    
    print("Comparison:")
    print(f"  Same target: {cli_same_target}")
    print(f"  {cli_explanation}")
