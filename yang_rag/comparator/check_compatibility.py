"""Refactored compatibility enrichment script.

Features:
 - Clean, single-pass logic (no unreachable branches from legacy version).
 - Unified subject parsing (keywords, attributes, constraints) with regex helpers.
 - Constraint evaluation supports: explicit value, from/to transitions, relaxed/narrowed flags.
 - Fallback relaxed/narrowed determination ensures every constraint change is tagged.
 - Conditional rules resolved by recursively inspecting children.
 - YANG type compatibility checking for type changes.
"""

import re
import xml.etree.ElementTree as ET
from typing import Optional, Tuple, List, Dict, Any

try:  # support running as script or package
    from constraint_change import parse_old_new_from_line, classify_constraint_change
    from yang_type_checker import check_type_compatibility, CompatibilityResult, clear_registries, resolve_typedef
    from typedef_loader import load_typedefs_from_file, load_identities_from_file
    from xpath_resolver import get_cached_xpath_resolver
except ImportError:  # pragma: no cover
    from .helper.constraint_change import parse_old_new_from_line, classify_constraint_change  # type: ignore
    from .helper.yang_type_checker import check_type_compatibility, CompatibilityResult, clear_registries, resolve_typedef  # type: ignore
    from .helper.typedef_loader import load_typedefs_from_file, load_identities_from_file  # type: ignore
    from .helper.xpath_resolver import get_cached_xpath_resolver  # type: ignore

try:
    from .core.constants import SYMBOLIC_KEYWORDS as _SYMBOLIC_KEYWORDS
except ImportError:
    try:
        from yang_rag.comparator.core.constants import SYMBOLIC_KEYWORDS as _SYMBOLIC_KEYWORDS  # type: ignore
    except ImportError:
        _SYMBOLIC_KEYWORDS = {'enum', 'bit'}  # fallback if running as standalone script

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _extract_context_path(lines: List[str], current_idx: int) -> Optional[str]:
    """
    Extract schema path from preceding report lines for use as XPath resolution context.
    
    Looks backward from current line to find the parent node path.
    
    Args:
        lines: All report lines
        current_idx: Index of current line being processed
        
    Returns:
        Schema path string or None if extraction fails
    """
    # Look backward for the parent path (lines that don't start with space and contain path info)
    for i in range(current_idx - 1, max(-1, current_idx - 15), -1):
        if i < 0:
            break
        raw_line = lines[i]

        # Path header lines are non-indented. Skip indented metadata/detail lines.
        if not raw_line.strip() or raw_line.startswith((' ', '\t')):
            continue

        line = raw_line.strip()
        
        # Remove module prefix if present (e.g., "openconfig-acl:" from
        # "openconfig-acl:acl/acl-sets/...").
        #
        # IMPORTANT: keep the FULL node path from the report line.
        # The report line already points to the node that owns the changed
        # constraint/attribute. Dropping the last segment shifts context to the
        # parent and mis-resolves relative XPaths (e.g. ../config from
        # .../channel/ethernet must resolve from ethernet, not channel).
        #
        # This handles both "module:path" and "path" formats.
        if ':' in line:
            module_name, schema_path = line.split(':', 1)
            schema_path = schema_path.rstrip('/')
            if schema_path:
                result = f"/{module_name}:{schema_path}"
                return result
            # If parsing failed, continue to next line
            continue
        else:
            # No module prefix in report line - use the full path as context.
            parts = line.rstrip('/').split('/')
            if len(parts) > 1:
                # Try to identify if first part looks like a container/list (no hyphens usually)
                # or a module name (usually has hyphens)
                first_part = parts[0]
                
                # If first part has NO hyphens and length < 20, it's likely a root container, not a module
                if '-' not in first_part or len(first_part) > 20:
                    # Treat as schema path without module.
                    result = '/' + '/'.join(parts)
                    return result
                else:
                    # First part looks like it might be a module (has hyphens)
                    # Construct path with module prefix
                    if len(parts) > 1:
                        child_parts = parts[1:]
                        result = f"/{first_part}:{'/'.join(child_parts)}"
                        return result
            
            # Line didn't produce valid path, continue to next line
            continue
    
    return None


def _extract_file_from_report_lines(lines: List[str], current_idx: int) -> Optional[str]:
    """
    Extract the YANG file name from the nearest parent report line metadata.

    Report lines for type/keyword changes carry a ``[file: <name>.yang, ...]``
    annotation, e.g.::

        5. (type changed) [file: old-path.yang, old_line: 94, ...]

    When the XPath resolver is loaded with the *main* module (e.g.
    ``vpn-services-old.yang``) it may not have submodule nodes in its cache.
    Using the actual file that contains the changed node (e.g. ``old-path.yang``)
    gives the resolver the correct scope.

    Looks backward from ``current_idx`` through up to 20 lines to find the
    nearest indented parent line that contains ``file: <name>.yang``.

    Args:
        lines: All report lines.
        current_idx: Index of the current line being processed.

    Returns:
        The bare filename (e.g. ``'old-path.yang'``) or ``None`` if not found.
    """
    import re as _re
    _file_re = _re.compile(r'\bfile:\s*([\w\-\.]+\.yang)\b')
    for i in range(current_idx - 1, max(-1, current_idx - 20), -1):
        if i < 0:
            break
        raw = lines[i]
        m = _file_re.search(raw)
        if m:
            return m.group(1)
    return None


def _resolve_yang_file_from_metadata(
    bare_filename: Optional[str],
    search_dirs: Optional[List[str]],
) -> Optional[str]:
    """
    Given a bare YANG filename (e.g. ``'old-path.yang'``) and a list of
    search directories, return the first full path where the file exists.

    Args:
        bare_filename: Filename without directory (e.g. ``'old-path.yang'``).
        search_dirs: Directories to search (old_dir first, then new_dir).

    Returns:
        Full path string or ``None`` if not found.
    """
    if not bare_filename or not search_dirs:
        return None
    import os as _os
    for d in search_dirs:
        candidate = _os.path.join(d, bare_filename)
        if _os.path.isfile(candidate):
            return candidate
    return None


def _resolve_xpath(xpath: str, context_path: str) -> str:
    """
    Resolve an XPath expression (absolute or relative) to its absolute form.
    
    This works for any XPath-like path expression including:
    - leafref path attributes
    - when statement XPath expressions
    - must statement XPath expressions
    
    Args:
        xpath: The XPath expression (e.g., "../../../../next-hop-groups/..." or "/network-instances/...")
        context_path: The context schema path where the XPath is evaluated
        
    Returns:
        Absolute path string
    """
    # If already absolute (starts with /), return as-is
    if xpath.startswith('/'):
        return xpath
    
    # Otherwise, it's relative - resolve by going up from context
    # Split context path into parts
    context_parts = [p for p in context_path.split('/') if p]
    
    # Count "../" occurrences
    up_count = 0
    remaining = xpath
    while remaining.startswith('../'):
        up_count += 1
        remaining = remaining[3:]  # Remove "../"
    
    # Go up from context (remove last N parts)
    if up_count > len(context_parts):
        # The relative path navigates above the grouping root (context is a
        # shallow grouping-level path, not the full instantiated schema path).
        # In this case the forward tail IS the path from the schema root —
        # return it as an absolute path so Phase 2 can compare it directly
        # with the other (absolute) path.
        if remaining:
            return '/' + remaining
        return xpath  # No forward tail — cannot resolve
    
    base_parts = context_parts[:-up_count] if up_count > 0 else context_parts
    
    # Append the remaining path
    if remaining:
        result_parts = base_parts + [p for p in remaining.split('/') if p]
    else:
        result_parts = base_parts
    
    # Return as absolute path
    return '/' + '/'.join(result_parts)


def _strip_all_ns_prefixes(path_str: str) -> str:
    """Strip namespace prefixes from ALL segments of an XPath-like path.

    e.g. '/ocif:interfaces/ocif:interface/ocif:state'
         → '/interfaces/interface/state'
    e.g. '/oc-if:interfaces/oc-if:interface/oc-if:state'
         → '/interfaces/interface/state'

    NOTE: This is a heuristic — it cannot distinguish between different
    modules that happen to share the same local node names.  Use the
    XPathResolver (which uses pyang + YANG files) for authoritative checks.
    """
    return '/'.join(
        seg.split(':', 1)[1] if ':' in seg else seg
        for seg in path_str.split('/')
    )


def _resolve_prefix_to_module(prefix: str, yang_file: Optional[str],
                               search_dirs: Optional[List[str]] = None) -> Optional[str]:
    """Resolve a namespace prefix to its YANG module name using pyang.

    Args:
        prefix: The namespace prefix (e.g. 'oc-if', 'ocif').
        yang_file: Path to the YANG file that imports/uses the prefix.
        search_dirs: Additional directories to search for imported modules.

    Returns:
        The module name the prefix maps to, or None if resolution fails.
    """
    if not yang_file or not prefix:
        return None
    try:
        import pyang
        from pyang import context as pyang_ctx, repository as pyang_repo
        repos = pyang_repo.FileRepository(*(search_dirs or []))
        ctx = pyang_ctx.Context(repos)
        with open(yang_file, 'r', encoding='utf-8') as f:
            src = f.read()
        module = ctx.add_module(yang_file, src)
        if module is None:
            return None
        # Look up the prefix in the module's imports
        for imp in module.search('import'):
            imp_prefix = imp.search_one('prefix')
            if imp_prefix and imp_prefix.arg == prefix:
                return imp.arg  # module name
        # Check if it's the module's own prefix
        own_prefix = module.search_one('prefix')
        if own_prefix and own_prefix.arg == prefix:
            return module.arg
        return None
    except Exception:
        return None


