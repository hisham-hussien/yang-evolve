#!/usr/bin/env python3
"""
Group report items by base level into parent entries with nested attribute and constraint entries.

Inputs (defaults):
    - output/compatible_list.txt
    - output/non_compatible_list.txt

Output:
    - output/final_Report.json with keys: { "compatible": [<grouped>...], "non_compatible": [<grouped>...] }

Grouped entry shape (example):
    {
        "level": "2.",
        "path": "openconfig-if-ethernet/2024-09-17",
        "keyword": "revision",
        "action": "added",
        "attributes": [
            { "level": "2.1", "action": "added", "attribute": "description", "value": "..." },
            { "level": "2.2", "action": "added", "attribute": "name", "value": "2024-09-17" },
            { "level": "2.3", "action": "added", "attribute": "reference", "value": "2.14.0" }
        ],
        "constraints": [
            { "level": "2.4", "action": "added", "constraint": "when", "value": "some-condition" }
        ]
    }

Notes:
    - Parent records are items whose level ends with a trailing dot (e.g., "2.").
        - Attribute/constraint records are items whose level starts with the parent base (e.g., "2.1", "2.2", ...).
            Items with a 'constraint' key go under 'constraints'; items with an 'attribute' key go under 'attributes'.
    - Grouping key: (path, keyword, base-level). Items are kept intact with minimal transformation.
    - Child objects omit duplicated fields (path, keyword) for brevity.
    - Missing inputs are tolerated and will produce empty arrays.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

# Import type resolution and compatibility checking (if available)
try:
    from yang_rag.comparator.helper.yang_type_checker import resolve_typedef, is_superset_value_space
    TYPE_CHECKING_AVAILABLE = True
except ImportError:
    TYPE_CHECKING_AVAILABLE = False
    def resolve_typedef(typename):
        return typename
    def is_superset_value_space(old, new):
        return (None, "Type checking not available")


def load_json_list(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        print(f"[group_by_path] Warning: file not found: {path}")
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        print(f"[group_by_path] Warning: expected list in {path}, got {type(data)}; ignoring")
        return []
    except Exception as e:
        print(f"[group_by_path] Warning: failed to parse {path} as JSON: {e}")
        return []


_LEVEL_BASE_RE = re.compile(r"^(\d+)\.")


def first_level_number(level: str) -> int:
    """Extract the first numeric level component, e.g. '2.' or '2.1' -> 2.
    Returns -1 if not found.
    """
    if not isinstance(level, str):
        return -1
    m = _LEVEL_BASE_RE.match(level.strip())
    return int(m.group(1)) if m else -1


def is_parent_level(level: str) -> bool:
    """Parent entries end with a trailing dot, e.g., '2.'"""
    return isinstance(level, str) and level.strip().endswith('.')


def attribute_sort_key(level: str) -> Tuple[int, int]:
    """Sort attributes numerically by their sub-level: '2.1' < '2.2' < '2.10'.
    Returns (base, sub) tuple for stable ordering; base helps in mixed data.
    """
    base = first_level_number(level)
    sub = 0
    if isinstance(level, str):
        parts = level.strip().split('.')
        # parts like ['2', '1', ''] for '2.1' or ['2', ''] for '2.'
        if len(parts) > 1 and parts[1].isdigit():
            sub = int(parts[1])
    return (base, sub)


def group_by_parent(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group entries into parent-with-attributes based on level, path, and keyword.

    - Parent key is (path, keyword, base-level, action), where base-level is like '2.'
    - Attribute items with level starting with the same base-level are nested under parent's 'attributes'.
    - If attributes appear before the parent, create a placeholder parent and update when parent is seen.
    """
    groups: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str, str, str]] = []  # preserve first-seen group order

    def _has_group_for_level(path_val: str, keyword_val: str, level_val: str) -> bool:
        return any(
            k[0] == path_val and k[1] == keyword_val and k[2] == level_val
            for k in groups.keys()
        )

    for item in entries:
        path = item.get("path") or item.get("Path") or ""
        keyword = item.get("keyword") or item.get("Keyword") or ""
        level = item.get("level") or ""
        
        # Check if this is a nested parent keyword (has keyword field but no attribute/constraint field)
        # Examples: "7.3 (type added)" is a nested parent under "7. (leaf-list added)"
        is_nested_parent = (keyword and not item.get("attribute") and not item.get("constraint") 
                           and not level.endswith('.'))
        
        base_num = first_level_number(level)
        action = item.get("action", "")
        if base_num < 0:
            # Fallback: emit as its own parent with no attributes
            key = (path, keyword, level or "?", action)
            if key not in groups:
                parent = {k: v for k, v in item.items()}
                parent.setdefault("attributes", [])
                groups[key] = parent
                order.append(key)
            else:
                # treat as attribute if duplicate key occurs (rare)
                attr = {k: v for k, v in item.items() if k not in ("path", "Path", "keyword", "Keyword")}
                groups[key].setdefault("attributes", []).append(attr)
            continue

        # For nested parent keywords, use the full level as key to create separate group
        # For regular items (top-level parents and children), use base level
        # But if a child attribute's immediate parent level exists as a nested parent, group under that
        if is_nested_parent:
            base_level = level if level.endswith('.') else f"{level}."
        else:
            # For child attributes/constraints, check if their immediate parent level exists as a nested group
            # Example: for 7.3.1, check if 7.3. group exists; if so, use it instead of base 7.
            parts = level.split('.')
            if len(parts) >= 2:  # e.g., ['7', '3', '1'] for "7.3.1"
                parent_level = '.'.join(parts[:-1]) + '.'  # "7.3."
                if _has_group_for_level(path, keyword, parent_level):
                    base_level = parent_level
                else:
                    base_level = f"{base_num}."
            else:
                base_level = f"{base_num}."
        # For child records (have attribute or constraint), find the existing group
        # for this (path, keyword, base_level) regardless of the child's action.
        # The child's action describes what happened to the constraint/attribute,
        # not the parent node — so we must not use it to look up the parent group.
        is_child = bool(item.get("attribute") or item.get("constraint"))
        if is_child:
            # Find any existing group for this path/keyword/base_level
            existing_key = next(
                (k for k in groups if k[0] == path and k[1] == keyword and k[2] == base_level),
                None,
            )
            if existing_key is not None:
                key = existing_key
            else:
                key = (path, keyword, base_level, action)
        else:
            key = (path, keyword, base_level, action)

        if key not in groups:
            # create placeholder parent; we'll overwrite fields if the true parent arrives
            groups[key] = {
                "level": base_level,
                "path": path,
                "keyword": keyword,
                "action": item.get("action", ""),
                "attributes": [],
                "constraints": [],
                "_is_placeholder_parent": True,
            }
            order.append(key)

        # Check if this is a parent record or child attribute/constraint
        # Parent records: have keyword field AND no attribute/constraint field
        # Child records: have attribute or constraint field (may also have keyword if inherited)
        is_parent = (keyword and not item.get("attribute") and not item.get("constraint"))
        
        if is_parent:
            # update/overwrite parent core fields with the actual parent record
            parent = groups[key]
            # preserve existing children lists and metadata
            attrs = parent.get("attributes", [])
            cons = parent.get("constraints", [])
            is_placeholder_parent = parent.get("_is_placeholder_parent", False)
            existing_metadata = {}
            # Extract metadata fields from existing parent (line_number, file, etc.)
            for k in list(parent.keys()):
                if k in ("line_number", "line_number_old", "line_number_new", "file", "old_file", "new_file"):
                    existing_metadata[k] = parent[k]
            
            parent.clear()
            parent.update({k: v for k, v in item.items()})
            parent.setdefault("attributes", attrs)
            parent.setdefault("constraints", cons)
            # Mark as a real parent once we've seen an explicit parent-level entry
            parent["_is_placeholder_parent"] = False
            
            # Merge metadata from both entries (new entry takes precedence if same field)
            for k, v in existing_metadata.items():
                if k not in parent:
                    parent[k] = v
        else:
            # child record -> route to attributes vs constraints (omit duplicate path/keyword)
            child = {k: v for k, v in item.items() if k not in ("path", "Path", "keyword", "Keyword")}
            if "constraint" in child and child.get("constraint"):
                groups[key].setdefault("constraints", []).append(child)
            else:
                groups[key].setdefault("attributes", []).append(child)

    # Defensive sanitizer: handle a corner case where an attribute 'value' accidentally
    # concatenates the next attribute line (when upstream tag is missing), e.g.:
    #   "2025-07-17 2.2 attribute added: ['keyword'] -> revision"
    # We split such tails into a new child entry.
    EMBED_HEAD_RE = re.compile(r"\b\d+(?:\.\d+)*\s+attribute\s+(added|changed|deleted):", re.IGNORECASE)
    EMBED_TAIL_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+attribute\s+(added|changed|deleted):\s+\[([^\]]+)\]\s*->\s*(.*)$", re.IGNORECASE)

    def _strip_attr_token(tok: str) -> str:
        t = tok.strip()
        # remove quotes if present and any surrounding spaces
        if (t.startswith("'") and t.endswith("'")) or (t.startswith('"') and t.endswith('"')):
            t = t[1:-1]
        return t

    def sanitize_children(parent: Dict[str, Any]):
        attrs = parent.get("attributes") or []
        new_attrs: List[Dict[str, Any]] = []
        for child in attrs:
            # We only consider attributes with a string value field
            target_key = 'value' if child.get('action') == 'added' else 'new_value'
            val = child.get(target_key)
            if isinstance(val, str):
                m = EMBED_HEAD_RE.search(val)
                if m:
                    head_idx = m.start()
                    before = val[:head_idx].strip()
                    tail = val[head_idx:].strip()
                    m2 = EMBED_TAIL_RE.match(tail)
                    if m2:
                        # Update current child with the cleaned value (if any)
                        if before:
                            child[target_key] = before
                            new_attrs.append(child)
                        else:
                            # if no clean value remains, drop this key to avoid empty strings
                            if target_key in child:
                                child.pop(target_key, None)
                            new_attrs.append(child)
                        # Create extra child from embedded tail
                        lvl, act, attr_tok, rest_val = m2.groups()
                        extra: Dict[str, Any] = {
                            'level': lvl if isinstance(lvl, str) else str(lvl),
                            'action': act.lower(),
                            'attribute': _strip_attr_token(attr_tok),
                        }
                        # assign value keys depending on action
                        if extra['action'] == 'added':
                            extra['value'] = rest_val.strip()
                        elif extra['action'] == 'changed':
                            # try to split inline "(was ...)" if present
                            was_m = re.search(r"^(.*)\(was\s+(.*)\)\s*$", rest_val.strip(), re.IGNORECASE)
                            if was_m:
                                extra['new_value'] = was_m.group(1).strip()
                                extra['old_value'] = was_m.group(2).strip()
                            else:
                                extra['new_value'] = rest_val.strip()
                        else:
                            extra['new_value'] = rest_val.strip()
                        new_attrs.append(extra)
                        continue  # proceed to next child
            # default, unchanged
            new_attrs.append(child)
        # replace and sort later
        parent['attributes'] = new_attrs

    # apply sanitizer first
    for g in groups.values():
        sanitize_children(g)

    # sort attributes within each group by line number (if available), fallback to level numeric part
    # Also extract helper files from constraint values
    helper_file_pattern = re.compile(r'\[file:\s*([^\]]+)\]')
    
    def get_line_number_for_sorting(item: Dict[str, Any]) -> int:
        """Extract line number from item for sorting. Returns a high value if not found."""
        # Try single line_number first
        if "line_number" in item and item["line_number"] is not None:
            return int(item["line_number"])
        # Try line_number_new (for added/changed items)
        if "line_number_new" in item and item["line_number_new"] is not None:
            return int(item["line_number_new"])
        # Try line_number_old (for deleted items)
        if "line_number_old" in item and item["line_number_old"] is not None:
            return int(item["line_number_old"])
        # Fallback to high value so items without line numbers appear at the end
        return 999999
    
    for g in groups.values():
        attrs = g.get("attributes", [])
        cons = g.get("constraints", [])
        try:
            # Sort by line number, with fallback to level if line numbers are equal
            attrs.sort(key=lambda a: (get_line_number_for_sorting(a), attribute_sort_key(str(a.get("level", "")))))
        except Exception:
            pass
        try:
            cons.sort(key=lambda a: (get_line_number_for_sorting(a), attribute_sort_key(str(a.get("level", "")))))
        except Exception:
            pass
        g["attributes"] = attrs
        g["constraints"] = cons
        
        # If parent doesn't have file field, inherit from first child that has it
        if "file" not in g:
            for child in attrs + cons:
                if "file" in child:
                    g["file"] = child["file"]
                    break
        
        # Extract helper files from constraint values and clean them up
        for constraint in cons:
            # Check new_value for helper file
            new_val = constraint.get("new_value", "")
            if isinstance(new_val, str):
                match = helper_file_pattern.search(new_val)
                if match:
                    helper_file = match.group(1).strip()
                    constraint["file"] = helper_file
                    # Remove the helper file part from the value
                    constraint["new_value"] = helper_file_pattern.sub('', new_val).strip()
            
            # Check old_value for helper file (though it should usually be in new_value)
            old_val = constraint.get("old_value", "")
            if isinstance(old_val, str):
                match = helper_file_pattern.search(old_val)
                if match:
                    if "file" not in constraint:
                        constraint["file"] = match.group(1).strip()
                    constraint["old_value"] = helper_file_pattern.sub('', old_val).strip()

    # build list and sort parents by ascending level number, then path, then keyword
    def parent_sort_key(g: Dict[str, Any]):
        lvl = first_level_number(str(g.get("level", "")))
        if lvl < 0:
            lvl = 10**9  # push unknowns to the end
        return (
            lvl,
            str(g.get("path", "")),
            str(g.get("keyword", "")),
        )

    grouped_list = [groups[k] for k in order]

    # Drop placeholder parents that are already empty at this stage.
    # A second pass after redundancy pruning handles placeholders that become empty later.
    grouped_list = [
        g for g in grouped_list
        if not (g.get("_is_placeholder_parent") and not g.get("attributes") and not g.get("constraints"))
    ]

    try:
        grouped_list.sort(key=parent_sort_key)
    except Exception:
        pass
    # remove empty children arrays for cleaner output
    for g in grouped_list:
        if not g.get("attributes"):
            g.pop("attributes", None)
        if not g.get("constraints"):
            g.pop("constraints", None)
    
    # Post-processing: Filter redundant parent-level attribute/constraint changes
    # When a parent node has a child node that reports the same attribute or constraint change,
    # remove the redundant parent-level change since the child-level change typically has
    # more specific classification rules applied to it.
    #
    # This is generic and works for ANY parent-child relationship, not just specific keywords.
    # Examples:
    # - leaf with child type: parent's "name" change is redundant if child type also reports it
    # - container with child uses: parent's change may duplicate child's structural change
    # - typedef with child type: parent's attribute change duplicates child's type change
    
    # Step 1: Build a map of child changes indexed by parent path
    # Map structure: {parent_path: {(attribute_name, old_val, new_val): True, ...}}
    child_changes_by_parent = {}
    
    for g in grouped_list:
        full_path = g.get("path", "")
        if '/' in full_path:
            # This node has a parent - extract parent path
            parent_path = full_path.rsplit('/', 1)[0]
            
            # Collect all attribute changes from this child node
            for attr in g.get("attributes", []):
                attr_name = attr.get("attribute")
                old_val = attr.get("old_value")
                new_val = attr.get("new_value")
                
                # For "added" action, old_val might not exist but new_val does
                if not new_val and attr.get("value"):
                    new_val = attr.get("value")
                
                if attr_name:
                    # Create signature for this attribute change
                    change_sig = (attr_name, old_val, new_val)
                    if parent_path not in child_changes_by_parent:
                        child_changes_by_parent[parent_path] = {}
                    child_changes_by_parent[parent_path][change_sig] = True
            
            # Collect all constraint changes from this child node
            for cons in g.get("constraints", []):
                cons_name = cons.get("constraint")
                old_val = cons.get("old_value")
                new_val = cons.get("new_value")
                
                # For "added" action, old_val might not exist but new_val does
                if not new_val and cons.get("value"):
                    new_val = cons.get("value")
                
                if cons_name:
                    # Create signature for this constraint change
                    change_sig = (cons_name, old_val, new_val)
                    if parent_path not in child_changes_by_parent:
                        child_changes_by_parent[parent_path] = {}
                    child_changes_by_parent[parent_path][change_sig] = True
    
    # Step 2: Filter redundant changes from parent nodes
    for g in grouped_list:
        path = g.get("path", "")
        
        # Check if this path has children with changes
        if path not in child_changes_by_parent:
            continue
        
        child_change_sigs = child_changes_by_parent[path]
        
        # Filter attributes
        attrs = g.get("attributes", [])
        if attrs:
            filtered_attrs = []
            for attr in attrs:
                attr_name = attr.get("attribute")
                old_val = attr.get("old_value")
                new_val = attr.get("new_value")
                
                # For "added" action, old_val might not exist
                if not new_val and attr.get("value"):
                    new_val = attr.get("value")
                
                # Check if this attribute change is also reported by a child
                change_sig = (attr_name, old_val, new_val)
                if change_sig in child_change_sigs:
                    # This is redundant - the child reports it, so skip it at parent level
                    continue
                
                filtered_attrs.append(attr)
            g["attributes"] = filtered_attrs
        
        # Filter constraints
        cons = g.get("constraints", [])
        if cons:
            filtered_cons = []
            for constraint in cons:
                cons_name = constraint.get("constraint")
                old_val = constraint.get("old_value")
                new_val = constraint.get("new_value")
                
                # For "added" action, old_val might not exist
                if not new_val and constraint.get("value"):
                    new_val = constraint.get("value")
                
                # Check if this constraint change is also reported by a child
                change_sig = (cons_name, old_val, new_val)
                if change_sig in child_change_sigs:
                    # This is redundant - the child reports it, so skip it at parent level
                    continue
                
                filtered_cons.append(constraint)
            g["constraints"] = filtered_cons

    # Redundancy pruning can empty placeholder parents that were only scaffolding for child rows.
    # Drop them to avoid emitting false standalone structural deletions/additions in 'unmarked'.
    grouped_list = [
        g for g in grouped_list
        if not (g.get("_is_placeholder_parent") and not g.get("attributes") and not g.get("constraints"))
    ]

    # Drop no-op structural parents: if an item is marked as changed but has no
    # attributes or constraints left, it does not carry actionable detail.
    grouped_list = [
        g for g in grouped_list
        if not (g.get("action") == "changed" and not g.get("attributes") and not g.get("constraints"))
    ]
    
    # Post-processing: Detect and annotate type reference changes
    # This applies to ANY type name change (typedef, identityref, leafref, grouping references, etc.)
    if TYPE_CHECKING_AVAILABLE:
        for i, g in enumerate(grouped_list):
            if g.get("keyword") == "type" and g.get("action") == "changed":
                # Check if this is a type name change in attributes
                attrs = g.get("attributes", [])
                for attr in attrs:
                    if (attr.get("attribute") == "name" and 
                        attr.get("action") == "changed" and
                        attr.get("new_value") and 
                        attr.get("old_value")):
                        
                        new_val = str(attr.get("new_value"))
                        old_val = str(attr.get("old_value"))
                        
                        # Resolve both type references to their expanded definitions
                        # This handles typedefs, but could be extended to other YANG elements
                        new_resolved = resolve_typedef(new_val)
                        old_resolved = resolve_typedef(old_val)
                        
                        # Use the comprehensive compatibility checker from yang_type_checker
                        # This handles all constraints: ranges, lengths, patterns, enums, bits, etc.
                        is_compatible, explanation = is_superset_value_space(old_resolved, new_resolved)
                        
                        # Don't add verbose type_reference_analysis or note field
                        # The compatibility status is already determined by the tag
                        # No need to clutter output with redundant note field
    
    # Internal marker used only while grouping.
    for g in grouped_list:
        g.pop("_is_placeholder_parent", None)

    return grouped_list


