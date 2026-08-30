"""
Leafref compatibility checker.

This module handles all compatibility cases involving leafref types:

  Case 1: leafref -> leafref (path changed, same or different target)
    - Same path                                    : BC
    - Different path, same target node             : BC  (semantically equivalent)
    - Different path, old invalid / new valid      : BC  (fix — constraint was broken)
    - Different path, old valid / new invalid      : NBC (regression)
    - Different path, different target nodes       : NBC
    - Both paths invalid                           : NBC (incomparable)

  Case 2: leafref -> other_type (type changed away from leafref)
    The leafref's target type is resolved and compared against the new type
    using the normal type compatibility engine:
    - leafref(->int8)   -> int32  : BC  (int32 is a superset of int8)
    - leafref(->int32)  -> int8   : NBC (int8 is a subset of int32)
    - leafref(->int16)  -> string : NBC (incompatible type families)
    - leafref(->int8)   -> string : NBC (incompatible type families)
    - leafref(target unknown) -> any : NBC (conservative, target unknown)

  Case 3: other_type -> leafref (type changed to leafref -- always NBC)
    - any -> leafref : NBC (adds referential integrity constraint)

Path comparison uses :class:`~yang_rag.comparator.helper.xpath_comparison.PathComparisonOutcome`
(cases A–E) for clean, enum-based classification.
"""

from typing import Any, Dict, List, Optional, Tuple

from yang_rag.comparator.helper.yang_type_checker import (
    check_type_compatibility,
    CompatibilityResult,
)
from yang_rag.comparator.helper.xpath_comparison import (
    PathComparisonOutcome,
    compare_single_xpath,
)


def _get_target_type_base(descriptor: Dict[str, Any]) -> Optional[str]:
    """Extract the resolved target type base name from a leafref descriptor."""
    target = descriptor.get("target_type")
    if not target:
        return None
    return target.get("base") or target.get("kind") or None


def resolve_leafref_target(
    parent_path: str,
    yang_file: str,
    search_dirs: Optional[List[str]] = None,
) -> Optional[str]:
    """
    Resolve the leafref path and return the target node's type name.

    Delegates to XPathResolver.get_leafref_target_type() which is the single
    source of truth for leafref target resolution, handling grouping contexts
    correctly via the existing resolve_xpath infrastructure.

    Args:
        parent_path: Schema path of the leaf containing the leafref type
                     (e.g., '/old-types:leafref-to-int').
        yang_file: Path to the YANG file to load for resolution.
        search_dirs: Directories to search for imported YANG modules.

    Returns:
        The target node's type name (e.g., 'int8', 'string') or None if
        resolution fails.
    """
    try:
        from yang_rag.comparator.helper.xpath_resolver import get_cached_xpath_resolver
        resolver = get_cached_xpath_resolver(yang_file, search_dirs=search_dirs or [])
        return resolver.get_leafref_target_type(parent_path)
    except Exception:
        return None


def _resolve_target_from_context(
    descriptor: Dict[str, Any],
    yang_file: Optional[str],
    context_path: Optional[str],
    search_dirs: Optional[List[str]],
) -> Optional[str]:
    """
    Get the resolved target type base for a leafref descriptor.

    First checks if 'target_type' is already set in the descriptor.
    If not, attempts XPath resolution using the YANG file and context path.
    """
    # Check descriptor first
    base = _get_target_type_base(descriptor)
    if base:
        return base
    # Try XPath resolution.
    # The context_path may have the form '<leaf-path>/type/<type-name>'
    # (e.g. '.../required-module/type/string') or just '<leaf-path>/type'.
    # Strip everything from '/type' onward to get the leaf node path.
    if yang_file and context_path:
        type_idx = context_path.rfind('/type')
        parent_path = context_path[:type_idx] if type_idx != -1 else context_path
        return resolve_leafref_target(parent_path, yang_file, search_dirs)
    return None


