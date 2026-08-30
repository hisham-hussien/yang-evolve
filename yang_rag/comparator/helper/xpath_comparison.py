"""
xpath_comparison.py — Clean XPath path comparison with enum-based results.

This module provides the canonical XPath comparison logic for YANG compatibility
checking.  It covers two distinct use-cases driven by the XML rule ``type``
attribute:

  type="XPath"     — Single-path comparison (leafref, augment, deviation).
                     One XPath string is compared across old and new schemas.

  type="condition" — Two-sided comparison (when, must).
                     A condition of the form ``<LHS-path> <op> <RHS-value>``
                     is analysed: the LHS path is resolved as a single-path
                     comparison, and the RHS value is validated separately.

Classification enum
-------------------
Both comparison types produce a ``PathComparisonOutcome`` value:

  EQUIVALENT   — Both sides resolve to the same schema node.  → BC.
  RELAXED      — Old side was invalid; new side is valid (fix).  → BC.
  NARROWED     — Old side was valid; new side is invalid (regression).  → NBC.
  INCOMPARABLE — Both valid but different nodes, or both invalid,
                 or relationship cannot be determined.  → NBC (conservative).

Context normalisation
---------------------
For ``leafref`` and ``uses`` statements the XPath is evaluated from the *leaf*
(or *uses* parent) node, not from the sub-statement node.  The helper
``normalise_context_path`` strips the last path segment when it is a YANG
sub-statement keyword (not a data-tree node) so that relative ``../`` steps
are counted correctly.

The set of strippable keywords is NOT hardcoded — instead the function checks
whether the last segment matches a known YANG sub-statement keyword set
(RFC 7950 §7).  This set covers keywords that appear as sub-statements of
data nodes but are not themselves data nodes.  New keywords can be added to
``YANG_SUB_STATEMENT_KEYWORDS`` without touching any other logic.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# YANG sub-statement keywords (RFC 7950)
# These are keywords that appear as sub-statements of data nodes but are NOT
# themselves data-tree nodes.  When one of these appears as the last segment
# of a context path it should be stripped so that XPath resolution starts
# from the correct data-tree node.
#
# NOTE: augment and deviation are NOT included here because they ARE real
# path segments in the data tree (augment targets a real schema node).
# ---------------------------------------------------------------------------

YANG_SUB_STATEMENT_KEYWORDS: Set[str] = {
    # Type-related sub-statements
    "type",
    "leafref",
    "union",
    "enumeration",
    "bits",
    "identityref",
    "instance-identifier",
    # Grouping-related (uses is a sub-statement, grouping is a definition)
    "uses",
    "grouping",
    # Other sub-statements that are not data nodes
    "typedef",
    "input",
    "output",
}


# ---------------------------------------------------------------------------
# Public enum
# ---------------------------------------------------------------------------


class PathComparisonOutcome(enum.Enum):
    """Result of comparing an XPath expression across old and new YANG schemas.

    Maps directly to the five cases:

    A. EQUIVALENT   — both valid, same node          → BC
    B. INCOMPARABLE — both valid, different nodes    → NBC
    C. RELAXED      — old invalid, new valid (fix)   → BC
    D. NARROWED     — old valid, new invalid (break) → NBC
    E. INCOMPARABLE — both invalid                   → NBC
    """

    EQUIVALENT   = "equivalent"    # A — same target node
    RELAXED      = "relaxed"       # C — old broken → new valid (fix)
    NARROWED     = "narrowed"      # D — old valid → new broken (regression)
    INCOMPARABLE = "incomparable"  # B / E — cannot determine or genuinely different

    @property
    def is_backward_compatible(self) -> bool:
        """True when the outcome is backward-compatible (BC)."""
        return self in (PathComparisonOutcome.EQUIVALENT, PathComparisonOutcome.RELAXED)

    @property
    def is_non_backward_compatible(self) -> bool:
        """True when the outcome is non-backward-compatible (NBC)."""
        return not self.is_backward_compatible


@dataclass(frozen=True)
class PathComparisonResult:
    """Full result of a single-path XPath comparison.

    Attributes:
        outcome:     The classification enum value.
        explanation: Human-readable explanation of the decision.
        old_abs:     Resolved absolute path in the old schema (or None).
        new_abs:     Resolved absolute path in the new schema (or None).
    """

    outcome: PathComparisonOutcome
    explanation: str
    old_abs: Optional[str] = None
    new_abs: Optional[str] = None

    @property
    def is_backward_compatible(self) -> bool:
        return self.outcome.is_backward_compatible


# ---------------------------------------------------------------------------
# Context path normalisation
# ---------------------------------------------------------------------------


def normalise_context_path(context_path: str) -> str:
    """Strip the last path segment if it is a YANG sub-statement keyword.

    The rule is: if the last segment of the context path is a keyword that
    appears as a sub-statement of a data node (not a data node itself), strip
    it.  This ensures that relative XPath steps (``../``) are evaluated from
    the correct data-tree node.

    Examples::

        /mod:container/leaf/type      →  /mod:container/leaf
        /mod:container/leaf/leafref   →  /mod:container/leaf
        /mod:container/leaf/uses      →  /mod:container/leaf
        /mod:container/leaf           →  /mod:container/leaf  (unchanged)
        /mod:container/augment/leaf   →  /mod:container/augment/leaf  (unchanged)

    The check is case-insensitive and strips any namespace prefix from the
    last segment before comparing (e.g. ``oc:type`` → ``type``).

    Args:
        context_path: Raw context path from the report.

    Returns:
        Normalised context path with the trailing sub-statement keyword
        removed, or the original path if the last segment is a data node.
    """
    if not context_path:
        return context_path

    parts = context_path.rstrip("/").split("/")
    if not parts:
        return context_path

    last = parts[-1]
    # Strip any namespace prefix (e.g. "oc:type" → "type")
    local = last.split(":")[-1].lower() if ":" in last else last.lower()

    if local in YANG_SUB_STATEMENT_KEYWORDS:
        # Strip the sub-statement keyword segment
        return "/".join(parts[:-1]) or "/"

    return context_path


# ---------------------------------------------------------------------------
# Cross-version suffix match helper
# ---------------------------------------------------------------------------


def _path_suffix_match(shorter: str, longer: str) -> bool:
    """Return True if *shorter* (minus module prefix) is a tail of *longer*.

    This handles the case where a new top-level container was added between
    schema versions (e.g. ``mpls`` → ``mpls-top/mpls``).  Both paths point
    to the same logical node even though their absolute representations differ.

    Requires at least 2 matching tail segments to avoid false positives on
    very short paths.
    """
    s_parts = [p for p in shorter.split("/") if p][1:]  # strip module name
    l_parts = [p for p in longer.split("/") if p][1:]
    return len(s_parts) >= 2 and l_parts[-len(s_parts):] == s_parts


# ---------------------------------------------------------------------------
# Single-path XPath comparison  (type="XPath")
# ---------------------------------------------------------------------------


def compare_single_xpath(
    old_path: str,
    new_path: str,
    context_path: str,
    old_yang_file: Optional[str],
    new_yang_file: Optional[str],
    search_dirs: Optional[List[str]] = None,
) -> PathComparisonResult:
    """Compare a single XPath path expression across old and new YANG schemas.

    Used for attributes declared with ``type="XPath"`` in the XML rules
    (e.g. leafref ``path``, augment/deviation ``name``).

    The context path is normalised (trailing YANG sub-statement keywords
    stripped) before resolution so that relative ``../`` steps are evaluated
    from the correct data-tree node.

    Classification (cases A–E):
        A. Both valid, same node       → EQUIVALENT   (BC)
        B. Both valid, different nodes → INCOMPARABLE (NBC)
        C. Old invalid, new valid      → RELAXED      (BC)
        D. Old valid, new invalid      → NARROWED     (NBC)
        E. Both invalid                → INCOMPARABLE (NBC)

    Args:
        old_path:      Old XPath string (relative or absolute).
        new_path:      New XPath string (relative or absolute).
        context_path:  Schema path where the XPath is attached.  Will be
                       normalised automatically.
        old_yang_file: Path to the old YANG file for resolution.
        new_yang_file: Path to the new YANG file for resolution.
        search_dirs:   Additional directories to search for imports.

    Returns:
        A :class:`PathComparisonResult` with the outcome and explanation.
    """
    # Fast path: identical strings → trivially equivalent
    if old_path == new_path:
        return PathComparisonResult(
            outcome=PathComparisonOutcome.EQUIVALENT,
            explanation=f"XPath path unchanged: '{old_path}'",
        )

    # Normalise context (strip YANG sub-statement keywords)
    effective_context = normalise_context_path(context_path)

    if not old_yang_file or not new_yang_file or not effective_context:
        # Cannot resolve — conservative INCOMPARABLE
        return PathComparisonResult(
            outcome=PathComparisonOutcome.INCOMPARABLE,
            explanation=(
                f"Cannot resolve XPath semantically "
                f"(missing YANG files or context): "
                f"'{old_path}' → '{new_path}'"
            ),
        )

    try:
        from yang_rag.comparator.helper.xpath_resolver import get_cached_xpath_resolver

        old_resolver = get_cached_xpath_resolver(
            old_yang_file, search_dirs=search_dirs or []
        )
        new_resolver = get_cached_xpath_resolver(
            new_yang_file, search_dirs=search_dirs or []
        )

        old_resolved = old_resolver.resolve_xpath(effective_context, old_path)
        new_resolved = new_resolver.resolve_xpath(effective_context, new_path)

        old_abs: Optional[str] = (
            old_resolved.absolute_path
            if old_resolved and old_resolved.success
            else None
        )
        new_abs: Optional[str] = (
            new_resolved.absolute_path
            if new_resolved and new_resolved.success
            else None
        )

        return _classify_resolved_paths(old_path, new_path, old_abs, new_abs)

    except Exception as exc:
        return PathComparisonResult(
            outcome=PathComparisonOutcome.INCOMPARABLE,
            explanation=(
                f"XPath resolution error: {exc}; "
                f"cannot compare '{old_path}' → '{new_path}'"
            ),
        )


def _classify_resolved_paths(
    old_path: str,
    new_path: str,
    old_abs: Optional[str],
    new_abs: Optional[str],
) -> PathComparisonResult:
    """Apply the A→E classification given resolved absolute paths."""

    match (bool(old_abs), bool(new_abs)):

        case (True, True):
            # Both resolved — compare absolute paths
            if old_abs == new_abs:
                # Case A: same node
                return PathComparisonResult(
                    outcome=PathComparisonOutcome.EQUIVALENT,
                    explanation=f"Both XPaths resolve to the same node: {old_abs}",
                    old_abs=old_abs,
                    new_abs=new_abs,
                )

            # Cross-version suffix match: schema root depth changed
            # (e.g. 'mpls' → 'mpls-top/mpls' added a container)
            if _path_suffix_match(old_abs, new_abs) or _path_suffix_match(
                new_abs, old_abs
            ):
                return PathComparisonResult(
                    outcome=PathComparisonOutcome.EQUIVALENT,
                    explanation=(
                        f"XPaths resolve to the same logical node "
                        f"(cross-version schema root change): "
                        f"'{old_path}' → {old_abs}, "
                        f"'{new_path}' → {new_abs}"
                    ),
                    old_abs=old_abs,
                    new_abs=new_abs,
                )

            # Case B: different nodes
            return PathComparisonResult(
                outcome=PathComparisonOutcome.INCOMPARABLE,
                explanation=(
                    f"XPaths resolve to different nodes: "
                    f"'{old_path}' → {old_abs}, "
                    f"'{new_path}' → {new_abs}"
                ),
                old_abs=old_abs,
                new_abs=new_abs,
            )

        case (False, True):
            # Case C: old invalid, new valid → fix (RELAXED = BC)
            return PathComparisonResult(
                outcome=PathComparisonOutcome.RELAXED,
                explanation=(
                    f"Old XPath was invalid/unresolvable ('{old_path}'), "
                    f"new XPath resolves to {new_abs} — constraint fixed (BC)"
                ),
                old_abs=None,
                new_abs=new_abs,
            )

        case (True, False):
            # Case D: old valid, new invalid → regression (NARROWED = NBC)
            return PathComparisonResult(
                outcome=PathComparisonOutcome.NARROWED,
                explanation=(
                    f"Old XPath resolved to {old_abs}, "
                    f"new XPath is invalid/unresolvable ('{new_path}') — "
                    f"constraint broken (NBC)"
                ),
                old_abs=old_abs,
                new_abs=None,
            )

        case (False, False):
            # Case E: both invalid → INCOMPARABLE
            return PathComparisonResult(
                outcome=PathComparisonOutcome.INCOMPARABLE,
                explanation=(
                    f"Both XPaths are invalid/unresolvable: "
                    f"'{old_path}' and '{new_path}'"
                ),
            )

        case _:  # pragma: no cover
            return PathComparisonResult(
                outcome=PathComparisonOutcome.INCOMPARABLE,
                explanation="Unexpected resolution state",
            )


# ---------------------------------------------------------------------------
# Convenience: map outcome to legacy string tokens used by the rule engine
# ---------------------------------------------------------------------------


def outcome_to_semantics_preserving(outcome: PathComparisonOutcome) -> bool:
    """Return True when the outcome is semantics-preserving (BC).

    Used to select the correct ``semantics-preserving="true/false"`` rule
    from the XML rule set.
    """
    return outcome.is_backward_compatible
