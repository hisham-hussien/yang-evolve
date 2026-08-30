#!/usr/bin/env python3
"""
generate_concise_reports.py

Walks the 'yang_comparator_reports' directory, finds every final_report.json,
and writes a sibling concise_final_report.json with the following rules:

Rules for condensing diff entries:
  - "added" action   → strip 'attributes' and 'constraints'; keep only the main
                        node fields (level, path, keyword, action, line_number, file).
  - "deleted" action → same as "added": strip 'attributes' and 'constraints'.
  - "changed" action → keep the entry as-is, including all 'attributes' and
                        'constraints' children (they ARE the actual diff detail).
  - "renamed" detection (applied before the above, two cases):
      Case 1 – node-path changed:
        If a "changed" entry's 'attributes' list contains any child with
        attribute == "node-path" and action == "changed", then:
          • The parent action is changed from "changed" → "renamed".
          • Any child attribute with attribute == "name" is removed.
          • path is reconstructed as: module_name + "/" + node-path old_value
      Case 2 – name changed (structural keywords only):
        If a "changed" entry's keyword is a structural YANG node (leaf, container,
        list, etc.) and its 'attributes' list contains a child with
        attribute == "name" and action == "changed" (but no node-path change), then:
          • The parent action is changed from "changed" → "renamed".
          • path is reconstructed by dropping the last 2 segments (keyword-type
            label + new-name) and appending the name old_value.
  - Child-path deduplication (applied after condensing, per section):
      For entries with action "added", "deleted", or "renamed", if an entry's
      path is a strict sub-path of another entry's path WITH THE SAME ACTION
      in the same section, the child entry is removed (it is implied by the
      parent). This applies per-action independently.

All top-level sections (compatible, non_compatible, not-found, unmarked,
llm_verification_summary) are preserved in the output.
"""

import json
import sys
from pathlib import Path

# ── Fields to keep for "added" / "deleted" top-level nodes ──────────────────
_STRIP_KEYS = {"attributes", "constraints"}

# ── Actions subject to child-path deduplication ──────────────────────────────
# For "added", "deleted", "renamed", "reallocated": if a parent entry exists,
# all child-path entries of the same action are implied and removed.
_DEDUP_ACTIONS = {"added", "deleted", "renamed", "reallocated"}

# ── Keywords eligible for name=changed rename detection ─────────────────────
# Excludes type/enum/union/import/namespace which are not structural renames.
_NAME_RENAME_KEYWORDS = {
    "leaf", "leaf-list", "container", "list", "grouping",
    "identity", "typedef", "augment", "uses", "rpc",
    "notification", "choice", "case", "anydata", "anyxml",
}


# ── Helper: detect node-path change ─────────────────────────────────────────

def _has_node_path_change(attributes: list) -> bool:
    """Return True if any attribute child has attribute=='node-path' and action=='changed'."""
    for attr in attributes:
        if isinstance(attr, dict) and attr.get("attribute") == "node-path" and attr.get("action") == "changed":
            return True
    return False


def _has_name_change(attributes: list) -> bool:
    """Return True if any attribute child has attribute=='name' and action=='changed'."""
    for attr in attributes:
        if isinstance(attr, dict) and attr.get("attribute") == "name" and attr.get("action") == "changed":
            return True
    return False


def _remove_name_attributes(attributes: list) -> list:
    """Return a new list with 'name' attribute children removed, but ONLY when
    their old_value/new_value are identical to the sibling 'node-path' attribute's
    old_value/new_value.

    Rationale:
    - When a node is renamed/reallocated, the 'name' attribute often just echoes
      the same identifiers as 'node-path' (e.g. both carry old='foo', new='bar').
      In that case 'name' is redundant and should be dropped.
    - When 'name' carries a *different* semantic change (e.g. old='string',
      new='leafref' alongside a node-path rename), it must be kept because it
      represents an independent type/value change.
    """
    # Extract node-path old/new values for comparison
    node_path_old = ""
    node_path_new = ""
    for a in attributes:
        if isinstance(a, dict) and a.get("attribute") == "node-path" and a.get("action") == "changed":
            node_path_old = (a.get("old_value") or "").strip()
            node_path_new = (a.get("new_value") or "").strip()
            break

    result = []
    for a in attributes:
        if not (isinstance(a, dict) and a.get("attribute") == "name"):
            result.append(a)
            continue
        # It's a 'name' attribute — decide whether to drop it.
        name_old = (a.get("old_value") or "").strip()
        name_new = (a.get("new_value") or "").strip()
        # Drop only when the name values are identical to the node-path values
        # (i.e. the name change is purely a side-effect of the path rename).
        # Use last-segment comparison for node-path since it may be a full path.
        node_path_old_last = node_path_old.split("/")[-1] if node_path_old else node_path_old
        node_path_new_last = node_path_new.split("/")[-1] if node_path_new else node_path_new
        if name_old == node_path_old_last and name_new == node_path_new_last:
            # Redundant — drop it
            continue
        # Different values — keep it (independent semantic change)
        result.append(a)
    return result


# ── Path reconstruction helpers ──────────────────────────────────────────────

def _build_old_path_from_node_path(path_field, old_value: str):
    """
    For Case 1 (node-path changed): reconstruct OLD path as:
        <module_name> + "/" + <old_value>
    Works for both string and list path fields.
    """
    if not old_value:
        return path_field

    def _fix_one(path: str) -> str:
        if not path:
            return path
        module = path.split("/")[0]
        return module + "/" + old_value

    if isinstance(path_field, list):
        return [_fix_one(str(p)) for p in path_field]
    return _fix_one(str(path_field))


def _build_old_path_from_name(path_field, old_value: str):
    """
    For Case 2 (name changed): reconstruct OLD path by dropping the last 2
    segments (keyword-type label + new-name) and appending old_value.

    Example:
      "oc-td/terminal-optical-channel-top/container/optical-channel", old="optical-channels"
        → "oc-td/terminal-optical-channel-top/optical-channels"
    Works for both string and list path fields.
    """
    if not old_value:
        return path_field

    def _fix_one(path: str) -> str:
        if not path:
            return path
        parts = path.split("/")
        # Drop last 2 segments (keyword-type + new-name), keep module + parent segments
        if len(parts) >= 3:
            parts = parts[:-2]
        elif len(parts) == 2:
            parts = parts[:1]
        return "/".join(parts) + "/" + old_value

    if isinstance(path_field, list):
        return [_fix_one(str(p)) for p in path_field]
    return _fix_one(str(path_field))


# ── Entry condensing ─────────────────────────────────────────────────────────

# ── Keywords for which a pure-node-path reallocated/renamed entry is dropped ─
# A "type/reallocated" entry whose ONLY attribute is "node-path" is implied by
# the parent structural node's reallocated entry and adds no new information.
# "union" is excluded because union type changes carry independent semantics.
_DROP_PURE_NODEPATH_KEYWORDS = {"type"}