# Regex patterns for pyang STDERR error lines we care about in other-errors.
# Format: /path/to/file.yang:LINE: error: MESSAGE
_PYANG_ERROR_LINE_RE = re.compile(
    r'^(.+\.yang):(\d+):\s+error:\s+(.+)$'
)

# Substrings that identify the error categories we want to surface.
_PYANG_OTHER_ERROR_PATTERNS: List[tuple] = [
    # (substring_in_message, error_type_label)
    ('not found in', 'NOT_FOUND'),
    ('circular dependency', 'CIRCULAR_DEPENDENCY')
]


def _parse_pyang_output_file(pyang_output_path: str) -> List[Dict[str, Any]]:
    """
    Parse a pre-generated pyang output file (``*_pyang_output.txt``) and
    return structured error entries for the ``other-errors`` section.

    The file format is:
        # Old: /path/to/old/file.yang
        # New: /path/to/new/file.yang
        ===...===
        STDERR:
        ===...===
        /path/to/file.yang:LINE: error: MESSAGE

    Each returned entry has the shape:
        {
            "error_type": "NOT_FOUND",
            "message": "type \"sr-sid-type\" not found in module \"openconfig-segment-routing\"",
            "file": "openconfig-rib-bgp-attributes.yang",
            "line_number": 697,
            "version": "old",   # derived from whether the path contains /old/ or /new/
            "source": "pyang",
        }
    """
    if not pyang_output_path or not os.path.exists(pyang_output_path):
        return []

    errors: List[Dict[str, Any]] = []
    try:
        with open(pyang_output_path, 'r', encoding='utf-8', errors='replace') as fh:
            content = fh.read()

        # Extract the STDERR section
        stderr_marker = 'STDERR:'
        stderr_idx = content.find(stderr_marker)
        if stderr_idx == -1:
            stderr_text = content
        else:
            # Skip past the separator line after STDERR:
            after_marker = content[stderr_idx + len(stderr_marker):]
            # Skip the === line
            lines_after = after_marker.splitlines()
            start = 0
            for idx, ln in enumerate(lines_after):
                if ln.startswith('==='):
                    start = idx + 1
                    break
            stderr_text = '\n'.join(lines_after[start:])

        for line in stderr_text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = _PYANG_ERROR_LINE_RE.match(line)
            if not m:
                continue
            file_path, line_no_str, message = m.group(1), m.group(2), m.group(3)

            # Determine which error category this belongs to
            msg_lower = message.lower()
            error_type = None
            for pattern, etype in _PYANG_OTHER_ERROR_PATTERNS:
                if pattern in msg_lower:
                    error_type = etype
                    break
            if error_type is None:
                continue  # Not a category we care about

            # Determine version (old/new) from the file path
            norm_path = file_path.replace('\\', '/')
            if '/old/' in norm_path:
                version = 'old'
            elif '/new/' in norm_path:
                version = 'new'
            else:
                version = 'unknown'

            errors.append({
                "error_type": error_type,
                "message": message.strip(),
                "file": os.path.basename(file_path),
                "line_number": int(line_no_str),
                "version": version,
                "source": "pyang",
            })
    except Exception:
        pass

    return errors