def _are_paths_equivalent(
    old_path: str,
    new_path: str,
    context_path: Optional[str],
    old_yang_file: Optional[str] = None,
    new_yang_file: Optional[str] = None,
    search_dirs: Optional[List[str]] = None,
) -> bool:
    """
    Check if two XPath/leafref path expressions resolve to the same schema node.

    Two-phase algorithm:

    Phase 1 — Prefix resolution:
      Normalise both paths to prefix-free (local-name only) form by resolving
      any namespace prefixes to their module names and then stripping them.
      Three sub-cases:
        a) Both paths have prefixes → resolve each prefix via pyang to its
           module name; if resolution succeeds for both, compare the resulting
           module-qualified paths.  If equal → prefix phase resolved (continue
           to Phase 2 with normalised paths).  If different → return False.
        b) One path has prefixes, the other does not → resolve the prefixed
           side; strip prefixes from both; compare local-name-only paths.
           If equal → prefix phase resolved.  If different → return False.
        c) Neither path has prefixes → prefix phase resolved immediately
           (nothing to normalise).

    Phase 2 — Path comparison:
      Compare the prefix-normalised paths structurally.
        a) If both are absolute → compare directly.
        b) If one is relative → resolve it to absolute using the context_path
           (via _resolve_xpath), then compare with the other (absolute) path.
        c) If both are relative → resolve both to absolute, then compare.
      If equal → return True.  Else → return False.

    Note: The LLM double-check (when --llm-verify is active) is handled
    upstream via the assistance="true" flag in the XML rules; this function
    only returns a boolean equivalence result.

    Args:
        old_path: Old path expression (may be relative or absolute, with or
                  without namespace prefixes).
        new_path: New path expression (same).
        context_path: Schema path of the node where the expression is
                      evaluated.  Required for resolving relative paths.
        old_yang_file: Path to old YANG file (for prefix→module resolution).
        new_yang_file: Path to new YANG file (for prefix→module resolution).
        search_dirs: Directories to search for imported YANG modules.

    Returns:
        True if both paths resolve to the same absolute schema location.
    """
    # Fast path: identical strings → trivially equivalent
    if old_path == new_path:
        return True

    # ── Phase 1: Prefix resolution ────────────────────────────────────────────
    #
    # Paths may contain XPath predicates such as
    #   /oc-acl:acl-set[oc-acl:name=current()/../../../../set-name]/...
    # Splitting on '/' breaks predicate content (current()/ inside [...]).
    # We therefore work on the *structural* path (predicates stripped) for
    # prefix detection and resolution, then apply the same normalisation to
    # the full path using a regex-based substitution that is predicate-safe.

    import re as _re_compat

    def _strip_predicates_for_prefix(path_str: str) -> str:
        """Strip [...] predicate blocks from a path for prefix-detection purposes.

        Uses a simple bracket-depth counter so nested predicates are handled
        correctly.  The result is the structural path without any predicates.
        """
        result = []
        depth = 0
        for ch in path_str:
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
            elif depth == 0:
                result.append(ch)
        return ''.join(result)

    def _has_prefixes(path_str: str) -> bool:
        """Return True if any *structural* segment of path_str has a namespace prefix.

        Predicates are stripped first so that 'current()' inside [...] does not
        trigger a false positive (current() contains no prefix, but predicate
        content like 'oc-acl:name=...' would if not stripped).
        """
        structural = _strip_predicates_for_prefix(path_str)
        return any(':' in seg for seg in structural.split('/') if seg)

    def _resolve_prefixes_in_path(path_str: str, yang_file: Optional[str]) -> Optional[str]:
        """
        Replace each prefixed segment 'pfx:local' with 'module:local' using
        pyang to map pfx → module name.  Returns None if any prefix cannot
        be resolved (signals that the caller should fall back).

        Operates on the *structural* path (predicates stripped) for the
        resolution logic, then applies the same prefix→module substitution
        to the full path (including predicates) using a regex so that
        predicate attribute prefixes are also expanded correctly.
        """
        if not yang_file:
            return None

        # Build a prefix→module map from the structural segments first.
        structural = _strip_predicates_for_prefix(path_str)
        prefix_to_module: dict = {}
        for seg in structural.split('/'):
            if seg and ':' in seg:
                prefix = seg.split(':', 1)[0]
                if prefix not in prefix_to_module:
                    module_name = _resolve_prefix_to_module(prefix, yang_file, search_dirs)
                    if module_name is None:
                        return None  # Cannot resolve → abort
                    prefix_to_module[prefix] = module_name

        if not prefix_to_module:
            # No prefixes found in structural path — return path unchanged
            return path_str

        # Apply the prefix→module substitution to the full path (including
        # predicates) using a regex so that predicate attribute prefixes like
        # [oc-acl:name=...] are also expanded.
        def _replace(m):
            pfx = m.group(1)
            local = m.group(2)
            mod = prefix_to_module.get(pfx)
            if mod:
                return f'{mod}:{local}'
            # Unknown prefix in predicate — try to resolve it on the fly
            mod2 = _resolve_prefix_to_module(pfx, yang_file, search_dirs)
            if mod2:
                prefix_to_module[pfx] = mod2
                return f'{mod2}:{local}'
            return m.group(0)  # Leave unchanged if unresolvable

        return _re_compat.sub(
            r'\b([a-zA-Z][a-zA-Z0-9_-]*):([a-zA-Z][a-zA-Z0-9_-]*)',
            _replace,
            path_str
        )

    old_has_pfx = _has_prefixes(old_path)
    new_has_pfx = _has_prefixes(new_path)

    # Normalised (prefix-free local-name) versions used in Phase 2.
    # Use regex-based stripping so predicate content is handled correctly
    # (simple split('/') breaks on 'current()/' inside predicates).
    def _strip_prefixes_safe(path_str: str) -> str:
        """Strip all 'prefix:' tokens from path_str using regex (predicate-safe)."""
        return _re_compat.sub(
            r'\b[a-zA-Z][a-zA-Z0-9_-]*:([a-zA-Z][a-zA-Z0-9_-]*)',
            r'\1',
            path_str
        )

    old_norm: str = _strip_prefixes_safe(old_path)
    new_norm: str = _strip_prefixes_safe(new_path)

    if old_has_pfx or new_has_pfx:
        if old_has_pfx and new_has_pfx:
            # Sub-case (a): both have prefixes → resolve to module-qualified form
            old_mod = _resolve_prefixes_in_path(old_path, old_yang_file or new_yang_file)
            new_mod = _resolve_prefixes_in_path(new_path, new_yang_file or old_yang_file)
            if old_mod is not None and new_mod is not None:
                # Strip the *current module's own name* from both sides so that
                # 'openconfig-acl:acl' (new) and 'acl' (old, no prefix) compare
                # as equal.  Cross-module references keep their module qualifier.
                old_self = _strip_prefixes_safe(old_mod)
                new_self = _strip_prefixes_safe(new_mod)
                if old_self != new_self:
                    return False  # Different local-name paths → not equivalent
                # Same local-name path → prefix phase resolved; update norms
                old_norm = old_self
                new_norm = new_self
            # else: resolution failed → fall through with stripped local names
        else:
            # Sub-case (b): one side has prefixes, the other does not
            # Resolve the prefixed side and compare local names
            if old_has_pfx:
                old_mod = _resolve_prefixes_in_path(old_path, old_yang_file or new_yang_file)
                if old_mod is not None:
                    old_norm = _strip_prefixes_safe(old_mod)
            else:
                new_mod = _resolve_prefixes_in_path(new_path, new_yang_file or old_yang_file)
                if new_mod is not None:
                    new_norm = _strip_prefixes_safe(new_mod)
            # After normalisation, if local-name paths differ → not equivalent
            # (checked in Phase 2 below)
    # Sub-case (c): neither has prefixes → old_norm/new_norm already set correctly

    # ── Phase 2: Path comparison via XPathResolver ───────────────────────────
    # When YANG files are available, use the pyang-backed XPathResolver to
    # resolve both paths (relative or absolute) to canonical absolute schema
    # paths and compare them.  This is the authoritative check — it uses the
    # full schema tree including grouping instantiation points, so it correctly
    # handles relative paths that navigate above the grouping root.
    #
    # Fallback: when YANG files are not available, use the simple _resolve_xpath
    # heuristic (context-path string manipulation).

    def _resolve_to_canonical(path_str: str, yang_file: Optional[str]) -> Optional[str]:
        """
        Resolve path_str to a canonical absolute schema path using XPathResolver.
        Returns the normalised (prefix-stripped) absolute path, or None on failure.

        Three strategies (in order):
        1. Direct XPath resolution from context_path via expanded schema tree.
        2. Grouping-context instantiation: find where the grouping is used in the
           data tree and resolve from each instantiation point.
        3. Over-navigation tail resolution: when a relative path navigates above
           the grouping root (more `../` steps than context depth), the forward
           tail IS the path from the schema root.  This is valid YANG semantics —
           leafref paths in groupings are evaluated at the instantiation point,
           and over-navigation in a grouping context means the tail is an absolute
           path from the module root.  Resolve the tail via the node_cache.
        """
        if not yang_file or not context_path:
            return None
        try:
            resolver = get_cached_xpath_resolver(yang_file, search_dirs)
            resolved = resolver.resolve_xpath(context_path, path_str)
            if resolved.success and resolved.absolute_path:
                return _strip_all_ns_prefixes(resolved.absolute_path)
            # Direct resolution failed — try grouping-context instantiation
            inst_results = resolver.resolve_xpath_from_grouping_context(
                context_path, path_str
            )
            if inst_results:
                # Collect all successfully resolved absolute paths
                abs_paths = {
                    _strip_all_ns_prefixes(r.absolute_path)
                    for r in inst_results if r.success and r.absolute_path
                }
                if len(abs_paths) == 1:
                    return abs_paths.pop()
                # Multiple instantiation points — return None (ambiguous)

            # Strategy 3: over-navigation tail resolution.
            # When a relative path has more `../` steps than the context depth,
            # resolve_xpath returns an error like "XPath navigates N levels up
            # but only M levels available".  In this case the forward tail of the
            # path (everything after the `../` chain) is the effective absolute
            # path from the schema root — this is valid YANG semantics because
            # leafref paths in groupings are evaluated at the instantiation point.
            # Search the expanded_node_cache for any instantiated path whose
            # suffix matches the tail, then return it if unambiguous.
            if (not resolved.success and
                    resolved.error and
                    'levels up but only' in resolved.error and
                    not path_str.startswith('/')):
                # Extract the forward tail (strip all leading `../`)
                tail = path_str.strip()
                while tail.startswith('../'):
                    tail = tail[3:]
                if tail:
                    tail_suffix = '/' + tail  # e.g. '/logical-channels/channel/index'
                    # Search expanded_node_cache for data-node paths ending with this
                    # suffix.  Exclude paths that contain '/leafref/' — those are
                    # leafref path-statement values stored as cache keys, not actual
                    # schema nodes.
                    matches = [
                        k for k in resolver.expanded_node_cache
                        if k.endswith(tail_suffix) and '/leafref/' not in k
                    ]
                    if len(matches) == 1:
                        return _strip_all_ns_prefixes(matches[0])
                    # Also try node_cache as fallback
                    cache_path, cache_node, _ = resolver._resolve_absolute_path_via_node_cache(tail_suffix)
                    if cache_node is not None and cache_path is not None:
                        return _strip_all_ns_prefixes(cache_path)
        except Exception:
            pass
        return None

    old_canonical = _resolve_to_canonical(old_path, old_yang_file)
    new_canonical = _resolve_to_canonical(new_path, new_yang_file)

    if old_canonical is not None and new_canonical is not None:
        # Both resolved via pyang — structural node comparison.
        # _resolve_to_canonical strips predicates before schema navigation, so
        # two paths that differ ONLY in their predicates (e.g. one has an extra
        # [type=current()/.../type] filter) will produce the same canonical node
        # path.  We must therefore also compare the normalised predicate content:
        # if the predicates differ, the paths are NOT equivalent even though they
        # point to the same schema node (the predicate changes the set of matching
        # instances, which is a semantic change).
        if old_canonical != new_canonical:
            return False
        # Structural nodes match — now compare predicate content.
        # old_norm and new_norm are already prefix-stripped full paths (computed
        # above).  If they differ, the predicate content differs → not equivalent.
        return old_norm == new_norm

    # ── Fallback: simple string-based resolution ──────────────────────────────
    # Used when YANG files are unavailable or XPathResolver fails.
    # Convert relative paths to absolute via context-path string manipulation,
    # then compare the normalised (prefix-stripped) absolute paths.

    def _to_absolute_simple(path_str: str, norm_str: str) -> str:
        """Simple absolute-path resolver using context-path string manipulation."""
        if path_str.startswith('/'):
            return norm_str  # Already absolute; norm_str has prefixes stripped
        if context_path:
            resolved = _resolve_xpath(path_str, context_path)
            if resolved != path_str:  # Resolution changed the string → succeeded
                return _strip_all_ns_prefixes(resolved)
        return norm_str  # Fallback: use prefix-stripped form as-is

    old_abs = _to_absolute_simple(old_path, old_norm)
    new_abs = _to_absolute_simple(new_path, new_norm)

    return old_abs == new_abs


# ---------------------------------------------------------------------------
# Parent-matching helper
# ---------------------------------------------------------------------------

def _parent_matches(rule_parent: Any, parent_keyword: Optional[str]) -> bool:
    """Return True if *parent_keyword* matches the rule's parent specification.

    The ``parent`` field in a rule definition may be:
    - A list of strings (after parse_rules normalises pipe-separated values),
      e.g. ``['augment', 'deviation']`` from ``parent="augment|deviation"``.
    - A plain string (legacy single-value), e.g. ``'type'``.
    - ``None`` / absent (no parent restriction → always matches).

    Args:
        rule_parent: The ``parent`` value from the rule definition dict.
        parent_keyword: The parent keyword extracted from the report context.

    Returns:
        True if the rule applies to the current parent keyword.
    """
    if not rule_parent:
        # No parent restriction → matches any parent
        return True
    if isinstance(rule_parent, list):
        return parent_keyword in rule_parent
    # Legacy string value
    return parent_keyword == rule_parent


# ---------------------------------------------------------------------------
# Type checking helpers
# ---------------------------------------------------------------------------