def condense_entry(entry: dict) -> tuple:
    """
    Return (condensed_entry, renamed_flag, name_attrs_removed_count, drop_entry).

    renamed_flag: True if this entry was reclassified as "renamed".
    name_attrs_removed_count: number of "name" attribute children removed.
    drop_entry: True if the entire entry should be dropped from the output.
                This happens for keyword=="type" (not "union") reallocated/renamed
                entries whose ONLY attribute is "node-path" (pure structural move
                with no independent semantic change).
    """
    action = entry.get("action", "")

    if action in ("added", "deleted"):
        # Keep the entry as-is (attributes/constraints may contain important detail)
        return entry, False, 0, False

    if action == "changed":
        attributes = entry.get("attributes", [])

        if isinstance(attributes, list):
            # ── Case 1: node-path changed ────────────────────────────────────
            if _has_node_path_change(attributes):
                old_value = ""
                new_value_np = ""
                for a in attributes:
                    if isinstance(a, dict) and a.get("attribute") == "node-path":
                        old_value = a.get("old_value", "") or ""
                        new_value_np = a.get("new_value", "") or ""
                        break

                # Determine renamed vs reallocated based on which path segment changed:
                # - renamed:     only the last segment changed (parent path is the same)
                #                → remove 'node-path' attribute, keep 'name' and others
                # - reallocated: any non-last segment changed (parent path changed)
                #                → remove 'name' attribute (when values echo node-path),
                #                  keep 'node-path' and other semantic attributes
                old_parent = "/".join(old_value.split("/")[:-1]) if "/" in old_value else ""
                new_parent = "/".join(new_value_np.split("/")[:-1]) if "/" in new_value_np else ""
                sub_action = "renamed" if old_parent == new_parent else "reallocated"

                if sub_action == "renamed":
                    # ── Renamed: strip node-path, keep name + other attrs ────
                    # The 'node-path' attribute is already captured by the
                    # 'renamed' action and the reconstructed path — remove it.
                    # Keep 'name' (it IS the rename signal) and any other attrs
                    # (e.g. 'base/changed' for identity renames).
                    final_attrs = [
                        a for a in attributes
                        if not (isinstance(a, dict) and a.get("attribute") == "node-path")
                    ]
                    removed_count = len(attributes) - len(final_attrs)
                    new_entry = dict(entry)
                    new_entry["action"] = "renamed"
                    new_entry["attributes"] = final_attrs
                    new_entry["path"] = _build_old_path_from_node_path(entry.get("path", ""), old_value)
                    return new_entry, True, removed_count, False

                else:
                    # ── Reallocated: keep node-path, handle name and other attrs ─
                    #
                    # The 'node-path' attribute IS the primary semantic content for
                    # reallocated entries — it tells you where the node moved.
                    # Keep it unless a 'name' attribute carries an INDEPENDENT
                    # semantic change (values differ from node-path values), in which
                    # case 'name' is the primary change and 'node-path' is redundant.
                    #
                    # Two sub-cases:
                    #
                    # A) 'name' values MATCH node-path values (redundant echo):
                    #    → remove 'name', keep 'node-path' + other attrs/constraints
                    #    → action = 'reallocated'
                    #    → For 'type' keyword with ONLY node-path (no other attrs/
                    #      constraints): drop the entry entirely.
                    #
                    # B) 'name' values DIFFER from node-path values (independent change,
                    #    e.g. uint32→uint64 alongside a path relocation):
                    #    → remove 'node-path' (redundant relative to 'name'), keep 'name'
                    #    → action derived from remaining children
                    constraints = entry.get("constraints", [])

                    # Check if 'name' has independent values (differs from node-path)
                    has_independent_name = _has_independent_name_change(entry)

                    if has_independent_name:
                        # Sub-case B: name is independent → strip node-path, keep name
                        non_nodepath_attrs = [
                            a for a in attributes
                            if not (isinstance(a, dict) and a.get("attribute") == "node-path")
                        ]
                        removed_count = len(attributes) - len(non_nodepath_attrs)
                        child_actions = set()
                        for a in non_nodepath_attrs:
                            if isinstance(a, dict) and a.get("action"):
                                child_actions.add(a["action"])
                        for c in (constraints if isinstance(constraints, list) else []):
                            if isinstance(c, dict) and c.get("action"):
                                child_actions.add(c["action"])
                        if len(child_actions) == 1:
                            derived_action = next(iter(child_actions))
                        elif child_actions:
                            derived_action = "changed"
                        else:
                            derived_action = "changed"
                        new_entry = dict(entry)
                        new_entry["action"] = derived_action
                        new_entry["attributes"] = non_nodepath_attrs
                        new_entry["path"] = _build_old_path_from_node_path(entry.get("path", ""), old_value)
                        return new_entry, True, removed_count, False

                    else:
                        # Sub-case A: name is redundant (or absent) → strip name, keep node-path
                        cleaned_attrs = _remove_name_attributes(attributes)
                        removed_count = len(attributes) - len(cleaned_attrs)

                        # Check if only node-path remains (pure structural move)
                        non_nodepath_attrs = [
                            a for a in cleaned_attrs
                            if not (isinstance(a, dict) and a.get("attribute") == "node-path")
                        ]

                        if not non_nodepath_attrs and not constraints:
                            # Pure structural move — only node-path remains.
                            # For 'type' keyword (excluding 'union'): drop the entry.
                            # For all other keywords: keep with 'reallocated' action.
                            kw_c1 = entry.get("keyword", "")
                            if isinstance(kw_c1, list):
                                kw_c1 = kw_c1[0] if kw_c1 else ""
                            if kw_c1 in _DROP_PURE_NODEPATH_KEYWORDS:
                                return entry, False, removed_count, True
                            new_entry = dict(entry)
                            new_entry["action"] = "reallocated"
                            new_entry["attributes"] = cleaned_attrs
                            new_entry["path"] = _build_old_path_from_node_path(entry.get("path", ""), old_value)
                            return new_entry, True, removed_count, False
                        else:
                            # Other attrs/constraints remain alongside node-path.
                            # Keep node-path + other attrs + constraints, action = 'reallocated'.
                            new_entry = dict(entry)
                            new_entry["action"] = "reallocated"
                            new_entry["attributes"] = cleaned_attrs
                            new_entry["path"] = _build_old_path_from_node_path(entry.get("path", ""), old_value)
                            return new_entry, True, removed_count, False

            # ── Case 2: name changed (structural keywords only) ───────────────
            kw = entry.get("keyword", "")
            if isinstance(kw, list):
                kw = kw[0] if kw else ""
            if kw in _NAME_RENAME_KEYWORDS and _has_name_change(attributes):
                old_value = ""
                new_value_name = ""
                for a in attributes:
                    if isinstance(a, dict) and a.get("attribute") == "name" and a.get("action") == "changed":
                        old_value = a.get("old_value", "") or ""
                        new_value_name = a.get("new_value", "") or ""
                        break

                # name change always means the node itself was renamed (last segment)
                new_entry = dict(entry)
                new_entry["action"] = "renamed"
                new_entry["path"] = _build_old_path_from_name(entry.get("path", ""), old_value)
                return new_entry, True, 0, False

        # ── Normal changed: keep as-is ───────────────────────────────────────
        return entry, False, 0, False

    if action in ("reallocated", "renamed"):
        # ── Case 3: reallocated/renamed already set by final_report.json ─────
        # The flag mode may produce entries where action is already "reallocated"
        # or "renamed" and the attributes list contains BOTH a "node-path" child
        # AND other semantic children (e.g. any attribute/constraint with
        # old_value/new_value representing an independent change such as
        # uint32 → uint64 for a type, or a description change, etc.).
        #
        # Rule:
        #   1. Remove the "node-path" attribute — it is the cause of the
        #      reallocated/renamed classification and is already captured by the
        #      parent action + path fields.
        #   2. If other attributes/constraints remain, derive the new parent
        #      action directly from those remaining children:
        #        - Collect all unique child actions.
        #        - If only one distinct action → use it as the parent action.
        #        - If mixed actions → use "changed" as the most general action.
        #   3. If no other attributes/constraints remain:
        #        - For keyword == "type" (excluding "union"): DROP the entire
        #          entry — it is a pure structural move already implied by the
        #          parent node's reallocated/renamed entry.
        #        - For all other keywords: keep the entry with the original
        #          action (pure structural move with no extra semantic detail).
        attributes = entry.get("attributes", [])
        constraints = entry.get("constraints", [])
        if not isinstance(attributes, list):
            attributes = []

        has_node_path = any(
            isinstance(a, dict) and a.get("attribute") == "node-path"
            for a in attributes
        )
        if not has_node_path:
            # No node-path attribute — nothing to strip, pass through unchanged
            return entry, False, 0, False

        # Remove node-path attribute(s)
        remaining_attrs = [
            a for a in attributes
            if not (isinstance(a, dict) and a.get("attribute") == "node-path")
        ]
        removed_count = len(attributes) - len(remaining_attrs)

        if not remaining_attrs and not constraints:
            # Pure structural move — no independent semantic change remains.
            # For "type" keyword (excluding "union"): drop the entire entry.
            # For all other keywords: keep with original action.
            kw_check = entry.get("keyword", "")
            if isinstance(kw_check, list):
                kw_check = kw_check[0] if kw_check else ""
            if kw_check in _DROP_PURE_NODEPATH_KEYWORDS:
                # Signal caller to drop this entry entirely
                return entry, False, removed_count, True
            # Other keywords: keep with original action, strip node-path attr
            new_entry = dict(entry)
            new_entry["attributes"] = remaining_attrs
            return new_entry, False, removed_count, False

        # Derive the new parent action from the remaining children.
        # Collect all child actions from remaining attributes and constraints.
        child_actions = set()
        for a in remaining_attrs:
            if isinstance(a, dict) and a.get("action"):
                child_actions.add(a["action"])
        for c in (constraints if isinstance(constraints, list) else []):
            if isinstance(c, dict) and c.get("action"):
                child_actions.add(c["action"])

        if len(child_actions) == 1:
            # All remaining children share the same action → use it directly
            new_parent_action = next(iter(child_actions))
        elif child_actions:
            # Mixed actions among children → "changed" is the most general
            new_parent_action = "changed"
        else:
            # No child actions found → fall back to "changed"
            new_parent_action = "changed"

        new_entry = dict(entry)
        new_entry["action"] = new_parent_action
        new_entry["attributes"] = remaining_attrs
        return new_entry, False, removed_count, False

    # Unknown action – pass through unchanged
    return entry, False, 0, False