def _parse_pyang_errors(
    pyang_old_file: Optional[str],
    pyang_new_file: Optional[str],
    search_dirs: Optional[List[str]] = None,
    pyang_output_file: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Parse missing-reference and other structural errors from the pre-generated
    pyang output file (``*_pyang_output.txt``).  If no output file is provided,
    attempt to locate it automatically from the old YANG file path.

    Each returned entry has the shape:
        {
            "error_type": "NOT_FOUND",
            "message": "type \"sr-sid-type\" not found in module \"openconfig-segment-routing\"",
            "file": "openconfig-rib-bgp-attributes.yang",
            "line_number": 697,
            "version": "old",
            "source": "pyang"
        }

    Returns an empty list if the output file is not found or contains no
    relevant errors.
    """
    # If an explicit output file was provided, use it directly.
    if pyang_output_file and os.path.exists(pyang_output_file):
        return _parse_pyang_output_file(pyang_output_file)

    # Auto-discover the pyang output file from the old YANG file path.
    # The file lives at:
    #   comparison_results_concise/<commit>/pyang/<yang_base>_pyang_output.txt
    # The old YANG path follows the pattern:
    #   .../workspace/exports/<commit>/old/.../<yang_file>.yang
    if pyang_old_file:
        import re as _re
        m = _re.search(r'[/\\]exports[/\\]([0-9a-f]{40})[/\\]', pyang_old_file)
        if m:
            commit = m.group(1)
            yang_base = os.path.splitext(os.path.basename(pyang_old_file))[0]
            pyang_output_name = f"{yang_base}_pyang_output.txt"
            # Walk up from the YANG file to find the project root
            candidate = os.path.dirname(pyang_old_file)
            for _ in range(12):
                candidate = os.path.dirname(candidate)
                pyang_output_path = os.path.join(
                    candidate, 'comparison_results_concise', commit, 'pyang', pyang_output_name
                )
                if os.path.exists(pyang_output_path):
                    return _parse_pyang_output_file(pyang_output_path)
                if os.path.exists(os.path.join(candidate, 'comparison_results_concise')):
                    break  # Found the dir but file doesn't exist

    return []


def main():
    ap = argparse.ArgumentParser(description="Group compatible/non-compatible lists into parent with attributes")
    ap.add_argument("--compatible", default=os.path.join("output", "compatible_list.txt"),
                    help="Path to compatible_list.txt (JSON array)")
    ap.add_argument("--non-compatible", dest="non_compatible",
                    default=os.path.join("output", "non_compatible_list.txt"),
                    help="Path to non_compatible_list.txt (JSON array)")
    ap.add_argument("--out", default=os.path.join("output", "final_report.json"),
                    help="Output JSON path")
    ap.add_argument("--pyang-old-file", dest="pyang_old_file", default=None,
                    help="Path to the old YANG file (used by pyang to detect missing references)")
    ap.add_argument("--pyang-new-file", dest="pyang_new_file", default=None,
                    help="Path to the new YANG file (used by pyang to detect missing references)")
    ap.add_argument("--pyang-search-dirs", dest="pyang_search_dirs", nargs="*", default=None,
                    help="Additional search directories for pyang module resolution")
    args = ap.parse_args()

    # Resolve YANG file paths: prefer CLI args, fall back to env vars
    pyang_old_file = args.pyang_old_file or os.environ.get('YANG_OLD_FILE')
    pyang_new_file = args.pyang_new_file or os.environ.get('YANG_NEW_FILE')
    pyang_search_dirs = list(args.pyang_search_dirs or [])
    # Also add dirs from YANG_OLD_FILE and YANG_NEW_FILE env vars, including
    # parent and grandparent directories so that sibling modules (e.g.
    # openconfig-segment-routing.yang in a different sub-directory of
    # release/models/) can be resolved by pyang.
    for env_var in ('YANG_OLD_FILE', 'YANG_NEW_FILE'):
        env_path = os.environ.get(env_var)
        if not env_path:
            continue
        d = os.path.dirname(env_path)
        for candidate in (d, os.path.dirname(d), os.path.dirname(os.path.dirname(d))):
            if candidate and candidate not in pyang_search_dirs:
                pyang_search_dirs.append(candidate)

    compatible = load_json_list(args.compatible)
    non_compatible = load_json_list(args.non_compatible)
    
    # GROUP FIRST before categorization so that parent items have attributes/constraints arrays
    # This is crucial for extract_unmarked_children() to work correctly
    compatible = group_by_parent(compatible)
    non_compatible = group_by_parent(non_compatible)
    
    # Separate items by tags:
    # - Items with 'not-found' in all_tags go to other-errors section (legacy tag support)
    # - Items with no tags (empty all_tags or missing tag/all_tags) go to unmarked section
    # - Remaining compatible items stay in compatible section
    # - Remaining non-compatible items stay in non_compatible section
    
    def has_not_found_tag(item: Dict[str, Any]) -> bool:
        """Check if item or its children have 'not-found' tag (legacy enrichment tag)"""
        # Check parent-level all_tags
        if 'all_tags' in item and 'not-found' in item.get('all_tags', []):
            return True
        # Check attributes
        for attr in item.get('attributes', []):
            if 'all_tags' in attr and 'not-found' in attr.get('all_tags', []):
                return True
        # Check constraints
        for const in item.get('constraints', []):
            if 'all_tags' in const and 'not-found' in const.get('all_tags', []):
                return True
        return False
    
    def is_non_compatible(item: Dict[str, Any]) -> bool:
        """Check if item is non-backward-compatible (check parent and children)"""
        # Check parent tag
        if 'tag' in item and 'non-backward-compatible' in str(item.get('tag', '')):
            return True
        if 'all_tags' in item and 'non-backward-compatible' in item.get('all_tags', []):
            return True
        
        # Check children
        for attr in item.get('attributes', []):
            if 'tag' in attr and 'non-backward-compatible' in str(attr.get('tag', '')):
                return True
            if 'all_tags' in attr and 'non-backward-compatible' in attr.get('all_tags', []):
                return True
        
        for const in item.get('constraints', []):
            if 'tag' in const and 'non-backward-compatible' in str(const.get('tag', '')):
                return True
            if 'all_tags' in const and 'non-backward-compatible' in const.get('all_tags', []):
                return True
        
        return False
    
    def _item_has_own_tag(item: Dict[str, Any]) -> bool:
        """Return True if the item has a tag that is its own (not inherited from parent)."""
        if item.get('tag_inherited'):
            return False
        return bool(('tag' in item and item.get('tag')) or
                    ('all_tags' in item and len(item.get('all_tags', [])) > 0))

    def has_no_tags(item: Dict[str, Any]) -> bool:
        """Check if item has no compatibility tags (unmarked).

        An item is considered unmarked when:
        - The parent itself has no own tag, AND
        - None of its children have their own (non-inherited) tag.
        Items whose tag was inherited from the parent (tag_inherited=True) are
        treated as if they have no tag for this purpose.
        """
        parent_has_own_tag = _item_has_own_tag(item)

        # Also check if any children have their own (non-inherited) tags
        children_have_own_tags = False
        for attr in item.get('attributes', []):
            if _item_has_own_tag(attr):
                children_have_own_tags = True
                break

        if not children_have_own_tags:
            for const in item.get('constraints', []):
                if _item_has_own_tag(const):
                    children_have_own_tags = True
                    break

        # Item is unmarked only if neither parent nor any children have own tags
        return not parent_has_own_tag and not children_have_own_tags
    
    def extract_unmarked_children(item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract unmarked children (attributes/constraints without own tags, or with
        inherited-only tags) from an item.
        Returns a new item with only the unmarked children, or None if none found.

        SIDE EFFECT: Removes the unmarked children from the original item's 'attributes'
        and 'constraints' lists so they are not duplicated in the source section.
        """
        parent_has_own_tag = _item_has_own_tag(item)

        # Only extract if parent has its own tag (otherwise the whole item is unmarked)
        if not parent_has_own_tag:
            return None

        unmarked_attrs = []
        tagged_attrs = []
        unmarked_cons = []
        tagged_cons = []

        # Partition attributes: unmarked = no own tag (includes tag_inherited items)
        for attr in item.get('attributes', []):
            if _item_has_own_tag(attr):
                tagged_attrs.append(attr)
            else:
                unmarked_attrs.append(attr)

        # Partition constraints: unmarked = no own tag (includes tag_inherited items)
        for const in item.get('constraints', []):
            if _item_has_own_tag(const):
                tagged_cons.append(const)
            else:
                unmarked_cons.append(const)

        # If we found unmarked children, create a new item for the unmarked section
        if unmarked_attrs or unmarked_cons:
            # Remove the unmarked children from the original item so they are not duplicated
            item['attributes'] = tagged_attrs
            item['constraints'] = tagged_cons

            new_item = {k: v for k, v in item.items() if k not in ('attributes', 'constraints', 'tag', 'all_tags', 'tag_inherited')}
            new_item['attributes'] = unmarked_attrs
            new_item['constraints'] = unmarked_cons
            return new_item

        return None

    
    def _has_remaining_children(item: Dict[str, Any]) -> bool:
        """Return True if the item still has at least one attribute or constraint after extraction."""
        return bool(item.get('attributes')) or bool(item.get('constraints'))

    # Categorize items from both lists
    compatible_clean = []
    non_compatible_clean = []
    other_errors = []  # renamed from not_found; populated from legacy tags + pyang errors
    unmarked = []

    # XPath condition constraint types that can produce false-positive NBC verdicts
    # when the pyang plugin processes a grouping-level context (abstract path) instead
    # of an instantiated data-tree path.  Only 'when' and 'must' constraints suffer
    # from this because their validity depends on XPath resolution.
    _XPATH_CONDITION_TYPES = {'when', 'must'}

    def _bc_promotion_fingerprint(constraint: Dict[str, Any]) -> Optional[tuple]:
        """
        Build a fingerprint for a **changed** XPath condition constraint that can be
        used to detect false-positive NBC verdicts caused by grouping-context resolution
        failures.

        Returns ``None`` (no fingerprint) unless ALL of the following hold:
        - ``action`` is ``'changed'`` (not ``'added'`` or ``'deleted'``)
        - ``constraint`` type is ``'when'`` or ``'must'``
        - Both ``old_value`` and ``new_value`` are non-empty strings

        The ``action='added'`` case is intentionally excluded: adding a ``when``/``must``
        to an existing node is always NBC per the XML rules, regardless of how other
        instantiations classify it.  Only a *changed* condition can be a false-positive
        NBC due to the grouping context lacking schema information.

        Note: the ``file`` field is no longer required — constraints in the same file
        (no helper file) are equally eligible for fingerprint-based promotion.
        """
        action = (constraint.get('action') or '').lower()
        if action != 'changed':
            return None
        ctype = (constraint.get('constraint') or '').lower()
        if ctype not in _XPATH_CONDITION_TYPES:
            return None
        old_v = constraint.get('old_value') or ''
        new_v = constraint.get('new_value') or ''
        if not old_v or not new_v:
            return None
        # Use file as part of fingerprint when available; fall back to empty string
        # so that same-file constraints (no helper file) are still fingerprintable.
        file_ = constraint.get('file') or constraint.get('helper_file') or ''
        return (file_, ctype, old_v, new_v)

    def _item_bc_promotion_fingerprints(item: Dict[str, Any]) -> Set[tuple]:
        """Collect BC-promotion fingerprints from an item's constraints list."""
        fps: Set[tuple] = set()
        for c in item.get('constraints') or []:
            fp = _bc_promotion_fingerprint(c)
            if fp:
                fps.add(fp)
        return fps

    def _item_is_promotable(item: Dict[str, Any]) -> bool:
        """
        Return True if the item has at least one constraint AND every constraint
        in the item is a changed when/must with non-empty old/new values.

        Items with no constraints, or with any added/deleted constraints, are
        never promotable — they must stay in non_compatible.
        """
        constraints = item.get('constraints') or []
        if not constraints:
            return False
        return all(_bc_promotion_fingerprint(c) is not None for c in constraints)

    def _item_is_llm_confirmed_bc(item: Dict[str, Any]) -> bool:
        """
        Return True if the item should be promoted to compatible based on LLM
        verification: every constraint (and there is at least one) must have
        been **reclassified to backward-compatible** by the LLM verification
        layer (i.e. its ``tag`` is ``'backward-compatible'``).

        This handles the case where the static tool initially classified a
        changed ``when``/``must`` constraint as NBC but the LLM (and/or the
        static ConditionAnalyzer) confirmed it is actually BC (relaxed or
        equivalent) and the ``llm_verification_hook.py`` Step 2 already
        updated the compatibility tag on the enriched report line from
        ``<non-backward-compatible>`` to ``<backward-compatible>``.

        IMPORTANT: ``llm_assistance_decision == 'confirmed'`` alone is NOT
        sufficient — it only means "LLM agrees with the static tool's semantic
        direction", which could be NBC-confirmed (narrowed) or BC-confirmed
        (relaxed).  We must check the actual compatibility tag on the
        constraint record.

        Safety guards:
        - Only ``'changed'`` constraints are eligible (added/deleted constraints
          that are NBC must stay NBC regardless of LLM opinion).
        - The item must have at least one constraint; attribute-only items are
          not eligible.
        - No attributes may be present (an item with both NBC attributes and
          BC-confirmed constraints is still NBC overall).
        - Every constraint's ``tag`` must be ``'backward-compatible'``.
        """
        constraints = item.get('constraints') or []
        if not constraints:
            return False
        # Attribute-level NBC changes must not be silently promoted
        if item.get('attributes'):
            return False
        for c in constraints:
            action = (c.get('action') or '').lower()
            if action != 'changed':
                return False
            # The constraint tag must be backward-compatible (set by
            # llm_verification_hook.py Step 2 when llm_compat != orig_decision).
            # 'confirmed' alone is ambiguous — it could be NBC-confirmed.
            tag = (c.get('tag') or '').lower()
            if tag != 'backward-compatible':
                return False
        return True

    # Process items from compatible list
    for item in compatible:
        if has_not_found_tag(item):
            other_errors.append(item)
        elif has_no_tags(item):
            unmarked.append(item)
        else:
            # Has tags - extract any unmarked children into the unmarked section.
            # extract_unmarked_children() mutates item in-place, removing the unmarked
            # children from item['attributes'] / item['constraints'].
            unmarked_children_item = extract_unmarked_children(item)
            if unmarked_children_item:
                unmarked.append(unmarked_children_item)

            # After extraction, only keep the parent in its section if it still has
            # at least one tagged child (or no children at all, meaning it is a
            # standalone parent-level change with no sub-items).
            # If all children were unmarked and moved out, drop the parent entirely.
            if item.get('attributes') is not None or item.get('constraints') is not None:
                # Parent had children arrays; keep it only if some remain
                if _has_remaining_children(item):
                    if is_non_compatible(item):
                        non_compatible_clean.append(item)
                    else:
                        compatible_clean.append(item)
                # else: all children were unmarked → parent already moved to unmarked
            else:
                # Parent never had children arrays (standalone change) → keep it
                if is_non_compatible(item):
                    non_compatible_clean.append(item)
                else:
                    compatible_clean.append(item)

    # Build a set of BC-promotion fingerprints from the compatible list.
    # These represent changed when/must constraints that were correctly classified as BC
    # in at least one instantiation context.  NBC items whose constraints all match these
    # fingerprints are false positives caused by the grouping-level context lacking the
    # schema information needed to validate the XPath — the BC verdict from the
    # instantiated context is authoritative.
    bc_promotion_fps: Set[tuple] = set()
    for item in compatible_clean:
        bc_promotion_fps.update(_item_bc_promotion_fingerprints(item))

    # Process items from non_compatible list
    for item in non_compatible:
        if has_not_found_tag(item):
            other_errors.append(item)
        elif has_no_tags(item):
            unmarked.append(item)
        else:
            # Has tags - extract any unmarked children into the unmarked section.
            # extract_unmarked_children() mutates item in-place, removing the unmarked
            # children from item['attributes'] / item['constraints'].
            unmarked_children_item = extract_unmarked_children(item)
            if unmarked_children_item:
                unmarked.append(unmarked_children_item)

            # After extraction, only keep the parent in its section if it still has
            # at least one tagged child (or no children at all).
            if item.get('attributes') is not None or item.get('constraints') is not None:
                if _has_remaining_children(item):
                    # Promotion path 1: fingerprint-based — ALL constraints are changed
                    # when/must conditions confirmed BC in another instantiation context.
                    # This corrects false-positive NBC verdicts from grouping contexts.
                    item_fps = _item_bc_promotion_fingerprints(item)
                    fp_promoted = (
                        bc_promotion_fps
                        and item_fps
                        and _item_is_promotable(item)
                        and item_fps.issubset(bc_promotion_fps)
                    )
                    # Promotion path 2: LLM-confirmed BC — every constraint has been
                    # individually verified by the LLM (and/or static ConditionAnalyzer)
                    # as backward-compatible (relaxed/equivalent).  This handles items
                    # whose constraint only appears in the NBC list (no matching BC
                    # instantiation to build a fingerprint from).
                    llm_promoted = _item_is_llm_confirmed_bc(item)
                    if fp_promoted or llm_promoted:
                        compatible_clean.append(item)
                    else:
                        non_compatible_clean.append(item)
                # else: all children were unmarked → parent dropped from non_compatible
            else:
                # Standalone parent-level change → check for BC promotion
                item_fps = _item_bc_promotion_fingerprints(item)
                fp_promoted = (
                    bc_promotion_fps
                    and item_fps
                    and _item_is_promotable(item)
                    and item_fps.issubset(bc_promotion_fps)
                )
                llm_promoted = _item_is_llm_confirmed_bc(item)
                if fp_promoted or llm_promoted:
                    compatible_clean.append(item)
                else:
                    non_compatible_clean.append(item)
    
    # Items are already grouped, just organize into categories
    grouped = {
        "compatible": compatible_clean,
        "non_compatible": non_compatible_clean,
        "other-errors": other_errors,
        "unmarked": unmarked,
    }
    
    # Clean up internal categorization fields (tag, all_tags, tag_inherited) from final output
    # Also remove empty attributes/constraints arrays that may have been left behind after
    # extract_unmarked_children() moved children out of the parent item.
    def clean_tags(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove tag, all_tags and tag_inherited fields used only for categorization.
        Also prune empty attributes/constraints arrays for cleaner output."""
        for item in items:
            item.pop('tag', None)
            item.pop('all_tags', None)
            item.pop('tag_inherited', None)
            for attr in item.get('attributes', []):
                attr.pop('tag', None)
                attr.pop('all_tags', None)
                attr.pop('tag_inherited', None)
            for const in item.get('constraints', []):
                const.pop('tag', None)
                const.pop('all_tags', None)
                const.pop('tag_inherited', None)
            # Remove empty arrays (may result from extract_unmarked_children mutations)
            if 'attributes' in item and not item['attributes']:
                item.pop('attributes')
            if 'constraints' in item and not item['constraints']:
                item.pop('constraints')
        return items

    # Apply cleanup to all sections
    grouped['compatible'] = clean_tags(grouped['compatible'])
    grouped['non_compatible'] = clean_tags(grouped['non_compatible'])
    grouped['other-errors'] = clean_tags(grouped['other-errors'])
    grouped['unmarked'] = clean_tags(grouped['unmarked'])

    # Final guardrail: remove structural 'changed' items that carry no child details.
    # These are noise-only parent entries and should not appear in final_report.json.
    def prune_empty_changed(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        pruned = []
        for item in items:
            if item.get('action') == 'changed' and not item.get('attributes') and not item.get('constraints'):
                continue
            pruned.append(item)
        return pruned

    grouped['compatible'] = prune_empty_changed(grouped['compatible'])
    grouped['non_compatible'] = prune_empty_changed(grouped['non_compatible'])
    grouped['other-errors'] = prune_empty_changed(grouped['other-errors'])
    grouped['unmarked'] = prune_empty_changed(grouped['unmarked'])

    # Inject pyang-detected errors into 'other-errors' by parsing the pre-generated
    # pyang output file (comparison_results_concise/<commit>/pyang/<yang>_pyang_output.txt).
    # The function auto-discovers the file from the old YANG path.
    if pyang_old_file or pyang_new_file:
        pyang_errors = _parse_pyang_errors(
            pyang_old_file,
            pyang_new_file,
        )
        if pyang_errors:
            grouped['other-errors'].extend(pyang_errors)
            print(f"[group_by_path] Added {len(pyang_errors)} pyang error(s) to 'other-errors'")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(grouped, f, indent=2, ensure_ascii=False)
    print(f"[group_by_path] Wrote grouped output to {args.out}")


if __name__ == "__main__":
    main()