def check_leafref_compatibility(
    old: Any,
    new: Any,
    old_yang_file: Optional[str] = None,
    new_yang_file: Optional[str] = None,
    context_path: Optional[str] = None,
    search_dirs: Optional[List[str]] = None,
) -> Tuple[Optional[CompatibilityResult], str]:
    """
    Check compatibility when one or both types involve leafref.

    Handles all three cases:
      1. leafref -> leafref  (path changed; compare resolved target types)
      2. leafref -> other    (type changed away from leafref)
      3. other  -> leafref   (type changed to leafref -- always NBC)

    Args:
        old: Old type descriptor (dict with 'kind'/'base') or type name string.
             If kind='leafref' and 'target_type' is set, the target type is used
             for comparison instead of the generic leafref kind.
        new: New type descriptor or type name string.
             If kind='leafref' and 'target_type' is set, the target type is used.
        old_yang_file: Path to old YANG file (for XPath resolution).
        new_yang_file: Path to new YANG file (for XPath resolution).
        context_path: Schema path of the node being compared (e.g., '/mod:leaf/type').
                      Used to find the leaf node and extract the leafref path.
        search_dirs: Directories to search for imported YANG modules.

    Returns:
        (CompatibilityResult, explanation) or (None, '') if not applicable
        (i.e., neither old nor new is a leafref type).
    """
    # Normalize to dicts
    old_desc = old if isinstance(old, dict) else {"base": str(old), "kind": str(old)}
    new_desc = new if isinstance(new, dict) else {"base": str(new), "kind": str(new)}

    old_kind = old_desc.get("kind", "")
    new_kind = new_desc.get("kind", "")

    # Not a leafref case -- caller should use check_type_compatibility directly
    if old_kind != "leafref" and new_kind != "leafref":
        return None, ""

    # ── Case 3: other_type -> leafref ────────────────────────────────────────
    # Changing any type TO leafref adds referential integrity constraints.
    # This is always NBC (narrowing: fewer values are valid).
    if old_kind != "leafref" and new_kind == "leafref":
        return (
            CompatibilityResult.INCOMPATIBLE,
            f"{old_kind} -> leafref: incompatible (adds referential integrity constraint)",
        )

    # ── Case 1: leafref -> leafref ────────────────────────────────────────────
    if old_kind == "leafref" and new_kind == "leafref":
        old_path = old_desc.get("path", "")
        new_path = new_desc.get("path", "")
        if old_path == new_path:
            return CompatibilityResult.COMPATIBLE, "leafref path unchanged"

        # Different path string — delegate to compare_single_xpath which
        # implements the canonical A→E classification using PathComparisonOutcome.
        # The context_path is normalised inside compare_single_xpath (strips
        # trailing /type, /leafref, etc.) so XPath resolution starts from the
        # correct leaf-level node.
        cmp = compare_single_xpath(
            old_path=old_path,
            new_path=new_path,
            context_path=context_path or "",
            old_yang_file=old_yang_file,
            new_yang_file=new_yang_file,
            search_dirs=search_dirs,
        )

        match cmp.outcome:
            case PathComparisonOutcome.EQUIVALENT:
                return CompatibilityResult.COMPATIBLE, cmp.explanation
            case PathComparisonOutcome.RELAXED:
                # Old path was broken, new is valid — BC fix
                return CompatibilityResult.COMPATIBLE, cmp.explanation
            case PathComparisonOutcome.NARROWED:
                # Old path was valid, new is broken — NBC regression
                return CompatibilityResult.INCOMPATIBLE, cmp.explanation
            case PathComparisonOutcome.INCOMPARABLE:
                return CompatibilityResult.INCOMPATIBLE, cmp.explanation
            case _:
                return CompatibilityResult.INCOMPATIBLE, cmp.explanation

    # ── Case 2: leafref -> other_type ────────────────────────────────────────
    if old_kind == "leafref" and new_kind != "leafref":
        new_base = new_desc.get("base") or new_desc.get("kind", "")

        # Resolve the old leafref's target type and compare it against the new type
        # using the normal type compatibility engine.
        #
        # Examples:
        #   leafref(→int8)  -> int32  : int8 vs int32  → BC  (int32 is superset)
        #   leafref(→int8)  -> string : int8 vs string → NBC (incomparable families)
        #   leafref(→int16) -> string : int16 vs string → NBC (incomparable families)
        #
        # If the target type cannot be resolved (unknown leafref target), fall back
        # to NBC (conservative).
        old_target_base = _resolve_target_from_context(
            old_desc, old_yang_file, context_path, search_dirs
        )

        if old_target_base:
            # Resolved target type available -- compare target type against new type
            compat, expl = check_type_compatibility(old_target_base, new_base)
            return compat, f"leafref(target={old_target_base}) -> {new_base}: {expl}"
        else:
            # No resolved target -- NBC (conservative, target type unknown)
            return (
                CompatibilityResult.INCOMPATIBLE,
                f"leafref -> {new_kind}: incompatible type change (leafref target type unknown)",
            )

    # Should not reach here
    return None, ""