# ── Grouping-expansion deduplication ─────────────────────────────────────────

def _entry_source_key(entry: dict) -> tuple:
    """
    Build a key that identifies the SOURCE change (ignoring which grouping path
    it was expanded into).  Entries with the same key are the same YANG source
    change reported at multiple grouping-expansion paths.

    Key components:
      (keyword, action, file, line_number, node-path-old-last-segment, node-path-new-last-segment)

    We use only the LAST SEGMENT of the node-path old/new values because different
    grouping expansions produce different full paths but the same leaf name.
    All fields are normalized to strings/ints to ensure hashability.
    """
    kw = entry.get("keyword", "")
    if isinstance(kw, list):
        kw = kw[0] if kw else ""
    kw = str(kw)

    action = str(entry.get("action", ""))
    file_ = str(entry.get("file", ""))

    # line_number can be a list in multi-path entries — take first element
    ln = entry.get("line_number") or entry.get("line_number_old") or entry.get("line_number_new") or 0
    if isinstance(ln, list):
        ln = ln[0] if ln else 0
    try:
        ln = int(ln)
    except (TypeError, ValueError):
        ln = 0

    # Extract node-path old/new from attributes (for renamed entries)
    # Use only the last segment to handle grouping expansion differences
    np_old_last = np_new_last = ""
    attrs = entry.get("attributes", []) or []
    for a in attrs:
        if isinstance(a, dict) and a.get("attribute") == "node-path":
            old_val = a.get("old_value", "") or ""
            new_val = a.get("new_value", "") or ""
            # old_val can be a list in multi-path entries
            if isinstance(old_val, list):
                old_val = old_val[0] if old_val else ""
            if isinstance(new_val, list):
                new_val = new_val[0] if new_val else ""
            np_old_last = str(old_val).split("/")[-1] if old_val else ""
            np_new_last = str(new_val).split("/")[-1] if new_val else ""
            break

    return (kw, action, file_, ln, np_old_last, np_new_last)