def _handle_type_attribute_check(
    matched_attr_def: Dict[str, Any],
    action: str,
    parent_keyword: Optional[str],
    rule: Dict[str, Any],
    old_new_func: callable,
    old_yang_file: Optional[str] = None,
    new_yang_file: Optional[str] = None,
    context_path: Optional[str] = None,
    search_dirs: Optional[List[str]] = None,
    lines: Optional[List[str]] = None,
    line_index: Optional[int] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Generic handler for type-related attribute checks (identical, relaxed, condition, etc.)
    
    This consolidates the logic for checking type compatibility attributes to avoid
    code duplication. Supports:
    - identical="true": Check if types resolve to structurally identical definitions
    - relaxed="true": Check if type change is compatible (value space relaxing)
    - relaxed="false": Check if type change is incompatible (value space narrowing)
    - type="condition": Treat the attribute value as an XPath/path expression and
                        run it through the ConditionAnalyzer (same as when/must).
                        Used for leafref 'path' attribute changes — the path is an
                        XPath expression pointing to a schema node; if it resolves
                        to the same node the change is BC, otherwise NBC.
    
    Args:
        matched_attr_def: The attribute definition dict with metadata
        action: The action being performed (added/changed/deleted)
        parent_keyword: The parent keyword context (e.g., 'type')
        rule: The rule dict containing compatibility info
        old_new_func: Function to get (old_value, new_value) tuple
        old_yang_file: Path to old YANG file (for XPath/leafref resolution)
        new_yang_file: Path to new YANG file (for XPath/leafref resolution)
        context_path: Schema path where the attribute is attached
        search_dirs: Directories to search for imported YANG modules
        
    Returns:
        Tuple of (should_return, compatibility_result)
        - should_return=True means we found a match and should return the compatibility
        - should_return=False means continue checking other rules
        - compatibility_result is the compatibility string or None
    """
    # Only apply to type name attributes with changed action.
    # Use _parent_matches to handle both list and string parent values.
    attr_parent = matched_attr_def.get('parent')
    if not _parent_matches(attr_parent, parent_keyword) or parent_keyword != 'type' or action != 'changed':
        return False, None
    
    # Get old and new values
    o, n = old_new_func()
    if not (o and n):
        return False, None

    # ── type="condition": treat attribute value as XPath/path expression ──────
    # This handles leafref 'path' attribute changes using the same ConditionAnalyzer
    # pipeline as when/must constraints.  The path is an XPath expression pointing
    # to a schema node; if it resolves to the same node → BC, otherwise → NBC.
    # NOTE: We build a synthetic entry dict with 'name' and 'type' keys so that
    # classify_constraint_change correctly identifies it as a condition and routes
    # it through the ConditionAnalyzer.  The attribute is NOT a constraint in YANG
    # terms, but its value IS an XPath expression that requires semantic analysis.
    if matched_attr_def.get('type') == 'condition':
        attr_name = matched_attr_def.get('name', 'path')
        condition_entry = {'name': attr_name, 'type': 'condition'}
        cls = classify_constraint_change(
            condition_entry,
            str(o),
            str(n),
            old_yang_file=old_yang_file,
            new_yang_file=new_yang_file,
            context_path=context_path,
            search_dirs=search_dirs,
        )
        if cls == 'needs_llm_analysis':
            # Use the rule's own compatibility verdict (BC or NBC) and append
            # needs-deep-analysis so the LLM layer reviews the semantic direction.
            # Do NOT hardcode non-backward-compatible: the static ConditionAnalyzer
            # may have already determined a preliminary direction (e.g. relaxed → BC).
            return True, f"{rule['compatible']} needs-deep-analysis"
        if cls == 'unchanged':
            # Paths resolve to the same schema node → backward-compatible
            return True, 'backward-compatible'
        if cls in ('narrowed', 'not_equivalent', 'unknown'):
            # 'narrowed'      → constraint value space narrowed (NBC)
            # 'not_equivalent' → XPath resolves to a different schema node (NBC)
            # 'unknown'       → cannot determine → conservative NBC
            return True, rule['compatible']  # NBC per rule
        if cls == 'relaxed':
            return True, 'backward-compatible'
        # Fallback: return rule's default compatibility
        return True, rule['compatible']
    
    # Check for 'identical' attribute (highest priority - most specific)
    # This checks if types resolve to the exact same definition
    if 'identical' in matched_attr_def and matched_attr_def.get('identical') == 'true':
        # Resolve both types to their final form
        old_resolved = resolve_typedef(str(o))
        new_resolved = resolve_typedef(str(n))
        
        # Compare resolved type descriptors for structural identity
        if _types_are_identical(old_resolved, new_resolved):
            # Types resolve to identical definitions - not a real change
            return True, rule['compatible']
        # Not identical, continue to next rule
        return False, None
    
    # Check for 'relaxed' attribute (compatibility checking with value space comparison)
    if 'relaxed' in matched_attr_def:
        relaxed_attr = matched_attr_def.get('relaxed')

        # When old type is 'leafref', use check_leafref_compatibility to determine
        # if the new type is compatible with the leafref's resolved target type.
        # e.g. leafref(target=int8) → int32 is COMPATIBLE (int32 is a superset of int8)
        # e.g. leafref(target=int8) → string is COMPATIBLE (string is a superset)
        # The rule's 'compatible' value is used for the result so that rfc7950
        # mode differences are respected (type-change-1-rule: BC default, NBC rfc7950).
        old_base = str(o).split(':')[-1].lower()
        new_base = str(n).split(':')[-1].lower()
        if old_base == 'leafref' or new_base == 'leafref':
            try:
                from yang_rag.comparator.helper.check_leafref import (
                    check_leafref_compatibility as _clr_h,
                    resolve_leafref_target as _rlt_h,
                )
                from yang_rag.comparator.helper.yang_type_checker import (
                    CompatibilityResult as _CR_h,
                )
                _old_d_h: Dict[str, Any] = {'kind': old_base, 'base': old_base}
                _new_d_h: Dict[str, Any] = {'kind': new_base, 'base': new_base}
                # Extract the actual submodule file from report metadata for fallback resolution.
                _meta_file_h = _extract_file_from_report_lines(lines, line_index) if lines and line_index is not None else None
                if old_base == 'leafref' and context_path and old_yang_file:
                    _lctx_h = context_path.rsplit('/type', 1)[0] if context_path.endswith('/type') else context_path
                    _tgt_h = _rlt_h(_lctx_h, old_yang_file, search_dirs)
                    if not _tgt_h and _meta_file_h:
                        _old_actual_h = _resolve_yang_file_from_metadata(
                            _meta_file_h,
                            [search_dirs[0]] if search_dirs else search_dirs,
                        )
                        if _old_actual_h:
                            _tgt_h = _rlt_h(_lctx_h, _old_actual_h, search_dirs)
                    if _tgt_h:
                        _old_d_h['target_type'] = {'base': _tgt_h, 'kind': _tgt_h}
                if new_base == 'leafref' and context_path and new_yang_file:
                    _lctx_h = context_path.rsplit('/type', 1)[0] if context_path.endswith('/type') else context_path
                    _tgt_h = _rlt_h(_lctx_h, new_yang_file, search_dirs)
                    if not _tgt_h and _meta_file_h:
                        _new_actual_h = _resolve_yang_file_from_metadata(
                            _meta_file_h,
                            [search_dirs[1]] if search_dirs and len(search_dirs) > 1 else search_dirs,
                        )
                        if _new_actual_h:
                            _tgt_h = _rlt_h(_lctx_h, _new_actual_h, search_dirs)
                    if _tgt_h:
                        _new_d_h['target_type'] = {'base': _tgt_h, 'kind': _tgt_h}
                _lr_c_h, _ = _clr_h(
                    old=_old_d_h, new=_new_d_h,
                    old_yang_file=old_yang_file,
                    new_yang_file=new_yang_file,
                    context_path=context_path,
                    search_dirs=search_dirs,
                )
                if _lr_c_h is not None:
                    if _lr_c_h == _CR_h.COMPATIBLE:
                        if relaxed_attr == 'true':
                            # Use rule's compatible value (respects rfc7950 flag)
                            return True, rule['compatible']
                        # For relaxed="false", COMPATIBLE means skip this rule
                        return False, None
                    elif _lr_c_h == _CR_h.INCOMPATIBLE:
                        if relaxed_attr == 'false':
                            return True, rule['compatible']
                        # For relaxed="true", INCOMPATIBLE means skip this rule
                        return False, None
            except Exception:
                pass

        # Perform type compatibility check
        type_compat, _ = check_type_compatibility(str(o), str(n))
        
        if relaxed_attr == 'true':
            # relaxed="true" means compatible type changes are OK
            # Return the compatibility from the rule which was selected based on the flag
            if type_compat == CompatibilityResult.COMPATIBLE:
                # Type is relaxed/compatible - return the rule's compatibility
                # (which is flag-aware: BC without flag, NBC with --rfc7950)
                return True, rule['compatible']
            elif type_compat == CompatibilityResult.INCOMPATIBLE:
                # Incompatible type change - skip this rule, let stricter rule (relaxed="false") handle it
                return False, None
            # For CONDITIONAL or UNKNOWN, fall through to return rule's compatibility
        elif relaxed_attr == 'false':
            # relaxed="false" means check if types are incompatible (narrowing)
            if type_compat == CompatibilityResult.INCOMPATIBLE:
                return True, rule['compatible']
            else:
                # Compatible or conditional type change - skip this rule
                return False, None
    
    return False, None


def _types_are_identical(type1: Dict[str, Any], type2: Dict[str, Any]) -> bool:
    """
    Check if two resolved type descriptors are structurally identical.
    
    Two types are considered identical if they have:
    - Same kind (integer, string, union, enumeration, etc.)
    - Same base primitive type
    - Same constraints (range, length, patterns, enum values, etc.)
    
    Args:
        type1: First resolved type descriptor
        type2: Second resolved type descriptor
        
    Returns:
        True if types are structurally identical, False otherwise
    """
    # Must have same kind
    kind1 = type1.get('kind')
    kind2 = type2.get('kind')
    if kind1 != kind2:
        return False
    
    # Must have same base type
    base1 = type1.get('base')
    base2 = type2.get('base')
    
    # Special handling for typedef kind: strip module prefix before comparing
    # This handles cases like "oc-sr:sr-sid-type" vs "oc-srt:sr-sid-type"
    if kind1 == 'typedef':
        # Strip prefix from both bases for comparison
        base1_unprefixed = base1.split(':')[-1] if base1 and ':' in base1 else base1
        base2_unprefixed = base2.split(':')[-1] if base2 and ':' in base2 else base2
        if base1_unprefixed != base2_unprefixed:
            return False
    else:
        # For non-typedef kinds, compare bases directly
        if base1 != base2:
            return False
    
    # For numeric types, check range constraints
    if kind1 in ('integer', 'decimal'):
        if type1.get('min') != type2.get('min'):
            return False
        if type1.get('max') != type2.get('max'):
            return False
        if type1.get('fraction-digits') != type2.get('fraction-digits'):
            return False
    
    # For string types, check length and patterns
    if kind1 == 'string':
        if type1.get('min_len') != type2.get('min_len'):
            return False
        if type1.get('max_len') != type2.get('max_len'):
            return False
        # Compare patterns (order doesn't matter)
        patterns1 = set(type1.get('patterns', []))
        patterns2 = set(type2.get('patterns', []))
        if patterns1 != patterns2:
            return False
    
    # For enumeration types, check enum values
    if kind1 == 'enumeration':
        values1 = set(type1.get('values', []))
        values2 = set(type2.get('values', []))
        if values1 != values2:
            return False
    
    # For bits types, check bit values
    if kind1 == 'bits':
        bits1 = set(type1.get('bits', []))
        bits2 = set(type2.get('bits', []))
        if bits1 != bits2:
            return False
    
    # For union types, check member types recursively
    if kind1 == 'union':
        members1 = type1.get('members', [])
        members2 = type2.get('members', [])
        if len(members1) != len(members2):
            return False
        # Members must be identical in order (union order matters)
        for m1, m2 in zip(members1, members2):
            if not _types_are_identical(m1, m2):
                return False
    
    # For leafref types, check path
    if kind1 == 'leafref':
        if type1.get('path') != type2.get('path'):
            return False
    
    # For identityref types, check base identity
    if kind1 == 'identityref':
        if type1.get('identity') != type2.get('identity'):
            return False
    
    # For instance-identifier, check require-instance
    if kind1 == 'instance-identifier':
        if type1.get('require-instance') != type2.get('require-instance'):
            return False
    
    # All checks passed - types are identical
    return True


# ---------------------------------------------------------------------------
# Rules parsing
# ---------------------------------------------------------------------------

def _norm_action(a: Optional[str]) -> Optional[str]:
    if not a:
        return None
    a = a.strip().lower()
    mapping = {"add": "added", "added": "added", "delete": "deleted", "deleted": "deleted", "change": "changed", "changed": "changed"}
    return mapping.get(a, a)


def parse_rules(xml_file: str, compatibility_flag: Optional[str] = None) -> List[Dict[str, Any]]:
    """Parse compatibility rules from XML.
    
    Supports both single <compatible> and multiple <compatibles> patterns:
    - Single: <compatible>backward-compatible</compatible>
    - Multiple: <compatibles>
                  <compatible>backward-compatible</compatible>
                  <compatible flag="rfc7950">non-backward-compatible</compatible>
                  <compatible flag="strict">non-backward-compatible</compatible>
                </compatibles>
    
    Args:
        xml_file: Path to the XML rules file
        compatibility_flag: Optional flag to select specific compatibility mode (e.g., "rfc7950", "strict")
                          If provided, prefers <compatible> with matching flag attribute
    
    Returns:
        List of rule dictionaries
    """
    tree = ET.parse(xml_file)
    root = tree.getroot()
    rules: List[Dict[str, Any]] = []
    for rule in root.findall("rule"):
        # Parse attributes: keep legacy list of names AND rich defs (type, separator, parent, ...)
        attr_names: List[str] = []
        attr_defs: List[Dict[str, Any]] = []
        for a in rule.findall("attributes/attribute"):
            name = (a.text or "").strip()
            d: Dict[str, Any] = {"name": name}
            # include any extra metadata such as type, separator, parent
            for k, v in a.attrib.items():
                if k == 'parent':
                    # Normalize pipe-separated parent values to a list
                    # e.g. parent="augment|deviation" → ['augment', 'deviation']
                    # e.g. parent="type" → ['type']
                    d['parent'] = [p.strip() for p in v.split('|') if p.strip()]
                else:
                    d[k] = v
            attr_defs.append(d)

            # Only add to legacy flat list if NO parent attribute (legacy list doesn't support parent matching)
            if name and 'parent' not in a.attrib:
                attr_names.append(name)

        # Parse compatibility - support both <compatible> and <compatibles> patterns
        compatible_value = None
        
        # Check for <compatibles> block (multiple compatibility outcomes)
        compatibles_elem = rule.find("compatibles")
        if compatibles_elem is not None:
            # Find all <compatible> entries
            compatible_entries = compatibles_elem.findall("compatible")
            
            if compatibility_flag:
                # Look for entry with matching flag first
                for comp_elem in compatible_entries:
                    if comp_elem.attrib.get("flag") == compatibility_flag:
                        compatible_value = (comp_elem.text or "").strip()
                        break
            
            # If no matching flag entry found (or no flag specified), use the default (no flag)
            if compatible_value is None:
                for comp_elem in compatible_entries:
                    if "flag" not in comp_elem.attrib:
                        compatible_value = (comp_elem.text or "").strip()
                        break
                
                # If still no match, fall back to first entry
                if compatible_value is None and compatible_entries:
                    compatible_value = (compatible_entries[0].text or "").strip()
        else:
            # Check for single <compatible> tag (legacy pattern)
            compatible_value = rule.findtext("compatible")

        # Parse keywords: keep legacy list of names AND rich defs (relaxed, parent, ...)
        kw_names: List[str] = []
        kw_defs: List[Dict[str, Any]] = []
        # Updated to use 'structurals/structural' instead of 'keywords/keyword'
        for kw in rule.findall("structurals/structural"):
            name = (kw.text or "").strip()
            d: Dict[str, Any] = {"name": name}
            # include any extra metadata such as relaxed, parent
            for k, v in kw.attrib.items():
                d[k] = v
            kw_defs.append(d)
            
            # Only add to legacy flat list if NO parent attribute (legacy list doesn't support parent matching)
            if name and 'parent' not in kw.attrib:
                kw_names.append(name)

        rules.append({
            "rule_id": rule.findtext("rule-id"),
            "keywords": kw_names,  # legacy flat names for existing logic
            "keyword_defs": kw_defs,  # rich keyword definitions (relaxed, ...)
            "attributes": attr_names,  # legacy flat names for existing logic
            "attribute_defs": attr_defs,  # rich attribute definitions (type, separator, ...)
            "constraints": [dict({"name": c.text}, **{k: v for k, v in c.attrib.items()}) for c in rule.findall("constraints/constraint")],
            "actions": [{"action": _norm_action(a.text), "parent": a.attrib.get("parent")} for a in rule.findall("actions/action")],
            "compatible": compatible_value
        })
    return rules


# ---------------------------------------------------------------------------
# Line parsing helpers
# ---------------------------------------------------------------------------

# Numbering patterns now allow an optional trailing dot after the last number (e.g. "4." or "4.1.")
# Updated to handle optional metadata brackets like [line: 235] or [old_line: X, new_line: Y] [file: ...]
# Use [\w-]+ instead of \w+ to match YANG keywords with hyphens (e.g., leaf-list, anyxml, anydata)
_HEADER_PAREN_RE = re.compile(r"^(?:\s*)(?:\d+(?:\.\d+)*\.?)\s*\(([\w-]+)\s+([\w-]+)\)(?:(?:\s*\[[^\]]+\])+)?\s*$")
_HEADER_INLINE_RE = re.compile(r"^(?:\s*)(?:\d+(?:\.\d+)*\.?)\s+([\w-]+)\s+([\w-]+):")
_ATTR_CONSTR_RE = re.compile(r"^(?:\s*)(?:\d+(?:\.\d+)*\.?)\s+(attribute|constraint)\s+(added|changed|deleted):\s*\['([^']+)'\]")


def parse_subject_action(line: str) -> Tuple[Optional[str], Optional[str], Optional[str], bool]:
    """Parse a report line.

    Returns:
      (kind, name, action, is_constraint)
      kind: 'keyword' or 'field'
      name: keyword / field identifier
      action: added/changed/deleted
      is_constraint: True only for constraint field lines
    """
    m = _ATTR_CONSTR_RE.search(line)
    if m:
        kind_word, action, field_name = m.group(1), m.group(2), m.group(3)
        return 'field', field_name, action, (kind_word == 'constraint')
    m = _HEADER_PAREN_RE.search(line)
    if m:
        return 'keyword', m.group(1), m.group(2), False
    m = _HEADER_INLINE_RE.search(line)
    if m:
        return 'keyword', m.group(1), m.group(2), False
    return None, None, None, False


def _find_parent_action(lines: List[str], idx: int) -> Optional[str]:
    j = idx - 1
    while j >= 0:
        subj, _name, action, _ = parse_subject_action(lines[j].rstrip())
        if subj == 'keyword':
            return action
        j -= 1
    return None


def _find_parent_keyword(lines: List[str], idx: int) -> Optional[str]:
    """
    Extract the parent keyword name from the hierarchical structure.

    Two strategies (in order):
    1. Walk backward through preceding lines looking for a keyword-type line
       (e.g. ``2. (union changed)``).  This covers nested keyword blocks.
    2. If the immediately preceding non-blank, non-numbered line looks like a
       YANG path header (e.g. ``openconfig-policy-types/tag-type/union/foo``),
       extract the second-to-last path segment.  This covers the common case
       where a new type is added directly under a union without a preceding
       ``(union added/changed)`` keyword line at the same nesting level.

    Args:
        lines: List of all lines in the diff output
        idx: Current line index

    Returns:
        Parent keyword name (e.g., 'type', 'container', 'union', 'leaf') or None
    """
    # YANG built-in keywords that can appear as path segments
    _YANG_KEYWORDS = {
        'union', 'type', 'container', 'list', 'leaf', 'leaf-list',
        'grouping', 'typedef', 'identity', 'rpc', 'notification',
        'input', 'output', 'choice', 'case', 'action', 'anydata', 'anyxml',
        'augment', 'uses', 'import', 'include', 'extension', 'module',
        'submodule', 'revision', 'bits', 'enumeration', 'import',
    }

    j = idx - 1
    while j >= 0:
        raw = lines[j].rstrip()
        stripped = raw.strip()

        # Skip blank lines
        if not stripped:
            j -= 1
            continue

        subj, name, _action, _ = parse_subject_action(raw)
        if subj == 'keyword':
            # For 'type' keyword lines, prefer the type_name annotation when
            # present (e.g. "type_name: leafref") so that the actual type name
            # is returned as the parent keyword instead of the generic 'type'.
            # This allows the compatibility engine to correctly route type-specific
            # attribute changes (e.g. leafref 'path') through the right handler.
            if name == 'type':
                import re as _re
                # For type-change lines with old_type_name/new_type_name annotations,
                # extract the type name to use as parent keyword.
                #
                # Strategy:
                # - For attribute DELETIONS (e.g. 'path' deleted when leafref → string):
                #   use old_type_name (the attribute existed in the old type).
                # - For attribute CHANGES/ADDITIONS (e.g. 'name' changed):
                #   use type_name (same old and new) or fall back to 'type'.
                #   Do NOT use old_type_name for changes — the rule matching uses
                #   parent="type" for type-change rules (type-change-1-rule etc.)
                #   which handle rfc7950 mode differences correctly.
                #
                # The caller (determine_compatibility) passes the action of the
                # sub-item line, not the parent keyword line.  We detect deletion
                # by checking the CURRENT line (lines[idx]), not the parent line (raw).
                _current_line_str = lines[idx].strip() if 0 <= idx < len(lines) else ''
                _is_deletion = 'deleted' in _current_line_str.lower()
                if _is_deletion:
                    # For deletions, use old_type_name so leafref-path-delete-rule matches
                    _old_tn = _re.search(r'\bold_type_name:\s*(\S+)', raw)
                    if _old_tn:
                        return _old_tn.group(1).rstrip(',]')
                # For changes/additions, use type_name (same old and new type)
                _tn = _re.search(r'(?<![a-z_])type_name:\s*(\S+)', raw)
                if _tn:
                    return _tn.group(1).rstrip(',]')
            return name  # Explicit keyword block found

        # Strategy 1b: indented sub-item keyword lines (e.g. "  4. (type changed) [...]")
        # These are NOT matched by parse_subject_action (which only handles top-level lines).
        # For type-change lines with old_type_name/new_type_name annotations, extract the
        # OLD type name — this is the parent context for attribute deletions (e.g. 'path'
        # deleted when leafref → string: the parent is 'leafref', not 'string').
        if stripped and stripped[0].isdigit() and '(type' in stripped:
            import re as _re
            _current_line_str2 = lines[idx].strip() if 0 <= idx < len(lines) else ''
            _is_deletion2 = 'deleted' in _current_line_str2.lower()
            _old_tn = _re.search(r'\bold_type_name:\s*(\S+)', raw)
            _new_tn = _re.search(r'\bnew_type_name:\s*(\S+)', raw)
            if _old_tn and _new_tn:
                # Type is changing from old to new (e.g. leafref → int32).
                # For deletions: use old_type_name (attribute existed in old type).
                # For changes/additions: return 'type' so type-change rules apply
                # (not leafref-attr-change-rule which only applies within leafref).
                if _is_deletion2:
                    return _old_tn.group(1).rstrip(',]')
                else:
                    return 'type'
            elif _old_tn:
                # Only old_type_name present (deletion context)
                return _old_tn.group(1).rstrip(',]')
            # Fall back to type_name (same old and new type)
            _tn = _re.search(r'\btype_name:\s*(\S+)', raw)
            if _tn:
                return _tn.group(1).rstrip(',]')
            # Extract keyword from "(type changed)" pattern
            _kw = _re.search(r'\((\w+)\s+(?:changed|added|deleted)\)', stripped)
            if _kw:
                return _kw.group(1)

        # Strategy 2: path-header line (no leading digits/spaces numbering)
        # These look like: ``openconfig-bgp/bgp-neighbors/neighbor/union/string``
        # They are NOT matched by parse_subject_action.
        if subj is None and '/' in stripped and not stripped.startswith((' ', '\t')):
            parts = [p for p in stripped.split('/') if p]
            # The last segment is the current node; second-to-last is the direct parent
            if len(parts) >= 2:
                parent_segment = parts[-2].lower()
                # Strip any module prefix (e.g. "oc-yang:hex-string-prefixed" → "hex-string-prefixed")
                if ':' in parent_segment:
                    parent_segment = parent_segment.split(':')[-1]
                if parent_segment in _YANG_KEYWORDS:
                    return parent_segment
            break  # Path header found but parent not a known YANG keyword — stop here

        j -= 1
    return None


def check_missing_references(
    line: str,
    lines: Optional[List[str]] = None,
    line_index: Optional[int] = None,
    old_yang_file: Optional[str] = None,
    new_yang_file: Optional[str] = None,
    search_dirs: Optional[List[str]] = None,
) -> bool:
    """
    Check if a line contains references to identities, typedefs, or groupings that don't exist.
    
    This function scans constraint values (like 'when' conditions) and type attributes for
    references to identities (e.g., 'oc-aaa-types:TACACS'), typedefs, or groupings that
    aren't defined in the loaded YANG modules.
    
    Args:
        line: The report line to check
        lines: All lines from the report (for context)
        line_index: Index of current line (for finding parent context)
        
    Returns:
        True if missing references are found, False otherwise
    """
    try:
        # Import helper functions
        from yang_type_checker import identity_exists, typedef_exists
        import yang_type_checker as _ytc
    except ImportError:
        from .helper.yang_type_checker import identity_exists, typedef_exists  # type: ignore
        try:
            from .helper import yang_type_checker as _ytc  # type: ignore
        except Exception:
            _ytc = None
    
    # Extract the value part from the line
    # Pattern for constraint/attribute lines: "constraint added: ['when'] -> value"
    value_match = re.search(r'->\s*(.+?)(?:\s*<|$)', line)
    if not value_match:
        return False
    
    value = value_match.group(1).strip()
    # Ensure `type_matches` is always defined before any conditional code paths
    # reference it. Some callers or report line shapes can skip the 'attribute'
    # detection block below, so initialize an empty list here for safety.
    type_matches: List[str] = []
    
    # Pattern to find identity references: word-with-hyphens:WORD or just WORD in certain contexts
    # Identity references typically look like: module-name:IDENTITY_NAME or type = 'module:IDENTITY'
    identity_pattern = r"['\"]?([a-zA-Z0-9_-]+:[A-Z][A-Z0-9_-]*)['\"]?"
    identity_matches = re.findall(identity_pattern, value)
    
    for identity_ref in identity_matches:
        # If the identity/type registries are empty we cannot reliably assert missing refs
        # (they may not have been loaded), so only report missing when registry has entries.
        registry_present = False
        try:
            if _ytc is not None and hasattr(_ytc, '_IDENTITY_REGISTRY'):
                registry_present = len(getattr(_ytc, '_IDENTITY_REGISTRY', {})) > 0
        except Exception:
            registry_present = False

        # Now that we have attempted to detect registry presence, verify the identity
        # If identity_exists returns False and registries are present we can safely
        # report a missing reference; otherwise fall back to local YANG search.
        try:
            exists = identity_exists(identity_ref)
        except Exception:
            exists = False

        if not exists:
            if registry_present:
                # If registries are present but identity missing, do a fallback search
                if _local_search_for_symbol(identity_ref, old_yang_file, new_yang_file, search_dirs):
                    # Found in source YANG files -> consider present
                    continue
                return True
            # Registries empty or inconclusive: attempt local search before giving up
            if _local_search_for_symbol(identity_ref, old_yang_file, new_yang_file, search_dirs):
                continue
            # Be conservative and assume present if searches are inconclusive
            continue
    
    # Also check for type attribute changes that reference typedefs
    # Pattern: attribute changed: ['type/name'] -> new_type (was old_type)
    if 'attribute' in line and 'type' in line.lower():
        # Extract type names (both new and old if it's a change)
        type_pattern = r'\b([a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+)\b'
        type_matches = re.findall(type_pattern, value)

    # Ensure type_matches is defined even when the above condition is False
    if 'type_matches' not in locals():
        type_matches = []

    for type_ref in type_matches:
        # Check registries presence before asserting missing
        typedefs_present = False
        identity_regs_present = False
        try:
            if _ytc is not None:
                typedefs_present = len(getattr(_ytc, '_TYPEDEF_REGISTRY', {})) > 0
                identity_regs_present = len(getattr(_ytc, '_IDENTITY_REGISTRY', {})) > 0
        except Exception:
            typedefs_present = identity_regs_present = False

        # Check if it's an identity reference (all caps after colon)
        if ':' in type_ref:
            _, local_name = type_ref.split(':', 1)
            if local_name.isupper():
                # Likely an identity reference
                if not identity_exists(type_ref):
                    if identity_regs_present:
                        if _local_search_for_symbol(type_ref, old_yang_file, new_yang_file, search_dirs):
                            continue
                        return True
                    else:
                        if _local_search_for_symbol(type_ref, old_yang_file, new_yang_file, search_dirs):
                            continue
                        continue
            else:
                # Likely a typedef reference
                if not typedef_exists(type_ref):
                    if typedefs_present:
                        if _local_search_for_symbol(type_ref, old_yang_file, new_yang_file, search_dirs):
                            continue
                        return True
                    else:
                        if _local_search_for_symbol(type_ref, old_yang_file, new_yang_file, search_dirs):
                            continue
                        continue
    
    return False


def _local_search_for_symbol(symbol: str, old_yang_file: Optional[str], new_yang_file: Optional[str], search_dirs: Optional[List[str]] = None) -> bool:
    """
    Heuristic: search local YANG files for the occurrence of a symbol (module:NAME or NAME).
    Returns True if found.
    """
    # Normalize symbol forms
    mod_part = None
    name_part = symbol
    if ':' in symbol:
        mod_part, name_part = symbol.split(':', 1)

    files_to_search = []
    if old_yang_file:
        files_to_search.append(old_yang_file)
    if new_yang_file and new_yang_file != old_yang_file:
        files_to_search.append(new_yang_file)
    if search_dirs:
        # Add all .yang files under search_dirs (non-recursive) - keep it lightweight
        import os
        for d in search_dirs:
            try:
                for fname in os.listdir(d):
                    if fname.endswith('.yang'):
                        files_to_search.append(os.path.join(d, fname))
            except Exception:
                continue

    for f in files_to_search:
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                txt = fh.read()
                # Check fully qualified occurrence
                if mod_part and (f"{mod_part}:{name_part}" in txt or f"identity {name_part}" in txt or f"typedef {name_part}" in txt):
                    return True
                # Check unqualified occurrence
                if (f"identity {name_part}" in txt) or (f"typedef {name_part}" in txt) or (name_part in txt):
                    return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Core compatibility evaluation
# ---------------------------------------------------------------------------

def _rule_element_has_assistance(rule: Dict[str, Any], name: Optional[str], is_constraint: bool,
                                  action: Optional[str] = None,
                                  parent_keyword: Optional[str] = None) -> bool:
    """Return True if the matched element in the rule has assistance="true".

    Checks attribute_defs (for attribute/field lines) and constraints (for
    constraint lines) for the element whose name matches the parsed subject name.
    Also checks keyword_defs for keyword (structural) lines.

    Args:
        rule: The rule dictionary from parse_rules()
        name: The element name (e.g., 'when', 'path', 'pattern')
        is_constraint: True if the line is a constraint line
        action: The action from the report line (e.g., 'changed', 'added', 'deleted').
                When provided, only rules whose declared actions include this action
                are considered.  This prevents false positives where a rule with
                assistance="true" for 'changed' also triggers for 'deleted'.
        parent_keyword: The parent keyword from the report context (e.g., 'leaf', 'augment').
                When provided, attribute_defs with a 'parent' restriction are only
                matched when the parent_keyword is in the restriction list.

    This is used by determine_compatibility() to decide whether to append
    <needs-deep-analysis> when --llm-verify is active.
    """
    if not name:
        return False

    # Check if the rule's declared actions include the current action.
    # If the rule has no actions declared, it applies to all actions.
    if action:
        rule_actions = [a.get('action') for a in (rule.get('actions') or []) if isinstance(a, dict)]
        if rule_actions and action not in rule_actions:
            return False  # This rule does not apply to the current action

    if is_constraint:
        for c in rule.get('constraints', []):
            if isinstance(c, dict) and c.get('name') == name:
                if c.get('assistance', '').lower() == 'true':
                    return True
    else:
        # Check attribute_defs — also enforce parent restriction if present
        for ad in rule.get('attribute_defs', []) or []:
            if isinstance(ad, dict) and ad.get('name') == name:
                # If the rule has a parent restriction, check it matches
                if 'parent' in ad and not _parent_matches(ad['parent'], parent_keyword):
                    continue  # Parent doesn't match → skip this rule entry
                if ad.get('assistance', '').lower() == 'true':
                    return True
        # Check keyword_defs (structural elements)
        for kd in rule.get('keyword_defs', []) or []:
            if isinstance(kd, dict) and kd.get('name') == name:
                if kd.get('assistance', '').lower() == 'true':
                    return True
    return False


def determine_compatibility(
    line: str,
    rules: List[Dict[str, Any]],
    parent_action: Optional[str] = None,
    old_yang_file: Optional[str] = None,
    new_yang_file: Optional[str] = None,
    search_dirs: Optional[List[str]] = None,
    lines: Optional[str] = None,
    line_index: Optional[int] = None,
    llm_verify: bool = False,
):
    """Determine compatibility for a single report line.

    When llm_verify=True, any rule element with assistance="true" causes
    <needs-deep-analysis> to be appended to the returned compatibility string,
    regardless of the element type (constraint, attribute, or keyword).
    This is the rule-driven approach: the XML rules declare which elements
    require LLM assistance, and --llm-verify enforces that all such elements
    are always sent to the LLM for second-layer verification.
    """
    subj_kind, name, action, is_constraint = parse_subject_action(line)
    if not action:
        return None, False

    # Defensive filter: symbolic keyword attribute lines (e.g. "attribute added: ['enum'] -> [...]")
    # are noise lines that should have been suppressed by the report generator.
    # When they appear in existing reports, pass them through untagged (None) so they
    # don't interfere with compatibility decisions — the structural (enum added) lines
    # already cover the same information.
    if subj_kind == 'field' and not is_constraint and name in _SYMBOLIC_KEYWORDS:
        return None, False

    # Extract parent keyword name for hierarchical matching
    parent_keyword = _find_parent_keyword(lines, line_index) if lines and line_index is not None else None

    # Extract context path for XPath resolution (if processing constraint and have line context)
    context_path = None
    if is_constraint and lines is not None and line_index is not None:
        context_path = _extract_context_path(lines, line_index)
    # Also extract for 'path' attribute changes (for XPath/leafref path resolution)
    elif subj_kind == 'field' and name == 'path' and lines is not None and line_index is not None:
        context_path = _extract_context_path(lines, line_index)
    # Also extract for 'name' attribute changes under 'leafref' parent (type name change)
    # so that check_leafref_compatibility can resolve the leafref target type.
    elif subj_kind == 'field' and name == 'name' and lines is not None and line_index is not None:
        context_path = _extract_context_path(lines, line_index)
    # Also extract for any attribute that may have type="XPath" in the rules
    elif subj_kind == 'field' and lines is not None and line_index is not None:
        # Check if any rule declares this attribute as type="XPath"
        for _r in rules:
            for _ad in _r.get('attribute_defs', []) or []:
                if isinstance(_ad, dict) and _ad.get('name') == name and _ad.get('type') == 'XPath':
                    context_path = _extract_context_path(lines, line_index)
                    break
            if context_path:
                break

    old_new_cache: Optional[Tuple[Any, Any]] = None

    def old_new():
        nonlocal old_new_cache
        if old_new_cache is None:
            old_new_cache = parse_old_new_from_line(line, lines, line_index)
        return old_new_cache

    # ── type="XPath" attribute handler ──────────────────────────────────────────
    # When an attribute is declared with type="XPath" in the XML rules, its value
    # is an XPath path expression (e.g. leafref 'path', augment/deviation 'name').
    # The rule may also restrict which parent keywords it applies to via the
    # 'parent' attribute (pipe-separated list, e.g. parent="augment|deviation").
    #
    # Compatibility is determined by semantic XPath comparison:
    #   - semantics-preserving="true"  → BC rule (both XPaths resolve to same node)
    #   - semantics-preserving="false" → NBC rule (XPaths resolve to different nodes)
    # The actual resolution result selects which rule applies.
    # If assistance="true" and llm_verify is active, append <needs-deep-analysis>.
    if subj_kind == 'field' and not is_constraint and name and action == 'changed':
        # Collect all type="XPath" rule entries that match this attribute name AND
        # parent keyword.  Rules with no 'parent' restriction match any parent.
        # Rules with 'parent' restriction only match when parent_keyword is in the list.
        xpath_attr_entries: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for _r in rules:
            for _ad in _r.get('attribute_defs', []) or []:
                if (isinstance(_ad, dict)
                        and _ad.get('name') == name
                        and _ad.get('type') == 'XPath'
                        and 'semantics-preserving' in _ad
                        and _parent_matches(_ad.get('parent'), parent_keyword)):
                    # Check action matches
                    _rule_actions = [_a.get('action') for _a in (_r.get('actions') or []) if isinstance(_a, dict)]
                    if not _rule_actions or action in _rule_actions:
                        xpath_attr_entries.append((_r, _ad))

        if xpath_attr_entries:
            o, n = old_new()
            if o and n:
                # ── Semantic XPath comparison ─────────────────────────────────────
                # Prefix stripping is intentionally NOT used as a fallback because
                # it is semantically incorrect: different modules can share the same
                # local node names (e.g. oc-inv:components vs oc-platform:components
                # both strip to /components/... but refer to different schema trees).
                # When the resolver is unavailable, we conservatively request LLM
                # analysis rather than guess.
                if not context_path:
                    # No context → cannot resolve XPath semantically.
                    # Find a rule with assistance="true" to emit needs-deep-analysis.
                    for _r, _ad in xpath_attr_entries:
                        if _ad.get('assistance', '').lower() == 'true':
                            compat = _r.get('compatible', 'non-backward-compatible')
                            return f'{compat} needs-deep-analysis', True
                    # No assistance rule → fall through to generic matching
                else:
                    # ── Leafref 'path' attribute: use check_leafref_compatibility ──────
                    # For leafref path changes, check_leafref_compatibility correctly
                    # handles all asymmetric cases:
                    #   - old invalid, new valid  → BC (fix: broken leafref corrected)
                    #   - old valid, new invalid  → NBC (regression: leafref broken)
                    #   - both resolve same node  → BC (semantics-preserving rewrite)
                    #   - both resolve diff nodes → NBC (different target)
                    # This avoids the XPath handler's binary True/False which cannot
                    # distinguish the invalid→valid fix case from a genuine NBC change.
                    if name == 'path' and parent_keyword == 'leafref':
                        try:
                            from yang_rag.comparator.helper.check_leafref import check_leafref_compatibility, CompatibilityResult
                        except ImportError:
                            try:
                                from .helper.check_leafref import check_leafref_compatibility, CompatibilityResult
                            except ImportError:
                                check_leafref_compatibility = None  # type: ignore[assignment]
                                CompatibilityResult = None  # type: ignore[assignment]

                        if check_leafref_compatibility is not None:
                            _lr_compat, _lr_expl = check_leafref_compatibility(
                                old={'kind': 'leafref', 'path': str(o)},
                                new={'kind': 'leafref', 'path': str(n)},
                                old_yang_file=old_yang_file,
                                new_yang_file=new_yang_file,
                                context_path=context_path,
                                search_dirs=search_dirs,
                            )
                            if _lr_compat is not None and CompatibilityResult is not None:
                                if _lr_compat == CompatibilityResult.COMPATIBLE:
                                    return 'backward-compatible', False
                                else:
                                    # NBC — find the semantics-preserving="false" rule for compat string
                                    for _r, _ad in xpath_attr_entries:
                                        if _ad.get('semantics-preserving', '').lower() == 'false':
                                            compat = _r.get('compatible', 'non-backward-compatible')
                                            needs_llm = (
                                                llm_verify
                                                and _ad.get('assistance', '').lower() == 'true'
                                            )
                                            if needs_llm:
                                                return f'{compat} needs-deep-analysis', True
                                            return compat, False
                                    return 'non-backward-compatible', False
                        # Fall through to generic _are_paths_equivalent if import failed

                    paths_equivalent = _are_paths_equivalent(
                        str(o), str(n), context_path,
                        old_yang_file=old_yang_file,
                        new_yang_file=new_yang_file,
                        search_dirs=search_dirs,
                    )

                    # Select the matching rule based on semantics-preserving and resolution result
                    # semantics-preserving="true"  → applies when paths ARE equivalent (BC)
                    # semantics-preserving="false" → applies when paths are NOT equivalent (NBC)
                    matched_rule = None
                    matched_ad = None
                    for _r, _ad in xpath_attr_entries:
                        sp = _ad.get('semantics-preserving', '').lower()
                        if paths_equivalent and sp == 'true':
                            matched_rule = _r
                            matched_ad = _ad
                            break
                        elif not paths_equivalent and sp == 'false':
                            matched_rule = _r
                            matched_ad = _ad
                            break

                    if matched_rule is not None:
                        compat = matched_rule.get('compatible', 'non-backward-compatible')
                        needs_llm = (
                            llm_verify
                            and matched_ad is not None
                            and matched_ad.get('assistance', '').lower() == 'true'
                        )
                        if needs_llm:
                            return f'{compat} needs-deep-analysis', True
                        return compat, False

                    # No matching rule found (e.g. no semantics-preserving attribute declared)
                    # Fall through to generic rule matching below

    # Special handling: list-type attributes with element-wise add/delete
    # If the subject is a field (attribute) and there exists at least one rule
    # declaring this attribute as type="list", then compute per-item diff and
    # classify using the per-action rules (added/deleted).
    if subj_kind == 'field' and not is_constraint and name:
        # Collect all olist/ulist-attribute rule entries for this attribute name
        list_attr_entries: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        list_type = None  # Will be 'olist', 'ulist', or 'list' (legacy)
        for r in rules:
            for ad in r.get('attribute_defs', []) or []:
                if isinstance(ad, dict) and ad.get('name') == name:
                    attr_type = ad.get('type', '')
                    if attr_type in ('list', 'olist', 'ulist'):
                        list_attr_entries.append((r, ad))
                        if attr_type in ('olist', 'ulist'):
                            list_type = attr_type  # Prefer olist/ulist over legacy 'list'
                        elif list_type is None:
                            list_type = 'list'
        
        if list_attr_entries:
            # Collect all declared separators for this attribute across rules
            seps: List[str] = []
            for _r, ad in list_attr_entries:
                s = ad.get('separator')
                if s is not None:
                    seps.append(str(s))

            def _split_list(val: Any, separators: List[str]) -> List[str]:
                if val is None:
                    return []
                s = str(val).strip()
                if not s:
                    return []
                # Map named separators to literal characters
                named_map = {
                    'space': ' ',
                    'whitespace': ' ',
                    'comma': ',',
                    'semicolon': ';',
                    'hyphen': '-',
                    'underscore': '_',
                }
                lits: List[str] = []
                for sep in separators:
                    key = sep.strip().lower()
                    lits.append(named_map.get(key, sep))
                # If no separators declared, or empty after mapping, default to whitespace split
                if not lits:
                    parts = re.split(r"\s+", s)
                else:
                    # Build a regex that splits on any of the literal separators OR consecutive whitespace
                    esc = [re.escape(x) for x in lits if x]
                    if not esc:
                        parts = re.split(r"\s+", s)
                    else:
                        pattern = r"(?:" + r"|".join(esc) + r"|\s+)"
                        parts = re.split(pattern, s)
                return [p.strip() for p in parts if p.strip()]

            o, n = old_new()
            old_list = _split_list(o, seps)
            new_list = _split_list(n, seps)
            
            # Shared helper used by both olist and ulist handlers below.
            #
            # Scans the XML rules collected for this attribute and returns the
            # <compatible> value of the first rule whose attribute def matches the
            # observed change direction.  A rule matches when its attribute def:
            #   - has set-change="<direction>"  (explicit directional match), or
            #   - has no set-change attribute   (direction-agnostic / legacy rule).
            #
            # set-change is optional for both olist and ulist.  When present it must
            # be one of "narrowed" (items removed) or "expanded" (items added).
            # When absent the rule applies to all change directions.
            #
            # The verdict always comes from the XML rules — no hardcoded defaults.
            # Returns None only when no matching rule exists at all.
            def _find_comp_for_set_change(direction: str) -> Optional[str]:
                # Two-pass lookup so that explicit set-change rules always take
                # priority over direction-agnostic (no set-change) rules.
                #
                # Only rules whose actions include 'changed' (without a parent
                # qualifier) are considered here.  Rules with only 'added' or
                # 'deleted' actions cover the statement itself being added or
                # removed — not a change to the value of an existing statement.
                def _rule_has_changed_action(r: Dict[str, Any]) -> bool:
                    return any(
                        act.get('action') == 'changed' and act.get('parent') is None
                        for act in (r.get('actions') or [])
                    )

                # Pass 1: explicit set-change="<direction>" match on a 'changed' rule.
                for r, ad in list_attr_entries:
                    if not _rule_has_changed_action(r):
                        continue
                    sc = ad.get('set-change')
                    if sc is not None and sc.lower() == direction:
                        comp_val = r.get('compatible')
                        if comp_val:
                            return comp_val
                # Pass 2: direction-agnostic 'changed' rule (no set-change).
                for r, ad in list_attr_entries:
                    if not _rule_has_changed_action(r):
                        continue
                    if ad.get('set-change') is None:
                        comp_val = r.get('compatible')
                        if comp_val:
                            return comp_val
                return None

            # For olist (ordered list), order matters - compare as lists
            # For ulist (unordered list), order doesn't matter - compare as sets
            if list_type == 'olist':
                # Ordered list: order matters, any difference is a change.
                # If parent was just added, the key attribute is BC (schema-ordered-list-rule1).
                if parent_action == 'added' and action == 'added':
                    return 'backward-compatible', False

                if old_list != new_list:
                    old_items_set = set(old_list)
                    new_items_set = set(new_list)
                    added_items   = sorted(new_items_set - old_items_set)
                    deleted_items = sorted(old_items_set - new_items_set)

                    # Pure reordering (same elements, different order): always NBC.
                    if not added_items and not deleted_items:
                        return 'non-backward-compatible', False

                    # Elements removed: look up set-change="narrowed" rule (or direction-agnostic).
                    if deleted_items:
                        comp = _find_comp_for_set_change('narrowed')
                        if comp is not None:
                            return comp, False

                    # Elements added (and none removed): look up set-change="expanded" rule.
                    if added_items:
                        comp = _find_comp_for_set_change('expanded')
                        if comp is not None:
                            return comp, False

                # No change
                return 'backward-compatible', False
            else:
                # Unordered list (ulist or legacy 'list'): compare as sets
                old_items = set(old_list)
                new_items = set(new_list)
                added_items   = sorted(list(new_items - old_items))
                deleted_items = sorted(list(old_items - new_items))

                # Deletions (items removed → set narrowed) take priority.
                if deleted_items:
                    comp = _find_comp_for_set_change('narrowed')
                    if comp is not None:
                        return comp, False

                # Additions only (items added → set expanded).
                if added_items:
                    comp = _find_comp_for_set_change('expanded')
                    if comp is not None:
                        return comp, False

                # No item-level change (reordering or identical): backward-compatible.
                return 'backward-compatible', False

    # Special handling: list-type constraints with element-wise add/delete
    if subj_kind == 'field' and is_constraint and name:
        list_constr_entries: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for r in rules:
            for cd in r.get('constraints', []) or []:
                if isinstance(cd, dict) and cd.get('name') == name and cd.get('type') == 'list':
                    list_constr_entries.append((r, cd))
        if list_constr_entries:
            seps: List[str] = []
            for _r, cd in list_constr_entries:
                s = cd.get('separator')
                if s is not None:
                    seps.append(str(s))

            def _split_list(val: Any, separators: List[str]) -> List[str]:
                if val is None:
                    return []
                s = str(val).strip()
                if not s:
                    return []
                named_map = {
                    'space': ' ',
                    'whitespace': ' ',
                    'comma': ',',
                    'semicolon': ';',
                    'hyphen': '-',
                    'underscore': '_',
                }
                lits = [named_map.get(x.strip().lower(), x) for x in (seps or []) if x is not None]
                if not lits:
                    parts = re.split(r"\s+", s)
                else:
                    esc = [re.escape(x) for x in lits if x]
                    pattern = r"(?:" + r"|".join(esc) + r"|\s+)"
                    parts = re.split(pattern, s)
                return [p.strip() for p in parts if p.strip()]

            o, n = old_new()
            old_items = set(_split_list(o, seps))
            new_items = set(_split_list(n, seps))
            added_items = sorted(list(new_items - old_items))
            deleted_items = sorted(list(old_items - new_items))

            action_comp_map: Dict[str, str] = {}
            for r, _cd in list_constr_entries:
                comp_val = r.get('compatible')
                for act in (r.get('actions') or []):
                    a_name = act.get('action')
                    if a_name in ('added', 'deleted') and comp_val:
                        action_comp_map[a_name] = comp_val

            if deleted_items:
                comp = action_comp_map.get('deleted', 'non-backward-compatible')
                return comp, False
            if added_items:
                comp = action_comp_map.get('added', 'backward-compatible')
                return comp, False
            return 'backward-compatible', False

    # DEBUG: Log when processing 'type' keyword
    debug_type = False  # Disabled debug
    
    for rule in rules:
        subject_match = False
        matched_keyword_def = None
        matched_attr_def = None
        matched_constraint_def = None
        
        if subj_kind == 'keyword' and name:
            # First try the legacy flat keyword list (keywords without parent restriction)
            if name in rule.get('keywords', []):
                subject_match = True
                # Find the matching keyword definition to check for attributes (e.g., relaxed, recursive)
                for kw_def in rule.get('keyword_defs', []):
                    if kw_def.get('name') == name:
                        # Check parent attribute if specified
                        if 'parent' in kw_def:
                            if not _parent_matches(kw_def['parent'], parent_keyword):
                                continue  # Skip - parent doesn't match
                        matched_keyword_def = kw_def
                        break
            else:
                # Also check keyword_defs for parent-restricted keywords
                # (these are excluded from the legacy flat keywords list)
                for kw_def in rule.get('keyword_defs', []):
                    if kw_def.get('name') == name and 'parent' in kw_def:
                        if _parent_matches(kw_def['parent'], parent_keyword):
                            subject_match = True
                            matched_keyword_def = kw_def
                            break
        elif subj_kind == 'field' and name:
            if is_constraint:
                # Match constraints and check parent attribute
                for c_def in rule.get('constraints', []):
                    if c_def.get('name') == name:
                        # Check parent attribute if specified
                        if 'parent' in c_def:
                            if not _parent_matches(c_def['parent'], parent_keyword):
                                continue  # Skip - parent doesn't match
                        subject_match = True
                        matched_constraint_def = c_def
                        break
                # Transparent cross-match: report says "constraint" but the generated
                # rule may have placed the keyword under <attributes> instead.
                # This handles the case where Phase 2 clones from an attribute rule.
                if not subject_match:
                    for a_def in rule.get('attribute_defs', []):
                        if a_def.get('name') == name:
                            if 'parent' in a_def:
                                if not _parent_matches(a_def['parent'], parent_keyword):
                                    continue
                            subject_match = True
                            matched_attr_def = a_def
                            break
                    if not subject_match and name in rule.get('attributes', []):
                        subject_match = True
            else:
                # Match attributes and check parent attribute
                for a_def in rule.get('attribute_defs', []):
                    if a_def.get('name') == name:
                        # Check parent attribute if specified
                        if 'parent' in a_def:
                            if not _parent_matches(a_def['parent'], parent_keyword):
                                continue  # Skip - parent doesn't match
                        subject_match = True
                        matched_attr_def = a_def
                        break
                # Fallback to legacy attribute list (for backward compatibility)
                # Only used for attributes without parent restrictions
                if not subject_match and name in rule.get('attributes', []):
                    subject_match = True
                # Transparent cross-match: report says "attribute" but the generated
                # rule may have placed the keyword under <constraints> instead.
                # This handles the case where Phase 2 clones from a constraint rule
                # (e.g. max-access cloned from pattern → <constraint>max-access</constraint>)
                # so the re-run correctly classifies it instead of leaving it [UNMARKED].
                if not subject_match:
                    for c_def in rule.get('constraints', []):
                        if c_def.get('name') == name:
                            if 'parent' in c_def:
                                if not _parent_matches(c_def['parent'], parent_keyword):
                                    continue
                            subject_match = True
                            matched_constraint_def = c_def
                            break
        if not subject_match:
            continue

        if debug_type:
            print(f"DEBUG: Matched rule {rule['rule_id']} for type changed, keyword_def={matched_keyword_def}")

        actions = rule.get('actions') or []
        if not actions:
            actions = [{"action": action, "parent": None}]

        for act in actions:
            a_name = act.get('action')
            a_parent = act.get('parent')
            if a_name and a_name != action:
                continue
            if a_parent and parent_action != a_parent:
                continue
            
            if debug_type:
                print(f"DEBUG: Action matched in rule {rule['rule_id']}, will return: '{rule['compatible']}'")

            # Handle keyword-specific attributes (generic approach)
            # Currently supports: relaxed (for type compatibility checking)
            # Can be extended for: recursive, severity, priority, etc.
            if matched_keyword_def:
                # Check for 'relaxed' attribute (for type compatibility checking)
                if action == 'changed' and 'relaxed' in matched_keyword_def:
                    relaxed_attr = matched_keyword_def.get('relaxed')
                    # This keyword requires type compatibility checking
                    o, n = old_new()
                    if o and n:
                        # Perform type compatibility check
                        type_compat, _ = check_type_compatibility(str(o), str(n))
                        
                        if relaxed_attr == 'true':
                            # relaxed="true" means compatible type changes are OK
                            if type_compat == CompatibilityResult.COMPATIBLE:
                                return rule['compatible'], False
                            elif type_compat == CompatibilityResult.INCOMPATIBLE:
                                # Incompatible type change - skip this rule, let stricter rule handle it
                                continue  # Continue to next rule
                            # For CONDITIONAL or UNKNOWN, fall through to return rule's compatibility
                        elif relaxed_attr == 'false':
                            # relaxed="false" means check if types are incompatible
                            if type_compat == CompatibilityResult.INCOMPATIBLE:
                                return rule['compatible'], False
                            else:
                                # Compatible or conditional type change - skip this rule
                                continue  # Continue to next rule
                
                # Future: Check for other keyword attributes here
                # Example: if 'recursive' in matched_keyword_def:
                #            handle recursive checking
                # Example: if 'severity' in matched_keyword_def:
                #            adjust compatibility based on severity level
                # Example: if 'conditional' in matched_keyword_def:
                #            trigger conditional evaluation

            # ── Leafref type-name change: check_leafref_compatibility ─────────────
            # When the 'name' attribute changes under a 'leafref' parent (e.g.
            # leafref → int32), use check_leafref_compatibility to determine if the
            # new type is compatible with the leafref's resolved target type.
            # This runs BEFORE matched_attr_def check because leafref-attr-change-rule
            # uses the legacy attribute format (not attribute_defs), so matched_attr_def
            # is None for that rule.
            if name == 'name' and parent_keyword in ('leafref', 'type') and action == 'changed':
                o, n = old_new()
                if o and n:
                    old_base_c = str(o).split(':')[-1].lower()
                    new_base_c = str(n).split(':')[-1].lower()
                    if old_base_c == 'leafref' or new_base_c == 'leafref':
                        try:
                            from yang_rag.comparator.helper.check_leafref import (
                                check_leafref_compatibility as _clr,
                                resolve_leafref_target as _rlt,
                            )
                            from yang_rag.comparator.helper.yang_type_checker import (
                                CompatibilityResult as _CR,
                            )
                            _old_d: Dict[str, Any] = {'kind': old_base_c, 'base': old_base_c}
                            _new_d: Dict[str, Any] = {'kind': new_base_c, 'base': new_base_c}
                            # Strip /type and everything after it to get the leaf path.
                            # e.g. /old-types:leafref-to-int/type/int32 → /old-types:leafref-to-int
                            # This is needed so resolve_leafref_target finds the correct leaf node.
                            _leaf_ctx = context_path.rsplit('/type', 1)[0] if context_path and '/type' in context_path else context_path
                            # Extract the actual file from the report metadata (e.g. 'old-path.yang').
                            # When the main module is used as old_yang_file/new_yang_file, submodule
                            # nodes may not be in the resolver's cache.  Using the actual submodule
                            # file that contains the changed leaf gives the resolver the correct scope.
                            _meta_file = _extract_file_from_report_lines(lines, line_index) if lines and line_index is not None else None
                            if old_base_c == 'leafref' and _leaf_ctx and old_yang_file:
                                _tgt = _rlt(_leaf_ctx, old_yang_file, search_dirs)
                                if not _tgt and _meta_file:
                                    # Fallback: try the actual submodule file from report metadata
                                    # (old version lives in the old search dir)
                                    _old_actual = _resolve_yang_file_from_metadata(
                                        _meta_file,
                                        [search_dirs[0]] if search_dirs else search_dirs,
                                    )
                                    if _old_actual:
                                        _tgt = _rlt(_leaf_ctx, _old_actual, search_dirs)
                                if _tgt:
                                    _old_d['target_type'] = {'base': _tgt, 'kind': _tgt}
                            if new_base_c == 'leafref' and _leaf_ctx and new_yang_file:
                                _tgt = _rlt(_leaf_ctx, new_yang_file, search_dirs)
                                if not _tgt and _meta_file:
                                    # Fallback: try the actual submodule file from report metadata
                                    # (new version lives in the new search dir)
                                    _new_actual = _resolve_yang_file_from_metadata(
                                        _meta_file,
                                        [search_dirs[1]] if search_dirs and len(search_dirs) > 1 else search_dirs,
                                    )
                                    if _new_actual:
                                        _tgt = _rlt(_leaf_ctx, _new_actual, search_dirs)
                                if _tgt:
                                    _new_d['target_type'] = {'base': _tgt, 'kind': _tgt}
                            _lr_c, _lr_e = _clr(
                                old=_old_d, new=_new_d,
                                old_yang_file=old_yang_file,
                                new_yang_file=new_yang_file,
                                context_path=_leaf_ctx,
                                search_dirs=search_dirs,
                            )
                            if _lr_c is not None:
                                if _lr_c == _CR.COMPATIBLE:
                                    # Find the type-change-1-rule (relaxed="true", parent="type")
                                    # to get the correct compatible value for the current flag mode.
                                    # In default mode: backward-compatible
                                    # In rfc7950 mode: non-backward-compatible
                                    _bc_compat = 'backward-compatible'
                                    for _r2 in rules:
                                        for _ad2 in _r2.get('attribute_defs', []) or []:
                                            if (isinstance(_ad2, dict)
                                                    and _ad2.get('name') == 'name'
                                                    and _ad2.get('relaxed') == 'true'
                                                    and 'type' in (_ad2.get('parent') or [])):
                                                _bc_compat = _r2.get('compatible', 'backward-compatible')
                                                break
                                        else:
                                            continue
                                        break
                                    return _bc_compat, False
                                elif _lr_c == _CR.INCOMPATIBLE:
                                    return 'non-backward-compatible', False
                        except Exception:
                            pass

            # Handle attribute-specific type checks (identical, relaxed, etc.)
            # This uses a centralized helper to avoid code duplication
            if matched_attr_def:
                # Special handling: resolve typedefs for type 'name' attribute changes
                # This ensures that 'oc-if:interface-id' is resolved to 'string', etc.
                if name == 'name' and parent_keyword in ('type', 'leafref') and action == 'changed':
                    o, n = old_new()
                    if o and n:
                        # Resolve both old and new type names
                        old_resolved = resolve_typedef(str(o))
                        new_resolved = resolve_typedef(str(n))
                        
                        # Check if they resolve to the same primitive type
                        old_base = old_resolved.get('base') if isinstance(old_resolved, dict) else str(o)
                        new_base = new_resolved.get('base') if isinstance(new_resolved, dict) else str(n)
                        
                        # Strip module prefixes for comparison (e.g., "oc-types:string" -> "string")
                        old_base_clean = old_base.split(':')[-1] if old_base and ':' in str(old_base) else old_base
                        new_base_clean = new_base.split(':')[-1] if new_base and ':' in str(new_base) else new_base
                        
                        # If they resolve to the same primitive type, it's backward-compatible
                        if old_base_clean == new_base_clean:
                            if debug_type:
                                print(f"DEBUG: Type name changed from '{o}' to '{n}', but both resolve to '{old_base_clean}' - backward-compatible")
                            return 'backward-compatible', False

                should_return, compat = _handle_type_attribute_check(
                    matched_attr_def, action, parent_keyword, rule, old_new,
                    old_yang_file=old_yang_file,
                    new_yang_file=new_yang_file,
                    context_path=context_path,
                    search_dirs=search_dirs,
                    lines=lines,
                    line_index=line_index,
                )
                if should_return:
                    # Definitive result from type checker - return it
                    return compat, False
                elif compat is None and (matched_attr_def.get('identical') == 'true' or matched_attr_def.get('relaxed') in ('true', 'false') or matched_attr_def.get('type') == 'condition'):
                    # Type checker said "don't handle" for a type-specific rule - skip to next rule
                    continue

            # Use matched_constraint_def is not None (not just is_constraint) so that
            # cross-matched constraint rules (e.g. report says "attribute" but rule has
            # <constraint>) also go through the full constraint evaluation path.
            # This generalises the fix for all cases — not just vendor extensions.
            if is_constraint or matched_constraint_def is not None:
                entries = [c for c in rule.get('constraints', []) if c.get('name') == name]
                if not entries:
                    continue
                bearing = [c for c in entries if any(k in c for k in ("relaxed", "value", "from-value", "to-value", "type"))]
                presence_only = [c for c in entries if c not in bearing]
                o, n = old_new()
                if action == 'deleted' and o is None and n is not None:
                    o, n = n, None
                for ce in bearing:
                    if 'from-value' in ce or 'to-value' in ce:
                        fv = ce.get('from-value')
                        tv = ce.get('to-value')
                        ol = str(o).strip().lower() if o is not None else None
                        nl = str(n).strip().lower() if n is not None else None
                        if fv and tv and ol == fv.strip().lower() and nl == tv.strip().lower():
                            return rule['compatible'], False
                        if fv and not tv and ol == fv.strip().lower():
                            return rule['compatible'], False
                        if tv and not fv and nl == tv.strip().lower():
                            return rule['compatible'], False
                        continue
                    val = ce.get('value')
                    if val is not None:
                        exp = val.strip().lower()
                        ol = str(o).strip().lower() if o is not None else None
                        nl = str(n).strip().lower() if n is not None else None
                        # For 'changed' action: match on the NEW value (what it's changed TO).
                        # For 'deleted' action: match on the OLD value (what was deleted).
                        # For 'added' action: match on the NEW value (what was added).
                        # This prevents 'mandatory-false-add-rule' (value="false") from
                        # matching 'mandatory changed: true (was false)' via the old value.
                        matched = (nl == exp) if action in ('changed', 'added') else (ol == exp)
                        if matched:
                            return rule['compatible'], False
                        # value= present but didn't match — this rule does not apply.
                        # Skip to the next constraint entry; do NOT fall through to the
                        # type-only branch below (which would incorrectly return BC).
                        continue
                    wid = ce.get('relaxed')
                    if wid is not None:
                        cls = classify_constraint_change(
                            ce, o, n,
                            old_yang_file=old_yang_file,
                            new_yang_file=new_yang_file,
                            context_path=context_path,
                            search_dirs=search_dirs
                        )
                        # Handle marker for complex cases requiring LLM assistance.
                        # Use the rule's own compatibility verdict (BC or NBC) so the
                        # correct tag is set on the enriched report line, then append
                        # needs-deep-analysis for the LLM verification layer to review.
                        if cls == 'needs_llm_analysis':
                            return f"{rule['compatible']} needs-deep-analysis", False
                        if cls == 'unchanged':
                            # Semantically equivalent constraint changes are backward-compatible
                            return 'backward-compatible', False
                        if cls == 'unknown':
                            continue
                        if (wid == 'true' and cls == 'relaxed') or (wid == 'false' and cls in ('narrowed', 'not_equivalent')):
                            return rule['compatible'], False
                    elif ce.get('type') is not None:
                        # Constraint has a 'type' attribute but no 'relaxed' and no 'value' —
                        # it is a direction-agnostic typed constraint. The rule author listed
                        # it without a direction qualifier, meaning the rule applies regardless
                        # of whether the change is a relaxation or a narrowing.
                        return rule['compatible'], False
                if bearing and not presence_only:
                    cls = classify_constraint_change(
                        bearing[0], o, n,
                        old_yang_file=old_yang_file,
                        new_yang_file=new_yang_file,
                        context_path=context_path,
                        search_dirs=search_dirs
                    )
                    # Handle marker for complex cases requiring LLM assistance.
                    # Use the rule's own compatibility verdict (BC or NBC) so the
                    # correct tag is set on the enriched report line, then append
                    # needs-deep-analysis for the LLM verification layer to review.
                    if cls == 'needs_llm_analysis':
                        return f"{rule['compatible']} needs-deep-analysis", False
                    if cls == 'unchanged':
                        # Semantically equivalent constraint changes are backward-compatible
                        return 'backward-compatible', False
                    if cls in ('relaxed', 'narrowed', 'not_equivalent'):
                        # 'not_equivalent' maps to 'narrowed' semantics for rule lookup
                        target_wid = 'true' if cls == 'relaxed' else 'false'
                        for r2 in rules:
                            for c2 in r2.get('constraints', []):
                                if c2.get('name') == name and c2.get('relaxed') == target_wid:
                                    acts2 = r2.get('actions') or []
                                    if (not acts2) or any(a.get('action') == action for a in acts2):
                                        return r2['compatible'], False
                if presence_only:
                    return rule['compatible'], False
            else:
                # Note: Type-specific attribute checking is now handled by _handle_type_attribute_check()
                # which properly respects flag-aware rule compatibility values
                
                if rule['compatible'] == 'conditional-backward-compatible':
                    return rule['compatible'], True
                return rule['compatible'], False
    return None, False


# ---------------------------------------------------------------------------
# Conditional children processing
# ---------------------------------------------------------------------------

def process_conditional_block(
    lines: List[str], 
    start_idx: int, 
    rules: List[Dict[str, Any]],
    old_yang_file: Optional[str] = None,
    new_yang_file: Optional[str] = None,
    search_dirs: Optional[List[str]] = None
) -> bool:
    """
    Process a conditional block and determine if all children are backward compatible.
    
    Returns True only if ALL children are backward-compatible.
    Returns False if ANY child is non-backward-compatible or needs analysis.
    """
    parent_indent = len(lines[start_idx]) - len(lines[start_idx].lstrip())
    subj, _name, action, _ = parse_subject_action(lines[start_idx].rstrip())
    parent_action = action if subj == 'keyword' else None
    idx = start_idx + 1
    all_ok = True
    while idx < len(lines):
        line = lines[idx]
        if not line.startswith(' '):
            break
        cur_indent = len(line) - len(line.lstrip())
        if cur_indent <= parent_indent:
            break
        comp, is_cond = determine_compatibility(
            line.rstrip(), rules, parent_action=parent_action,
            old_yang_file=old_yang_file, new_yang_file=new_yang_file, search_dirs=search_dirs,
            lines=lines, line_index=idx
        )
        
        # Check if child is non-backward-compatible or needs analysis
        if comp:
            # Handle multi-tag compatibility strings (e.g., "non-backward-compatible needs-deep-analysis")
            if 'non-backward-compatible' in comp or 'needs-deep-analysis' in comp or 'needs-dspy-analysis' in comp:
                all_ok = False
        
        # Recursively process nested conditional blocks
        if is_cond:
            if not process_conditional_block(
                lines, idx, rules,
                old_yang_file=old_yang_file, new_yang_file=new_yang_file, search_dirs=search_dirs
            ):
                all_ok = False
        idx += 1
    return all_ok


# ---------------------------------------------------------------------------
# Report enrichment
# ---------------------------------------------------------------------------

def enrich_report(
    comparison_file: str,
    xml_file: str,
    output_file: str,
    old_yang_file: Optional[str] = None,
    new_yang_file: Optional[str] = None,
    search_dirs: Optional[List[str]] = None,
    compatibility_flag: Optional[str] = None,
    llm_verify: bool = False,
):
    """
    Enrich comparison report with compatibility tags based on XML rules.
    
    Args:
        comparison_file: Path to filtered report file
        xml_file: Path to compatibility rules XML
        output_file: Path to save enriched report
        old_yang_file: Path to old YANG file (for XPath resolution and typedef loading)
        new_yang_file: Path to new YANG file (for XPath resolution and typedef loading)
        search_dirs: Directories to search for imported YANG modules
        compatibility_flag: Optional flag to select specific compatibility mode (e.g., "rfc7950", "strict")
                           If provided, uses rules with matching flag attribute when available
        llm_verify: When True, append <needs-deep-analysis> to every when/must/pattern/leafref
                    change so the LLM verification layer always reviews these semantically
                    complex constraints as a second-layer check.
    """
    # Clear and load typedefs from YANG files for type compatibility checking
    clear_registries()
    typedef_count = 0
    identity_count = 0
    if old_yang_file:
        try:
            count = load_typedefs_from_file(old_yang_file, search_dirs)
            typedef_count += count
            print(f"Loaded {count} typedefs from old YANG file: {old_yang_file}")
        except Exception as e:
            print(f"Warning: Failed to load typedefs from old YANG file: {e}")
        
        try:
            count = load_identities_from_file(old_yang_file, search_dirs)
            identity_count += count
            print(f"Loaded {count} identities from old YANG file: {old_yang_file}")
        except Exception as e:
            print(f"Warning: Failed to load identities from old YANG file: {e}")
    
    if new_yang_file:
        try:
            count = load_typedefs_from_file(new_yang_file, search_dirs)
            typedef_count += count
            print(f"Loaded {count} typedefs from new YANG file: {new_yang_file}")
        except Exception as e:
            print(f"Warning: Failed to load typedefs from new YANG file: {e}")
        
        try:
            count = load_identities_from_file(new_yang_file, search_dirs)
            identity_count += count
            print(f"Loaded {count} identities from new YANG file: {new_yang_file}")
        except Exception as e:
            print(f"Warning: Failed to load identities from new YANG file: {e}")
    
    if typedef_count > 0:
        print(f"Total typedefs loaded: {typedef_count}")
    else:
        print("Warning: No typedefs loaded - type compatibility checking may be inaccurate")
    
    if identity_count > 0:
        print(f"Total identities loaded: {identity_count}")
    else:
        print("Warning: No identities loaded - identity validation may be inaccurate")
    
    if compatibility_flag:
        print(f"Strict compatibility mode ENABLED: {compatibility_flag}")
    
    rules = parse_rules(xml_file, compatibility_flag=compatibility_flag)
    with open(comparison_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    enriched: List[str] = []
    for i, raw in enumerate(lines):
        line = raw.rstrip('\n')
        parent_action = _find_parent_action(lines, i) if line.startswith(' ') else None
        
        # Check for missing references (identity/typedef/grouping that don't exist)
        has_missing_refs = check_missing_references(
            line, lines, i, old_yang_file=old_yang_file, new_yang_file=new_yang_file, search_dirs=search_dirs
        )
        
        comp, is_cond = determine_compatibility(
            line, rules, parent_action=parent_action,
            old_yang_file=old_yang_file, new_yang_file=new_yang_file, search_dirs=search_dirs,
            lines=lines, line_index=i
        )

        # When --llm-verify is active, determine if this line targets a semantically
        # complex constraint/attribute that should always be reviewed by the LLM as a
        # second-layer check (when/must/pattern/leafref path).
        # We parse the subject here so we can reuse it below for the valueless-header check.
        subj_kind_line, name_line, action_line, is_constraint_line = parse_subject_action(line)

        # Rule-driven <needs-deep-analysis> injection when --llm-verify is active.
        # Check if any rule that matches this line has assistance="true" on the
        # matched element AND the rule's declared actions include the current action.
        # This prevents false positives where a rule with assistance="true" for
        # 'changed' also triggers for 'deleted' (e.g., leafref 'path' deleted).
        needs_deep = False
        if llm_verify and comp is not None and 'needs-deep-analysis' not in (comp or ''):
            # Extract parent keyword so _rule_element_has_assistance can enforce
            # parent restrictions (e.g. name-augment-change-rule only applies when
            # the parent is 'augment' or 'deviation', not 'leaf' or 'container').
            parent_kw_for_assist = _find_parent_keyword(lines, i) if lines else None
            for r in rules:
                if _rule_element_has_assistance(
                    r, name_line, is_constraint_line,
                    action=action_line,
                    parent_keyword=parent_kw_for_assist,
                ):
                    needs_deep = True
                    break
        
        if comp:
            if is_cond:
                ok = process_conditional_block(
                    lines, i, rules,
                    old_yang_file=old_yang_file, new_yang_file=new_yang_file, search_dirs=search_dirs
                )
                final = 'backward-compatible' if ok else 'non-backward-compatible'
                # Append <needs-deep-analysis> when llm_verify is active and rule has assistance="true"
                deep_tag = ' <needs-deep-analysis>' if needs_deep else ''
                # Add <not-found> tag if missing references detected
                if has_missing_refs:
                    enriched.append(f"{line} <{final}>{deep_tag} <not-found>\n")
                else:
                    enriched.append(f"{line} <{final}>{deep_tag}\n")
            else:
                # Format multi-tag: "non-backward-compatible needs-deep-analysis" becomes
                # "<non-backward-compatible> <needs-deep-analysis>"
                if ' ' in comp:
                    tags = ' '.join(f'<{tag}>' for tag in comp.split())
                    # Append <needs-deep-analysis> when rule has assistance="true" (avoid duplicate)
                    if needs_deep and '<needs-deep-analysis>' not in tags:
                        tags = f"{tags} <needs-deep-analysis>"
                    # Add <not-found> tag if missing references detected
                    if has_missing_refs:
                        enriched.append(f"{line} {tags} <not-found>\n")
                    else:
                        enriched.append(f"{line} {tags}\n")
                else:
                    # Append <needs-deep-analysis> when rule has assistance="true"
                    deep_tag = ' <needs-deep-analysis>' if needs_deep else ''
                    # Add <not-found> tag if missing references detected
                    if has_missing_refs:
                        enriched.append(f"{line} <{comp}>{deep_tag} <not-found>\n")
                    else:
                        enriched.append(f"{line} <{comp}>{deep_tag}\n")
        elif has_missing_refs:
            # No compatibility tag but has missing references - add <not-found> tag only
            enriched.append(f"{line} <not-found>\n")
        else:
            # comp is None — check if this is a valueless attribute-changed header
            # (e.g. "1.1 attribute changed: ['type/union/string']" with no '->').
            # These are structural grouping lines whose real content lives in child
            # sub-items (1.1.1, 1.1.2 …).  Inherit the worst-case tag from children
            # so the line is not left untagged and pulled into the 'unmarked' bucket.
            is_valueless_attr_header = (
                subj_kind_line == 'field'
                and action_line is not None
                and '->' not in line
            )
            if is_valueless_attr_header:
                all_children_ok = process_conditional_block(
                    lines, i, rules,
                    old_yang_file=old_yang_file, new_yang_file=new_yang_file, search_dirs=search_dirs
                )
                inherited = 'backward-compatible' if all_children_ok else 'non-backward-compatible'
                if has_missing_refs:
                    enriched.append(f"{line} <{inherited}> <not-found>\n")
                else:
                    enriched.append(f"{line} <{inherited}>\n")
            else:
                enriched.append(raw)
    with open(output_file, 'w', encoding='utf-8') as f:
        f.writelines(enriched)
    print(f"Enriched report saved to {output_file}")


if __name__ == '__main__':
    import sys
    import os
    import argparse
    
    parser = argparse.ArgumentParser(description='Enrich YANG comparison report with compatibility tags')
    parser.add_argument('old_yang', nargs='?', help='Path to old YANG file')
    parser.add_argument('new_yang', nargs='?', help='Path to new YANG file')
    parser.add_argument('search_dirs', nargs='*', help='Directories to search for imported modules')
    parser.add_argument(
        '--xml-rules',
        dest='xml_rules',
        default=None,
        help='Path to a custom compatibility_rules.xml (default: bundled rules beside this script)'
    )
    parser.add_argument(
        '--llm-verify',
        dest='llm_verify',
        action='store_true',
        default=False,
        help=(
            'When set, append <needs-deep-analysis> to every when/must/pattern/leafref '
            'change so the LLM verification layer always reviews these semantically '
            'complex constraints as a second-layer check.'
        )
    )
    
    # Accept any --flag format for compatibility modes
    # First, parse known args to separate positional from flags
    known_args, unknown_args = parser.parse_known_args()
    
    # Look for any --* flag in unknown args to use as compatibility flag
    # (skip --llm-verify which is already parsed above)
    compatibility_flag = None
    for arg in unknown_args:
        if arg.startswith('--') and arg != '--llm-verify':
            # Extract flag name (remove -- prefix)
            flag_name = arg[2:]
            compatibility_flag = flag_name
            break
    
    # For backward compatibility, also check if --rfc7950 was in original args
    if '--rfc7950' in sys.argv:
        compatibility_flag = 'rfc7950'
    
    # Determine path to rules file: user-supplied or default beside this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_xml = os.path.join(script_dir, 'compatibility_rules.xml')
    rules_xml = known_args.xml_rules if known_args.xml_rules else default_xml

    enrich_report(
        'output/filtered_report.txt',
        rules_xml,
        'output/enriched_report.txt',
        old_yang_file=known_args.old_yang,
        new_yang_file=known_args.new_yang,
        search_dirs=known_args.search_dirs if known_args.search_dirs else None,
        compatibility_flag=compatibility_flag,
        llm_verify=known_args.llm_verify,
    )
