#!/usr/bin/env python3
"""
Group duplicate changes in the final report.

When the same change appears in multiple locations (e.g., due to grouping expansion),
this script groups them together to make the report more concise.

Items are considered duplicates if they have the same:
- keyword
- action
- line_number
- attributes (content, not levels)
- constraints (content, not levels)

Only the paths and levels differ.
"""

import json
import sys
from typing import Dict, List, Any, Tuple, Optional
from collections import defaultdict


def _extract_name_transition(item: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Return (old_name, new_name) for simple name change on grouped item."""
    attrs = item.get('attributes') or []
    for attr in attrs:
        if attr.get('attribute') != 'name':
            continue
        if attr.get('action') != 'changed':
            continue
        old_v = attr.get('old_value')
        new_v = attr.get('new_value')
        if old_v is None or new_v is None:
            continue
        return (str(old_v), str(new_v))
    return None


def _canonical_path(path: Any, item: Optional[Dict[str, Any]] = None) -> str:
    """Canonicalize path variants so path-only duplicates can collapse safely."""
    p = str(path or '').strip()
    if not p:
        return ''

    segs = [s for s in p.split('/') if s]
    segs = [s.split(':', 1)[1] if ':' in s else s for s in segs]

    # Collapse duplicated leading module segment: module/module/... -> module/...
    if len(segs) >= 2 and segs[0] == segs[1]:
        segs = segs[1:]

    # Wrapper rename path variant:
    # .../<old-name>/<keyword>/<new-name> -> .../<keyword>/<new-name>
    if item is not None:
        node_kw = str(item.get('keyword') or '')
        name_tr = _extract_name_transition(item)
        if node_kw and name_tr and len(segs) >= 3:
            old_name, new_name = name_tr
            if segs[-1] == new_name and segs[-2] == node_kw and segs[-3] == old_name:
                del segs[-3]

    return '/'.join(segs)


def _path_quality(path: Any) -> Tuple[int, int, int, int]:
    """Higher tuple is better for selecting canonical representative path."""
    p = str(path or '')
    parts = [s for s in p.split('/') if s]
    has_prefix = any(':' in s for s in parts)
    has_double_slash = '//' in p
    depth = len(parts)
    # Prefer: prefixed, no //, shorter depth, shorter path
    return (int(has_prefix), int(not has_double_slash), -depth, -len(p))


def normalize_item_for_grouping(item: Dict[str, Any]) -> Tuple:
    """
    Create a hashable key for grouping items.
    
    Excludes path and level since those are what we want to group by.
    Items with the same line numbers and changes are grouped together.
    """
    # Start with basic properties (excluding path to allow grouping)
    key_parts = [
        item.get('keyword', ''),
        item.get('action', ''),
        item.get('line_number', ''),
        item.get('line_number_old', ''),
        item.get('line_number_new', ''),
    ]
    
    # Add attributes (without levels)
    attributes = item.get('attributes', [])
    if attributes:
        attr_tuples = []
        for attr in attributes:
            attr_tuple = (
                attr.get('attribute', ''),
                attr.get('action', ''),
                attr.get('value', ''),
                attr.get('old_value', ''),
                attr.get('new_value', ''),
                attr.get('line_number', ''),
                attr.get('line_number_old', ''),
                attr.get('line_number_new', ''),
            )
            attr_tuples.append(attr_tuple)
        key_parts.append(tuple(sorted(attr_tuples)))
    else:
        key_parts.append(None)
    
    # Add constraints (without levels)
    constraints = item.get('constraints', [])
    if constraints:
        constr_tuples = []
        for constr in constraints:
            constr_tuple = (
                constr.get('constraint', ''),
                constr.get('action', ''),
                constr.get('value', ''),
                constr.get('old_value', ''),
                constr.get('new_value', ''),
                constr.get('line_number', ''),
                constr.get('line_number_old', ''),
                constr.get('line_number_new', ''),
            )
            constr_tuples.append(constr_tuple)
        key_parts.append(tuple(sorted(constr_tuples)))
    else:
        key_parts.append(None)
    
    return tuple(key_parts)


def group_duplicate_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Group items that are identical except for path and level.
    
    Returns a new list with grouped items.
    """
    # Group items by their normalized key
    groups = defaultdict(list)
    
    for item in items:
        key = normalize_item_for_grouping(item)
        groups[key].append(item)
    
    # Build result list
    result = []
    
    for key, group_items in groups.items():
        if len(group_items) == 1:
            # Only one item, keep as-is
            result.append(group_items[0])
            continue

        # If all grouped paths are canonical-equivalent, keep a single canonical
        # representative instead of emitting list-valued path/level entries.
        canonical_paths = [_canonical_path(it.get('path'), it) for it in group_items]
        if canonical_paths and len(set(canonical_paths)) == 1:
            best_item = max(group_items, key=lambda it: _path_quality(it.get('path')))
            result.append(best_item)
            continue

        # Multiple items, group them
        grouped_item = group_items[0].copy()

        # Collect all paths and levels (order matters)
        paths = [item.get('path') for item in group_items]
        levels = [item.get('level') for item in group_items]
        grouped_item['path'] = paths
        grouped_item['level'] = levels

        # For provenance fields, if all group members share the same value -> emit scalar,
        # otherwise emit an array aligned to paths (including None for missing values).
        provenance_fields = ['file', 'old_file', 'new_file', 'line_number', 'line_number_old', 'line_number_new']
        for field in provenance_fields:
            values = [item.get(field) if field in item else None for item in group_items]
            # If every value is None, skip
            if all(v is None for v in values):
                grouped_item.pop(field, None)
                continue
            # If all non-None values are equal and at least one non-None exists, emit scalar
            non_none = [v for v in values if v is not None]
            if non_none and all(v == non_none[0] for v in non_none):
                grouped_item[field] = non_none[0]
            else:
                grouped_item[field] = values

        # Group attributes and constraints per-position: levels and provenance
        for collection_name in ('attributes', 'constraints'):
            if collection_name not in grouped_item or not grouped_item.get(collection_name):
                continue
            # For each item position in the base grouped_item, collect per-group values
            base_list = grouped_item[collection_name]
            for idx, base_elem in enumerate(base_list):
                # Collect levels across group
                levels_at_idx = []
                for it in group_items:
                    lst = it.get(collection_name) or []
                    if idx < len(lst):
                        levels_at_idx.append(lst[idx].get('level'))
                    else:
                        levels_at_idx.append(None)
                # If any meaningful level values, set aligned list
                if any(l is not None and l != '' for l in levels_at_idx):
                    base_elem['level'] = levels_at_idx

                # Propagate provenance fields into element (file/old_file/new_file/line numbers)
                for field in provenance_fields:
                    vals = []
                    for it in group_items:
                        lst = it.get(collection_name) or []
                        if idx < len(lst) and field in lst[idx]:
                            vals.append(lst[idx].get(field))
                        else:
                            vals.append(None)
                    if all(v is None for v in vals):
                        # ensure no stale key
                        base_elem.pop(field, None)
                        continue
                    non_none = [v for v in vals if v is not None]
                    if non_none and all(v == non_none[0] for v in non_none):
                        base_elem[field] = non_none[0]
                    else:
                        base_elem[field] = vals

        result.append(grouped_item)
    
    # Sort by first path for consistency; pyang error entries may not have 'path'
    def _sort_key(x: Dict[str, Any]) -> str:
        p = x.get('path', x.get('file', ''))
        return p[0] if isinstance(p, list) else (p or '')
    result.sort(key=_sort_key)
    
    return result


def _split_changed_multipath_entries(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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

    For 'added' and 'deleted' entries, multi-path lists are intentional (same
    grouping definition at multiple schema paths) and are left unchanged.
    """
    result = []
    for item in items:
        action = item.get('action', '')
        path = item.get('path')
        level = item.get('level')

        # Only split 'changed' entries with multiple paths
        if action != 'changed' or not isinstance(path, list) or len(path) <= 1:
            result.append(item)
            continue

        # Check if paths represent different schema nodes by comparing parent segments.
        # Extract the second-to-last segment of each path (the parent container/list).
        def _parent_seg(p: str) -> str:
            parts = [s for s in str(p).split('/') if s]
            return parts[-2] if len(parts) >= 2 else ''

        parent_segs = set(_parent_seg(p) for p in path)

        # If all paths have the same parent segment, they are grouping-expansion
        # duplicates of the same node → keep merged (do not split).
        if len(parent_segs) <= 1:
            result.append(item)
            continue

        # Different parent segments → different schema nodes → split into per-path entries.
        levels = level if isinstance(level, list) else [level] * len(path)
        while len(levels) < len(path):
            levels.append(levels[-1] if levels else '')

        for i, p in enumerate(path):
            new_item = dict(item)
            new_item['path'] = p
            new_item['level'] = levels[i] if i < len(levels) else ''

            # Also split per-path level values inside constraints and attributes
            for collection_name in ('constraints', 'attributes'):
                collection = item.get(collection_name)
                if not isinstance(collection, list):
                    continue
                new_collection = []
                for child in collection:
                    new_child = dict(child)
                    child_level = child.get('level', '')
                    if isinstance(child_level, list) and len(child_level) > i:
                        new_child['level'] = child_level[i]
                    elif isinstance(child_level, list) and child_level:
                        new_child['level'] = child_level[-1]
                    new_collection.append(new_child)
                new_item[collection_name] = new_collection

            result.append(new_item)

    return result


def _remove_noop_changes(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove attributes/constraints where action='changed' but new_value == old_value.

    This can happen when two nodes are paired as a rename (e.g. serial_no → serial-no)
    and the comparator detects a 'changed' description even though the text is identical.
    Such no-op changes add noise to the report without conveying useful information.
    """
    cleaned = []
    for item in items:
        item = item.copy()

        # Filter attributes
        attrs = item.get('attributes')
        if attrs:
            filtered_attrs = []
            for attr in attrs:
                if (attr.get('action') == 'changed'
                        and 'new_value' in attr
                        and 'old_value' in attr
                        and attr['new_value'] == attr['old_value']):
                    continue  # Skip no-op change
                filtered_attrs.append(attr)
            if filtered_attrs:
                item['attributes'] = filtered_attrs
            else:
                item.pop('attributes', None)

        # Filter constraints
        constrs = item.get('constraints')
        if constrs:
            filtered_constrs = []
            for constr in constrs:
                if (constr.get('action') == 'changed'
                        and 'new_value' in constr
                        and 'old_value' in constr
                        and constr['new_value'] == constr['old_value']):
                    continue  # Skip no-op change
                filtered_constrs.append(constr)
            if filtered_constrs:
                item['constraints'] = filtered_constrs
            else:
                item.pop('constraints', None)

        # Only keep the item if it still has meaningful content
        # (has attributes, constraints, or is a structural change with no sub-items)
        if item.get('attributes') or item.get('constraints') or (
                not item.get('attributes') and not item.get('constraints')):
            cleaned.append(item)

    return cleaned


def group_report(report_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Group duplicate changes in all categories of the report.
    """
    result = report_data.copy()

    # Group each category
    for category in ['compatible', 'non_compatible', 'other-errors', 'unmarked']:
        if category in result and result[category]:
            # First remove no-op changes (new_value == old_value)
            result[category] = _remove_noop_changes(result[category])
            # Split any 'changed' entries that were previously incorrectly merged
            # across different schema paths (e.g. encapsulation/config/X and
            # encapsulation/state/X merged because they share the same source line).
            # This corrects data from earlier runs before the fix was applied.
            result[category] = _split_changed_multipath_entries(result[category])
            # Then group duplicates (now with path included in key for 'changed')
            result[category] = group_duplicate_items(result[category])

    return result


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Group duplicate changes in the final report"
    )
    parser.add_argument(
        '--report',
        default='output/final_report.json',
        help='Path to final_report.json (default: output/final_report.json)'
    )
    parser.add_argument(
        '--output',
        help='Output path (default: overwrites input)'
    )
    
    args = parser.parse_args()
    
    # Load the report
    print(f"[group_duplicate_changes] Loading report from {args.report}")
    with open(args.report, 'r', encoding='utf-8') as f:
        report_data = json.load(f)
    
    # Count before grouping
    before_counts = {
        category: len(report_data.get(category, []))
        for category in ['compatible', 'non_compatible', 'other-errors', 'unmarked']
    }
    
    # Group duplicates
    print("[group_duplicate_changes] Grouping duplicate changes...")
    grouped_report = group_report(report_data)
    
    # Count after grouping
    after_counts = {
        category: len(grouped_report.get(category, []))
        for category in ['compatible', 'non_compatible', 'other-errors', 'unmarked']
    }
    
    # Write output
    output_path = args.output or args.report
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(grouped_report, f, indent=2, ensure_ascii=False)
    
    print(f"[group_duplicate_changes] Grouped report written to {output_path}")
    print()
    print("Summary:")
    for category in ['compatible', 'non_compatible', 'other-errors', 'unmarked']:
        before = before_counts[category]
        after = after_counts[category]
        if before > 0:
            reduction = before - after
            percent = (reduction / before * 100) if before > 0 else 0
            print(f"  {category:20s}: {before:3d} → {after:3d} ({reduction:3d} grouped, {percent:5.1f}% reduction)")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