def merge_grouping_expansions(entries: list) -> tuple:
    """
    Merge entries that represent the same source change expanded to multiple
    grouping paths into a single entry with a list of paths (and levels).

    Only merges entries where the source key is non-trivial (has a file and
    line_number) to avoid false merges.

    Returns (merged_list, merged_count).
    """
    from collections import OrderedDict

    seen: OrderedDict = OrderedDict()  # key -> first entry (representative)
    merged_count = 0

    for entry in entries:
        key = _entry_source_key(entry)
        # Only merge if we have enough signal (file + line_number)
        file_ = entry.get("file", "")
        ln = entry.get("line_number") or entry.get("line_number_old") or entry.get("line_number_new") or 0
        # Fallback: use line_number from the first constraint or attribute child
        # (e.g. type deleted entries whose line_number is on the constraint, not the parent)
        if not ln:
            for child in (entry.get("constraints") or []) + (entry.get("attributes") or []):
                child_ln = child.get("line_number") or child.get("line_number_old") or 0
                if child_ln:
                    ln = child_ln
                    break
        if not file_ or not ln:
            # Not enough signal — keep as-is
            unique_key = id(entry)
            seen[unique_key] = entry
            continue

        if key not in seen:
            seen[key] = dict(entry)  # copy
        else:
            # Merge: add this path to the existing entry's path list
            rep = seen[key]
            existing_path = rep.get("path", "")
            new_path = entry.get("path", "")
            existing_level = rep.get("level", "")
            new_level = entry.get("level", "")

            # Convert to lists if not already
            if not isinstance(existing_path, list):
                existing_path = [existing_path] if existing_path else []
            if not isinstance(existing_level, list):
                existing_level = [existing_level] if existing_level else []

            new_paths = [new_path] if isinstance(new_path, str) else new_path
            new_levels = [new_level] if isinstance(new_level, str) else new_level

            for p in new_paths:
                if p and p not in existing_path:
                    existing_path.append(p)
            for lv in new_levels:
                if lv and lv not in existing_level:
                    existing_level.append(lv)

            rep["path"] = existing_path
            rep["level"] = existing_level
            merged_count += 1

    return list(seen.values()), merged_count


# ── Path helpers for deduplication ───────────────────────────────────────────

def _get_paths(entry: dict) -> list:
    """Return the list of path strings for an entry (path can be str or list)."""
    p = entry.get("path", "")
    if isinstance(p, list):
        return [str(x) for x in p if x]
    return [str(p)] if p else []


def _is_child_of_any(candidate_paths: list, parent_paths_set: set) -> bool:
    """
    Return True if ANY of candidate_paths is a strict sub-path of ANY path
    in parent_paths_set (i.e., B starts with A + "/").
    """
    for cpath in candidate_paths:
        for ppath in parent_paths_set:
            if cpath.startswith(ppath + "/"):
                return True
    return False


# ── Keywords that are removed when their parent is deleted ───────────────────
# Only "type" (and sub-type keywords) are removed when the parent leaf/container
# is already reported as deleted. All other structural keywords are kept.
_DELETED_CHILD_REMOVE_KEYWORDS = {"type", "enum", "union"}


def _all_paths_are_children(candidate_paths: list, parent_paths_set: set) -> bool:
    """
    Return True only if ALL candidate paths are children of some path in parent_paths_set.
    Used for reallocated entries: only remove if ALL paths are implied by parents.
    """
    if not candidate_paths:
        return False
    return all(
        any(cpath.startswith(ppath + "/") for ppath in parent_paths_set)
        for cpath in candidate_paths
    )


def _has_independent_name_change(entry: dict) -> bool:
    """Return True if the entry has a 'name' attribute whose old/new values differ
    from the sibling 'node-path' attribute's last segment values.

    Such a 'name' attribute represents an independent semantic change (e.g. a type
    change from 'string' to 'leafref') that must be preserved even when the entry's
    path is a child of a parent reallocated entry.
    """
    attrs = entry.get("attributes", [])
    if not isinstance(attrs, list):
        return False

    # Extract node-path last segments
    node_path_old_last = ""
    node_path_new_last = ""
    for a in attrs:
        if isinstance(a, dict) and a.get("attribute") == "node-path" and a.get("action") == "changed":
            old_val = (a.get("old_value") or "").strip()
            new_val = (a.get("new_value") or "").strip()
            node_path_old_last = old_val.split("/")[-1] if old_val else ""
            node_path_new_last = new_val.split("/")[-1] if new_val else ""
            break

    # Check if any 'name' attribute has values different from node-path last segments
    for a in attrs:
        if not (isinstance(a, dict) and a.get("attribute") == "name"):
            continue
        name_old = (a.get("old_value") or "").strip()
        name_new = (a.get("new_value") or "").strip()
        if name_old != node_path_old_last or name_new != node_path_new_last:
            return True
    return False


def _node_identity_key(entry: dict, use_new_line: bool = False) -> tuple:
    """
    Build a (keyword, line_number) identity key for a node entry.

    Used to detect 'deleted' or 'added' entries that represent the same
    physical node as a 'renamed' or 'reallocated' entry — just seen from a
    different grouping expansion path.

    Args:
        entry: The report entry dict.
        use_new_line: If True, prefer line_number_new (for 'added' entries
                      which reference the new file's line number).
                      If False, prefer line_number_old (for 'deleted' and
                      'renamed'/'reallocated' entries which reference the
                      old file's line number).
    """
    kw = entry.get("keyword", "")
    if isinstance(kw, list):
        kw = kw[0] if kw else ""
    kw = str(kw)

    if use_new_line:
        ln = (entry.get("line_number_new")
              or entry.get("line_number")
              or entry.get("line_number_old")
              or 0)
    else:
        ln = (entry.get("line_number_old")
              or entry.get("line_number")
              or entry.get("line_number_new")
              or 0)
    if isinstance(ln, list):
        ln = ln[0] if ln else 0
    try:
        ln = int(ln)
    except (TypeError, ValueError):
        ln = 0

    return (kw, ln)


def deduplicate_section(entries: list) -> tuple:
    """
    Remove child-path entries implied by a parent entry of the same action.

    Rules:
    - For "added", "renamed": remove if ANY path is a child of a parent entry.
    - For "reallocated": remove only if ALL paths are children of parent entries
      of the same action OR of a "renamed" parent entry (since a type being
      reallocated purely because its parent leaf was renamed carries no new
      information — the relocation is implied by the rename).
    - For "deleted": only remove child-path entries whose keyword is in
      _DELETED_CHILD_REMOVE_KEYWORDS (type/enum/union). All other keywords
      (leaf, container, list, grouping, identity, typedef, etc.) are kept
      even if their parent is also deleted.
    - For "deleted" or "added" with the same (keyword, line_number) as a
      "renamed" or "reallocated" entry: remove unconditionally.  These are
      the same physical node seen from a different grouping expansion path —
      the rename/reallocation already captures the change; the deleted/added
      entry is redundant noise from nested grouping expansion.

    Returns (deduped_list, removed_count).
    """
    action_paths: dict = {}
    for e in entries:
        action = e.get("action", "")
        if action not in _DEDUP_ACTIONS:
            continue
        for p in _get_paths(e):
            action_paths.setdefault(action, set()).add(p)

    # Build a combined set of "renamed" + "reallocated" paths for cross-action
    # child-path checks.  A "reallocated" type entry whose ALL paths are children
    # of a "renamed" leaf entry is implied by the rename and should be removed.
    renamed_paths: set = action_paths.get("renamed", set())

    # Build the set of paths for "reallocated" entries that carry a node-path
    # change.  When a child "reallocated" entry's paths are all children of a
    # parent "reallocated" entry that ALSO has a node-path change, the child's
    # node-path change is implied by the parent's — the child is redundant.
    # Example: lsp-name (leaf, reallocated, node-path changed) implies
    #          lsp-name/type (type, reallocated, node-path changed).
    reallocated_nodepath_paths: set = set()
    for e in entries:
        if e.get("action") == "reallocated" and _has_node_path_change(e.get("attributes", [])):
            for p in _get_paths(e):
                reallocated_nodepath_paths.add(p)

    # Collect identity keys from all renamed/reallocated entries.
    # We store TWO keys per entry:
    #   - (keyword, line_number_old) → matches 'deleted' entries (old file line)
    #   - (keyword, line_number_new) → matches 'added' entries (new file line)
    # Any 'deleted' or 'added' entry sharing the same key is the same physical
    # node seen from a different grouping expansion path and should be suppressed
    # — the rename/reallocation already captures the change.
    moved_node_keys_old: set = set()  # for matching 'deleted' entries
    moved_node_keys_new: set = set()  # for matching 'added' entries
    for e in entries:
        if e.get("action") in ("renamed", "reallocated"):
            key_old = _node_identity_key(e, use_new_line=False)
            key_new = _node_identity_key(e, use_new_line=True)
            if key_old[1]:
                moved_node_keys_old.add(key_old)
            if key_new[1]:
                moved_node_keys_new.add(key_new)

    deduped = []
    removed = 0
    for e in entries:
        action = e.get("action", "")
        if action not in _DEDUP_ACTIONS:
            deduped.append(e)
            continue

        # Suppress 'deleted' or 'added' entries that are the same physical node
        # as a 'renamed' or 'reallocated' entry (same keyword + source line number).
        # These arise from nested grouping expansion: the comparator sees the node
        # as deleted/added in some grouping paths and renamed/reallocated in others,
        # but they all refer to the same physical YANG definition.
        # - 'deleted' entries use the OLD file line number
        # - 'added' entries use the NEW file line number
        if action == "deleted":
            key = _node_identity_key(e, use_new_line=False)
            if key[1] and key in moved_node_keys_old:
                removed += 1
                continue
        elif action == "added":
            key = _node_identity_key(e, use_new_line=True)
            if key[1] and key in moved_node_keys_new:
                removed += 1
                continue

        candidate_paths = _get_paths(e)
        parent_set = action_paths.get(action, set())

        if action == "reallocated":
            # For reallocated: remove if ALL paths are children of same-action
            # parents OR of renamed parents (type relocated because parent leaf
            # was renamed — no independent change introduced).
            combined_parent_set = parent_set | renamed_paths
            is_redundant = _all_paths_are_children(candidate_paths, combined_parent_set)
        else:
            is_redundant = _is_child_of_any(candidate_paths, parent_set)

        if is_redundant:
            if action == "deleted":
                # For deleted: only remove type/enum/union sub-entries that are
                # direct children of a deleted parent (e.g. leaf/type, leaf/enum).
                # EXCEPTION: do NOT remove 'type' entries that are union member
                # types (path contains '/union/'), enum member types, or typedef
                # types — these carry independent semantic information about which
                # union member / enum value / typedef was deleted.
                kw = e.get("keyword", "")
                if isinstance(kw, list):
                    kw = kw[0] if kw else ""
                kw_lower = str(kw).lower()
                if kw_lower in _DELETED_CHILD_REMOVE_KEYWORDS:
                    # Check if this is a union member type (path contains /union/)
                    # or an enum member type (path contains /enum/) — keep those.
                    paths = _get_paths(e)
                    is_union_or_enum_member = any(
                        "/union/" in str(p) or "/enum/" in str(p)
                        for p in paths
                    )
                    if is_union_or_enum_member:
                        # Union/enum member type — keep it (carries independent info)
                        deduped.append(e)
                    else:
                        removed += 1
                        continue  # remove this entry
                else:
                    # All other keywords (leaf, container, grouping, etc.) are kept
                    deduped.append(e)
            elif action == "reallocated" and _has_independent_name_change(e):
                # For reallocated: keep entries that carry an independent semantic
                # change in a 'name' attribute (e.g. type changed from 'string' to
                # 'leafref' alongside a path relocation).  Such entries are NOT
                # implied by the parent reallocated entry — they carry extra info.
                deduped.append(e)
            elif action == "reallocated" and _has_node_path_change(e.get("attributes", [])):
                # For reallocated: only remove 'type' keyword entries whose paths
                # are all children of a parent 'reallocated' entry that ALSO has a
                # node-path change.  All other keywords (leaf, container, list,
                # grouping, etc.) are kept even if their parent is also reallocated
                # — they carry independent semantic information about the relocation.
                # Example: lsp-name/type (type, reallocated) is implied by
                #          lsp-name (leaf, reallocated) → remove.
                # Example: mode-descriptor (list, reallocated) is NOT implied by
                #          operational-modes (container, renamed) → keep.
                kw = e.get("keyword", "")
                if isinstance(kw, list):
                    kw = kw[0] if kw else ""
                if (str(kw).lower() == "type"
                        and _all_paths_are_children(candidate_paths, reallocated_nodepath_paths)):
                    # type child of a reallocated parent → implied, remove
                    removed += 1
                else:
                    deduped.append(e)
            else:
                # For added/renamed/reallocated: remove redundant child-path entries
                removed += 1
        else:
            deduped.append(e)

    return deduped, removed


# ── Section and report condensing ────────────────────────────────────────────

def _split_changed_multipath_entries(entries: list) -> tuple:
    """
    Split 'changed' entries that have a list of paths into separate per-path entries,
    but ONLY when the paths represent genuinely different schema nodes (not just
    grouping-expansion duplicates of the same node).

    Background: when a 'uses' statement has a 'when' refinement, pyang attaches
    the 'when' to the expanded node.  Before the node_normalizer.py fix, all
    instantiations of the same grouping got the same line number (the grouping
    definition line), causing them to be merged even when they represent different
    schema nodes (e.g. encapsulation/config/control-word vs
    encapsulation/state/control-word).

    After the node_normalizer.py fix, each instantiation gets a unique line number
    (the 'uses' statement line), so they will NOT be merged by normalize_item_for_grouping.
    This function only needs to correct existing final_report.json files generated
    before the fix.

    Heuristic for "different schema nodes":
      The paths have different PARENT segments (second-to-last path component).
      Example: "encapsulation/config/control-word" vs "encapsulation/state/control-word"
        → parent segments are "config" and "state" → different → split.
      Example: "instance-config/route-distinguisher" vs "network-instance-top/.../route-distinguisher"
        → both end in "route-distinguisher" with same parent "config" → same node → keep merged.

    Returns (expanded_list, split_count).
    """
    def _parent_seg(p: str) -> str:
        parts = [s for s in str(p).split("/") if s]
        return parts[-2] if len(parts) >= 2 else ""

    result = []
    split_count = 0

    for entry in entries:
        action = entry.get("action", "")
        path = entry.get("path", "")
        level = entry.get("level", "")

        # Only split 'changed' entries with multiple paths
        if action != "changed" or not isinstance(path, list) or len(path) <= 1:
            result.append(entry)
            continue

        # Check if paths represent different schema nodes by comparing parent segments.
        parent_segs = set(_parent_seg(p) for p in path)

        # If all paths have the same parent segment, they are grouping-expansion
        # duplicates of the same node → keep merged (do not split).
        if len(parent_segs) <= 1:
            result.append(entry)
            continue

        # Different parent segments → different schema nodes → split into per-path entries.
        levels = level if isinstance(level, list) else [level] * len(path)
        while len(levels) < len(path):
            levels.append(levels[-1] if levels else "")

        for i, p in enumerate(path):
            new_entry = dict(entry)
            new_entry["path"] = p
            new_entry["level"] = levels[i] if i < len(levels) else ""

            # Also split per-path level values inside constraints and attributes
            for collection_name in ("constraints", "attributes"):
                collection = entry.get(collection_name)
                if not isinstance(collection, list):
                    continue
                new_collection = []
                for child in collection:
                    new_child = dict(child)
                    child_level = child.get("level", "")
                    if isinstance(child_level, list) and len(child_level) > i:
                        new_child["level"] = child_level[i]
                    elif isinstance(child_level, list) and child_level:
                        new_child["level"] = child_level[-1]
                    new_collection.append(new_child)
                new_entry[collection_name] = new_collection

            result.append(new_entry)

        split_count += len(path) - 1  # net new entries added

    return result, split_count


def condense_section(entries: list) -> tuple:
    """
    Apply condense_entry to every item in a section list, then:
      1. Split incorrectly-merged 'changed' multi-path entries into per-path entries.
      2. Merge grouping-expansion duplicates (same source change, different paths).
      3. Deduplicate child-path entries for renamed actions.
    Returns (condensed_list, total_renamed, total_name_removed, total_deduped, total_merged).
    """
    result = []
    total_renamed = 0
    total_name_removed = 0
    total_dropped = 0
    for e in entries:
        condensed, renamed, name_removed, drop = condense_entry(e)
        if drop:
            total_dropped += 1
            continue
        result.append(condensed)
        if renamed:
            total_renamed += 1
        total_name_removed += name_removed

    # Split 'changed' entries that were incorrectly merged across different paths.
    # This corrects the group_duplicate_changes step which merges by source line
    # number without considering that 'changed' entries at different schema paths
    # are distinct changes (e.g. encapsulation/config/X vs encapsulation/state/X).
    result, _split = _split_changed_multipath_entries(result)

    # Merge grouping-expansion duplicates first
    result, merged = merge_grouping_expansions(result)

    # Then deduplicate child-path entries
    result, deduped = deduplicate_section(result)
    return result, total_renamed, total_name_removed, deduped + merged, merged


def _collect_type_changed_parent_paths(entries: list) -> set:
    """
    Collect the parent paths of all 'type changed' entries.

    When a type changes from one typedef to another (e.g. inet:ip-address →
    inet:ip-address-no-zone), the comparator expands both typedefs and reports
    their union members as deleted/added.  These union member entries are
    sub-paths of the parent type node (e.g. .../address/type/union/inet:ipv4-address
    is a child of .../address/type/).

    This function returns the set of parent paths (one level up from the
    type-changed path) so that cross-section deduplication can remove the
    union member noise from non_compatible.

    Example:
        type changed path: .../address/type/inet:ip-address-no-zone
        parent path:       .../address/type
        union member path: .../address/type/union/inet:ipv4-address  ← child of parent
    """
    parent_paths: set = set()
    for e in entries:
        if e.get("keyword") != "type" or e.get("action") != "changed":
            continue
        for p in _get_paths(e):
            # Strip the last segment (the type name) to get the parent path
            parent = p.rsplit("/", 1)[0] if "/" in p else p
            if parent:
                parent_paths.add(parent)
    return parent_paths


def _strip_type_prefix(name: str) -> str:
    """Strip module prefix from a YANG type name (e.g. 'inet:ipv4-address' → 'ipv4-address')."""
    if name and ':' in name:
        return name.split(':', 1)[1]
    return name or ""


def _remove_type_expansion_noise(non_compat: list, compat_type_parent_paths: set,
                                  compat_entries: list = None) -> tuple:
    """
    Remove from non_compatible any 'type deleted'/'type added' entries that are
    union member expansion artifacts of a type change already reported in compatible.

    These entries have paths like .../type/union/inet:ipv4-address which are
    children of a .../type path that already has a 'type changed' entry in
    compatible.  Since the overall type change is BC, the union member deletions
    are noise — they're the internal expansion of the typedef comparison.

    Also handles the prefix/namespace migration case: when a union member type
    is renamed from one module's prefix to another (e.g. 'inet:ipv4-address' →
    'ipv4-address' from openconfig-inet-types), the comparator reports the old
    member as 'deleted' (NBC) and the new member as 'added' (compatible).  If
    the local name (after stripping the prefix) matches between a deleted NBC
    entry and an added compatible entry at the same parent path, the deletion is
    a false positive and should be removed.

    Only removes entries where keyword='type' and action in ('deleted', 'added')
    and ALL paths are children of a compatible type-changed parent path.

    Returns (filtered_list, removed_count).
    """
    if not compat_type_parent_paths and not compat_entries:
        return non_compat, 0

    # Build a set of (parent_path, local_name) for union member 'added' entries
    # in the compatible section — used to detect prefix-only renames.
    compat_union_added_local: set = set()
    if compat_entries:
        for ce in compat_entries:
            ce_kw = ce.get("keyword", "")
            if isinstance(ce_kw, list):
                ce_kw = ce_kw[0] if ce_kw else ""
            if str(ce_kw).lower() != "type" or ce.get("action") != "added":
                continue
            for cp in _get_paths(ce):
                cp_str = str(cp)
                if "/union/" in cp_str:
                    # Extract local name from the last path segment
                    local = _strip_type_prefix(cp_str.rsplit("/", 1)[-1])
                    # Parent path = everything up to and including /union
                    parent = cp_str.rsplit("/", 1)[0]  # e.g. .../type/union
                    compat_union_added_local.add((parent, local))

    filtered = []
    removed = 0
    for e in non_compat:
        kw = e.get("keyword", "")
        if isinstance(kw, list):
            kw = kw[0] if kw else ""
        action = e.get("action", "")

        # Only consider type deleted/added entries (union member expansion artifacts)
        if str(kw).lower() != "type" or action not in ("deleted", "added"):
            filtered.append(e)
            continue

        candidate_paths_early = _get_paths(e)
        is_union_member = any("/union/" in str(p) for p in candidate_paths_early)
        has_constraints = bool(e.get("constraints"))
        attrs = e.get("attributes") or []
        # 'name' attribute alone is not meaningful — it just identifies the type
        non_name_attrs = [a for a in attrs if a.get("attribute") != "name"]
        has_meaningful_attrs = bool(non_name_attrs)

        if is_union_member:
            # Check if this is a prefix-only rename: the local name of the deleted
            # union member matches an added union member in the compatible section
            # at the same parent path.  If so, it's a false positive.
            is_prefix_only_rename = False
            if compat_union_added_local and action == "deleted":
                for p in candidate_paths_early:
                    p_str = str(p)
                    if "/union/" not in p_str:
                        continue
                    local = _strip_type_prefix(p_str.rsplit("/", 1)[-1])
                    parent = p_str.rsplit("/", 1)[0]
                    if (parent, local) in compat_union_added_local:
                        is_prefix_only_rename = True
                        break
            if is_prefix_only_rename:
                removed += 1
                continue
            # Not a prefix-only rename — keep if it has constraints or meaningful attrs
            if has_constraints or has_meaningful_attrs:
                filtered.append(e)
                continue
            # Union member with no constraints/attrs: keep unless parent path is covered
            candidate_paths = _get_paths(e)
            if _all_paths_are_children(candidate_paths, compat_type_parent_paths):
                removed += 1
            else:
                filtered.append(e)
            continue

        if has_constraints or has_meaningful_attrs:
            # Entry carries independent content → keep it
            filtered.append(e)
            continue

        candidate_paths = _get_paths(e)
        # Remove if ALL paths are children of a compatible type-changed parent path
        if _all_paths_are_children(candidate_paths, compat_type_parent_paths):
            removed += 1
        else:
            filtered.append(e)

    return filtered, removed


# ── BC-promotion helpers (mirrors group_by_path.py logic) ────────────────────
# When the pyang plugin processes a grouping-level context it may produce a
# false-positive NBC verdict for a 'when'/'must' constraint that was correctly
# classified as BC in another instantiation of the same grouping.  The
# group_by_path.py pipeline corrects this at report-generation time, but
# existing final_report.json files stored on disk were generated before the fix.
# condense_report() applies the same promotion so that concise_final_report.json
# files are also correct.


def _fix_constraint_line_numbers(entries: list) -> list:
    """
    Fix incorrect line numbers on 'added' and 'deleted' constraints/attributes.

    Background: when a constraint (e.g. 'when') is 'added' to a node that was
    itself 'changed' (has both line_number_old and line_number_new), the
    generate_non_compatibility_list / generate_compatibility_list pipeline used
    to fall back to the parent's line_number_old AND line_number_new for the
    child constraint record.  This is wrong:

      - 'added' constraint → only exists in the new file → only line_number_new
        (or a single line_number pointing to the new file)
      - 'deleted' constraint → only exists in the old file → only line_number_old
        (or a single line_number pointing to the old file)
      - 'changed' constraint → exists in both → keep both line_number_old and
        line_number_new

    This function corrects existing final_report.json entries so that
    concise_final_report.json files are also correct without requiring a full
    re-run of the pipeline.
    """
    for entry in entries:
        for collection_name in ("constraints", "attributes"):
            collection = entry.get(collection_name)
            if not isinstance(collection, list):
                continue
            for child in collection:
                if not isinstance(child, dict):
                    continue
                action = child.get("action", "")
                if action == "added":
                    # Added child only exists in new file — remove line_number_old
                    # and convert line_number_new to a single line_number.
                    if "line_number_old" in child and "line_number_new" in child:
                        child["line_number"] = child.pop("line_number_new")
                        del child["line_number_old"]
                    elif "line_number_old" in child and "line_number_new" not in child:
                        # Only old line number present for an added item — wrong,
                        # but keep it as a single line_number (best we can do).
                        child["line_number"] = child.pop("line_number_old")
                elif action == "deleted":
                    # Deleted child only exists in old file — remove line_number_new
                    # and convert line_number_old to a single line_number.
                    if "line_number_old" in child and "line_number_new" in child:
                        child["line_number"] = child.pop("line_number_old")
                        del child["line_number_new"]
                    elif "line_number_new" in child and "line_number_old" not in child:
                        # Only new line number present for a deleted item — wrong,
                        # but keep it as a single line_number (best we can do).
                        child["line_number"] = child.pop("line_number_new")
    return entries

_XPATH_CONDITION_TYPES = {"when", "must"}


def _bc_promo_fp(constraint: dict):
    """Return a fingerprint tuple for a promotable changed when/must constraint, or None."""
    if not isinstance(constraint, dict):
        return None
    action = (constraint.get("action") or "").lower()
    if action != "changed":
        return None
    ctype = (constraint.get("constraint") or "").lower()
    if ctype not in _XPATH_CONDITION_TYPES:
        return None
    old_v = constraint.get("old_value") or ""
    new_v = constraint.get("new_value") or ""
    if not old_v or not new_v:
        return None
    file_ = constraint.get("file") or constraint.get("helper_file") or ""
    if not file_:
        return None
    return (file_, ctype, old_v, new_v)


def _item_promo_fps(item: dict) -> set:
    fps = set()
    for c in item.get("constraints") or []:
        fp = _bc_promo_fp(c)
        if fp:
            fps.add(fp)
    return fps


def _item_is_promotable(item: dict) -> bool:
    constraints = item.get("constraints") or []
    if not constraints:
        return False
    return all(_bc_promo_fp(c) is not None for c in constraints)


def _promote_nbc_to_bc(report: dict) -> tuple:
    """
    Scan the non_compatible section and move false-positive NBC items to compatible.

    An NBC item is a false positive when ALL of its constraints are changed
    when/must conditions (with non-empty old_value and new_value) that are
    already confirmed BC in the compatible section.  This corrects grouping-
    context resolution failures where the pyang plugin could not validate the
    XPath in an abstract grouping path but succeeded in an instantiated path.

    Returns (updated_report, promoted_count).
    """
    compatible = report.get("compatible") or []
    non_compatible = report.get("non_compatible") or []

    # Build BC fingerprint set from compatible section
    bc_fps: set = set()
    for item in compatible:
        bc_fps.update(_item_promo_fps(item))

    if not bc_fps:
        return report, 0

    promoted = 0
    remaining_nbc = []
    promoted_items = []

    for item in non_compatible:
        item_fps = _item_promo_fps(item)
        if (item_fps
                and _item_is_promotable(item)
                and item_fps.issubset(bc_fps)):
            promoted_items.append(item)
            promoted += 1
        else:
            remaining_nbc.append(item)

    if promoted:
        updated = dict(report)
        updated["compatible"] = list(compatible) + promoted_items
        updated["non_compatible"] = remaining_nbc
        return updated, promoted

    return report, 0


def condense_report(report: dict) -> tuple:
    """
    Build a concise version of a full final_report dict.
    Returns (concise_dict, total_renamed, total_name_removed, total_deduped, total_merged).
    """
    # Apply BC-promotion before condensing to correct false-positive NBC entries
    # caused by grouping-context XPath resolution failures in the pyang plugin.
    report, _promoted = _promote_nbc_to_bc(report)

    # Fix incorrect line numbers on 'added'/'deleted' constraints that inherited
    # both line_number_old and line_number_new from their parent 'changed' entry.
    # An 'added' constraint only exists in the new file (→ only line_number_new),
    # and a 'deleted' constraint only exists in the old file (→ only line_number_old).
    for section_key in ("compatible", "non_compatible", "other-errors", "unmarked"):
        section = report.get(section_key)
        if isinstance(section, list):
            _fix_constraint_line_numbers(section)

    concise = {}
    total_renamed = 0
    total_name_removed = 0
    total_deduped = 0
    total_merged = 0

    for key, value in report.items():
        if key in ("compatible", "non_compatible", "other-errors", "unmarked"):
            if isinstance(value, list):
                condensed, renamed, name_removed, deduped, merged = condense_section(value)
                concise[key] = condensed
                total_renamed += renamed
                total_name_removed += name_removed
                total_deduped += deduped
                total_merged += merged
            else:
                concise[key] = value
        else:
            concise[key] = value

    # Cross-section / intra-section deduplication of typedef expansion noise:
    #
    # When a type changes from one typedef to another (e.g. inet:ip-address →
    # inet:ip-address-no-zone), the comparator expands both typedefs and reports
    # their union members as deleted/added.  These union member entries are
    # sub-paths of the parent type node and are implied by the parent type-changed
    # entry — they carry no independent information.
    #
    # IMPORTANT: For non_compatible, only use parent paths from non_compatible
    # type-changed entries (not from compatible).  A union member deletion in
    # non_compatible should only be removed as noise if its parent type change is
    # ALSO in non_compatible (meaning the overall type change is NBC).  If the
    # parent type change is only in compatible (BC), the union member deletion is
    # an independent NBC change and must be kept.
    #
    # Example of correct behavior:
    #   - bgp-set-med-type/type/union/enumeration: type changed (BC, in compatible)
    #   - bgp-set-med-type/type/union/string: type deleted (NBC, in non_compatible)
    #   → The string deletion is NOT noise — it's an independent NBC change.
    #     The enumeration change being BC does NOT imply the string deletion is noise.
    #
    # For compatible, use parent paths from both sections (the original behavior)
    # since a union member deletion in compatible is noise if the parent type
    # change is in either section.
    compat_type_parent_paths: set = _collect_type_changed_parent_paths(
        concise.get("compatible", [])
    )
    nbc_type_parent_paths: set = _collect_type_changed_parent_paths(
        concise.get("non_compatible", [])
    )
    all_type_changed_parent_paths: set = compat_type_parent_paths | nbc_type_parent_paths

    if all_type_changed_parent_paths:
        # For compatible: remove noise using parent paths from both sections
        if "compatible" in concise and isinstance(concise["compatible"], list):
            filtered, cross_removed = _remove_type_expansion_noise(
                concise["compatible"], all_type_changed_parent_paths,
                compat_entries=None
            )
            if cross_removed:
                concise["compatible"] = filtered
                total_deduped += cross_removed

        # For non_compatible: ONLY use parent paths from non_compatible itself.
        # Do NOT remove union member deletions/additions from non_compatible just
        # because a sibling union member's type change is in compatible (BC).
        if "non_compatible" in concise and isinstance(concise["non_compatible"], list):
            compat_entries_for_filter = concise.get("compatible", [])
            filtered, cross_removed = _remove_type_expansion_noise(
                concise["non_compatible"], nbc_type_parent_paths,
                compat_entries=compat_entries_for_filter
            )
            if cross_removed:
                concise["non_compatible"] = filtered
                total_deduped += cross_removed

    return concise, total_renamed, total_name_removed, total_deduped, total_merged


# ── File and batch processing ─────────────────────────────────────────────────

def process_file(final_report_path: Path) -> tuple:
    """
    Read final_report.json, condense it, write concise_final_report.json.
    Returns (renamed_count, name_removed_count, deduped_count, merged_count).
    """
    try:
        with open(final_report_path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  [SKIP] Cannot read {final_report_path}: {exc}", file=sys.stderr)
        return 0, 0, 0, 0

    concise, renamed, name_removed, deduped, merged = condense_report(report)

    out_path = final_report_path.parent / "concise_final_report.json"
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(concise, fh, indent=2, ensure_ascii=False)
        print(f"  [OK]   {out_path}")
    except OSError as exc:
        print(f"  [ERR]  Cannot write {out_path}: {exc}", file=sys.stderr)

    return renamed, name_removed, deduped, merged


def main(root_dir: str = "yang_comparator_reports") -> None:
    root = Path(root_dir)
    if not root.is_dir():
        print(f"ERROR: directory not found: {root}", file=sys.stderr)
        sys.exit(1)

    report_files = sorted(root.rglob("final_report.json"))
    total = len(report_files)
    print(f"Found {total} final_report.json file(s) under '{root}'")

    grand_renamed = 0
    grand_name_removed = 0
    grand_deduped = 0
    grand_merged = 0

    for i, path in enumerate(report_files, 1):
        print(f"[{i}/{total}] Processing {path}")
        renamed, name_removed, deduped, merged = process_file(path)
        grand_renamed += renamed
        grand_name_removed += name_removed
        grand_deduped += deduped
        grand_merged += merged

    print(f"\nDone. Generated {total} concise_final_report.json file(s).")
    print(f"  Entries reclassified as 'renamed'                    : {grand_renamed:,}")
    print(f"  'name' attribute children removed                     : {grand_name_removed:,}")
    print(f"  Child-path entries removed (deduplication)            : {grand_deduped:,}")
    print(f"  Grouping-expansion duplicates merged (same source)    : {grand_merged:,}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate concise_final_report.json from every final_report.json "
                    "found under the given root directory."
    )
    parser.add_argument(
        "root_dir",
        nargs="?",
        default="yang_comparator_reports",
        help="Root directory to search (default: yang_comparator_reports)",
    )
    args = parser.parse_args()
    main(args.root_dir)
