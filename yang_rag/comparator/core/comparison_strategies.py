#!/usr/bin/env python3
"""
Comparison strategies for YANG data structures.

This module provides different strategies for comparing various types of
YANG data structures (lists, dictionaries, etc.).
"""

import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List
from deepdiff import DeepDiff

from .constants import ChangeType
from . import constants
from .data_classes import ChangeRecord
from .similarity import SimilarityCalculator


class ComparisonStrategy(ABC):
    """Abstract base class for comparison strategies."""
    
    @abstractmethod
    def compare(self, old_data: Any, new_data: Any) -> List[ChangeRecord]:
        """
        Compare two data structures and return change records.
        
        Args:
            old_data: Original data structure
            new_data: New data structure
            
        Returns:
            List of ChangeRecord objects
        """
        pass
    
    @abstractmethod
    def can_handle(self, data_type: str, data: Dict[str, Any]) -> bool:
        """
        Check if this strategy can handle the given data type.
        
        Args:
            data_type: Type identifier string
            data: Data to check
            
        Returns:
            True if this strategy can handle the data
        """
        pass


class ListComparisonStrategy(ComparisonStrategy):
    """
    Strategy for comparing lists (like enums, bits, union members).
    
    This strategy handles:
    - Positional comparison for union member types (duplicate names)
    - Name-based comparison with similarity matching for other lists
    """
    
    def can_handle(self, data_type: str, data: Dict[str, Any]) -> bool:
        """Check if this is a list type."""
        return isinstance(data, list)
    
    def compare(self, old_list: List[Dict], new_list: List[Dict], 
                similarity_threshold: float = 55.0) -> List[ChangeRecord]:
        """
        Compare two lists using similarity matching.
        
        Special handling for union member types: when list items have duplicate names
        (e.g., multiple 'type' children with name='string'), compare by index position
        instead of by name to preserve order and detect all changes.
        
        Args:
            old_list: Original list
            new_list: New list
            similarity_threshold: Minimum similarity percentage for matching (0-100)
            
        Returns:
            List of ChangeRecord objects
        """
        if not isinstance(old_list, list):
            old_list = []
        if not isinstance(new_list, list):
            new_list = []
        
        # Check if this looks like a union type list (multiple items with same name and type='type')
        # Union types have nested 'type' children that may share the same 'name' (e.g., 'string')
        is_union_members = self._is_union_member_list(old_list, new_list)
        
        if is_union_members:
            return self._compare_positional(old_list, new_list)
        else:
            return self._compare_by_name(old_list, new_list, similarity_threshold)
    
    def _is_union_member_list(self, old_list: List, new_list: List) -> bool:
        """Check if lists contain union member types with duplicate names."""
        if not (old_list and new_list):
            return False
        
        # Check if items are type children with duplicate names
        old_names = [
            item.get("attributes", {}).get("name") if isinstance(item, dict) and "attributes" in item 
            else item.get("name") 
            for item in old_list if isinstance(item, dict)
        ]
        new_names = [
            item.get("attributes", {}).get("name") if isinstance(item, dict) and "attributes" in item
            else item.get("name") 
            for item in new_list if isinstance(item, dict)
        ]
        
        # If we have duplicate names and items have 'type' field, treat as union members
        has_duplicates = (len(old_names) != len(set(old_names)) or 
                         len(new_names) != len(set(new_names)))
        has_type_field = any(isinstance(item, dict) and item.get("type") == "type" 
                           for item in old_list + new_list)
        
        return has_duplicates and has_type_field
    
    def _compare_positional(self, old_list: List, new_list: List) -> List[ChangeRecord]:
        """Compare lists by position (for union members)."""
        changes = []
        max_len = max(len(old_list), len(new_list))
        
        for i in range(max_len):
            old_item = old_list[i] if i < len(old_list) else None
            new_item = new_list[i] if i < len(new_list) else None
            
            if old_item is None:
                # Added item
                name = self._get_item_name(new_item, i)
                changes.append(ChangeRecord(
                    change_type=ChangeType.ADDED,
                    path=f"{name}[{i}]",
                    new_value=new_item
                ))
            elif new_item is None:
                # Removed item
                name = self._get_item_name(old_item, i)
                changes.append(ChangeRecord(
                    change_type=ChangeType.DELETED,
                    path=f"{name}[{i}]",
                    old_value=old_item
                ))
            else:
                # Compare items at same position
                name = self._get_item_name(old_item, i)
                item_changes = self._compare_items(old_item, new_item)
                
                change_type = ChangeType.CHANGED if item_changes else ChangeType.UNCHANGED
                changes.append(ChangeRecord(
                    change_type=change_type,
                    path=f"{name}[{i}]",
                    old_value=old_item,
                    new_value=new_item,
                    details=item_changes
                ))
        
        return changes
    
    @staticmethod
    def _strip_type_prefix(name: str) -> str:
        """Strip module prefix from a YANG type name.

        E.g. 'inet:ipv4-address' → 'ipv4-address', 'oc-inet:ip-address' → 'ip-address'.
        Returns the original name unchanged if no prefix is present.
        """
        if name and ':' in name:
            return name.split(':', 1)[1]
        return name

    def _compare_by_name(self, old_list: List, new_list: List,
                        similarity_threshold: float) -> List[ChangeRecord]:
        """Compare lists by name with similarity matching."""
        # Create maps for exact name matches
        old_map = {item.get("name"): item for item in old_list
                  if isinstance(item, dict) and "name" in item}
        new_map = {item.get("name"): item for item in new_list
                  if isinstance(item, dict) and "name" in item}
        
        changes = []
        matched_old = set()
        matched_new = set()
        
        # Handle exact matches
        for name in sorted(set(old_map.keys()) & set(new_map.keys())):
            old_item = old_map[name]
            new_item = new_map[name]
            matched_old.add(name)
            matched_new.add(name)
            
            item_changes = self._compare_items(old_item, new_item)
            change_type = ChangeType.CHANGED if item_changes else ChangeType.UNCHANGED
            
            changes.append(ChangeRecord(
                change_type=change_type,
                path=name,
                old_value=old_item,
                new_value=new_item,
                details=item_changes
            ))
        
        # Handle unmatched items with similarity matching
        unmatched_old = [item for item in old_list
                        if not (isinstance(item, dict) and item.get("name") in matched_old)]
        unmatched_new = [item for item in new_list
                        if not (isinstance(item, dict) and item.get("name") in matched_new)]

        # ── Local-name fallback match (prefix/namespace change) ──────────────
        # When a type is migrated between modules (e.g. ietf-inet-types →
        # openconfig-inet-types), the prefix changes but the local name stays
        # the same: 'inet:ipv4-address' → 'ipv4-address' (or 'oc-inet:ipv4-address').
        # Without this step the comparator would report a delete + add, producing
        # a false-positive NBC entry.  Match by local name first; if the local
        # names are equal the change is a prefix-only rename (compatible).
        local_matched_old_idx: set = set()
        local_matched_new_idx: set = set()

        # Build local-name → index maps for unmatched items
        old_local_map: dict = {}  # local_name → list of (idx, item)
        for idx, item in enumerate(unmatched_old):
            if isinstance(item, dict) and item.get("name"):
                local = self._strip_type_prefix(item["name"])
                old_local_map.setdefault(local, []).append((idx, item))

        new_local_map: dict = {}  # local_name → list of (idx, item)
        for idx, item in enumerate(unmatched_new):
            if isinstance(item, dict) and item.get("name"):
                local = self._strip_type_prefix(item["name"])
                new_local_map.setdefault(local, []).append((idx, item))

        for local_name in sorted(set(old_local_map.keys()) & set(new_local_map.keys())):
            old_candidates = old_local_map[local_name]
            new_candidates = new_local_map[local_name]
            # Pair them up 1-to-1 in order
            for (oi, old_item), (ni, new_item) in zip(old_candidates, new_candidates):
                if oi in local_matched_old_idx or ni in local_matched_new_idx:
                    continue
                old_name = old_item.get("name", local_name)
                new_name = new_item.get("name", local_name)
                # Only treat as prefix-only if the local names truly match
                if self._strip_type_prefix(old_name) != self._strip_type_prefix(new_name):
                    continue
                local_matched_old_idx.add(oi)
                local_matched_new_idx.add(ni)
                item_changes = self._compare_items(old_item, new_item)
                # Filter out pure prefix-only name changes from the details.
                # When the only difference is the module prefix on the type name
                # (e.g. 'inet:ipv4-address' → 'ipv4-address'), the local name is
                # identical and the change is semantically equivalent — suppress it
                # so the record is not flagged as meaningful.
                filtered_changes = [
                    ch for ch in item_changes
                    if not (
                        ch.get("type") == "attribute_changed"
                        and ch.get("path") == "name"
                        and self._strip_type_prefix(str(ch.get("old", "")))
                            == self._strip_type_prefix(str(ch.get("new", "")))
                    )
                ]
                change_type = ChangeType.CHANGED if filtered_changes else ChangeType.UNCHANGED
                changes.append(ChangeRecord(
                    change_type=change_type,
                    path=old_name,
                    old_value=old_item,
                    new_value=new_item,
                    old_path=old_name,
                    new_path=new_name,
                    details=filtered_changes
                ))

        # Remove locally-matched items from the unmatched lists
        unmatched_old = [item for idx, item in enumerate(unmatched_old)
                         if idx not in local_matched_old_idx]
        unmatched_new = [item for idx, item in enumerate(unmatched_new)
                         if idx not in local_matched_new_idx]
        
        # Similarity-based pairing
        similarity_pairs = []
        for i, old_item in enumerate(unmatched_old):
            for j, new_item in enumerate(unmatched_new):
                similarity = SimilarityCalculator.calculate_similarity(old_item, new_item, {"name"})
                similarity_pairs.append((similarity, i, j, old_item, new_item))
        
        similarity_pairs.sort(reverse=True, key=lambda x: x[0])
        
        paired_old_idx = set()
        paired_new_idx = set()
        
        for similarity, i, j, old_item, new_item in similarity_pairs:
            if similarity < similarity_threshold:
                break
            if i in paired_old_idx or j in paired_new_idx:
                continue
                
            paired_old_idx.add(i)
            paired_new_idx.add(j)
            
            old_name = self._get_item_name(old_item, i)
            new_name = self._get_item_name(new_item, j)
            
            item_changes = self._compare_items(old_item, new_item)
            # Collapse 'renamed' into 'changed'; path differences will be rendered as attribute details
            change_type = (ChangeType.CHANGED if (old_name != new_name or item_changes) 
                         else ChangeType.UNCHANGED)
            
            changes.append(ChangeRecord(
                change_type=change_type,
                path=old_name,
                old_value=old_item,
                new_value=new_item,
                old_path=old_name,
                new_path=new_name,
                details=item_changes
            ))
        
        # Handle remaining unmatched items
        for i, old_item in enumerate(unmatched_old):
            if i not in paired_old_idx:
                name = self._get_item_name(old_item, i)
                changes.append(ChangeRecord(
                    change_type=ChangeType.DELETED,
                    path=name,
                    old_value=old_item
                ))
        
        for i, new_item in enumerate(unmatched_new):
            if i not in paired_new_idx:
                name = self._get_item_name(new_item, i)
                changes.append(ChangeRecord(
                    change_type=ChangeType.ADDED,
                    path=name,
                    new_value=new_item
                ))
        
        return changes
    
    def _get_item_name(self, item: Any, index: int) -> str:
        """Extract name from item or generate placeholder."""
        if isinstance(item, dict):
            # Try attributes.name first, then name
            if "attributes" in item:
                return item["attributes"].get("name", f"<item_{index}>")
            return item.get("name", f"<item_{index}>")
        return f"<item_{index}>"
    
    def _compare_items(self, old_item: Any, new_item: Any) -> List[Dict]:
        """Compare individual items and return detailed changes."""
        if not isinstance(old_item, dict) or not isinstance(new_item, dict):
            return [{"type": "value", "old": old_item, "new": new_item}] if old_item != new_item else []

        # ── Prefix-only rename early exit ────────────────────────────────────
        # When two items have the same local name after stripping the module
        # prefix (e.g. 'inet:ipv4-address' and 'ipv4-address' both strip to
        # 'ipv4-address'), the change is a pure namespace/module migration.
        # Skip all deep constraint comparison — the typedef constraints from
        # different modules (ietf-inet-types vs openconfig-inet-types) should
        # NOT be compared because the type identity is preserved.
        old_name = old_item.get("name", "")
        new_name = new_item.get("name", "")
        if (old_name and new_name
                and self._strip_type_prefix(old_name) == self._strip_type_prefix(new_name)
                and old_name != new_name):
            # Same local name, different prefix → prefix-only rename, no changes
            return []

        # Filter out metadata before comparison to avoid comparing line numbers/file paths
        old_filtered = self._filter_metadata(old_item)
        new_filtered = self._filter_metadata(new_item)
        
        diff = DeepDiff(old_filtered, new_filtered, verbose_level=2, view="tree")
        changes = []
        
        for change in diff.get("values_changed", []):
            changes.append({
                "type": "attribute_changed",
                "path": self._extract_path_from_deepdiff(change.path()),
                "old": change.t1,
                "new": change.t2
            })
        
        for change in diff.get("dictionary_item_added", []):
            changes.append({
                "type": "attribute_added", 
                "path": self._extract_path_from_deepdiff(change.path()),
                "new": getattr(change, "t2", None)
            })
        
        for change in diff.get("dictionary_item_removed", []):
            changes.append({
                "type": "attribute_removed",
                "path": self._extract_path_from_deepdiff(change.path()),
                "old": getattr(change, "t1", None)
            })
        
        return changes
    
    def _extract_path_from_deepdiff(self, path_str: str) -> str:
        """Extract clean path from DeepDiff path string."""
        matches = re.findall(r"\['([^']+)'\]", path_str)
        return matches[-1] if matches else path_str
    
    def _filter_metadata(self, item: Dict) -> Dict:
        """
        Filter out metadata from dictionary to avoid comparing line numbers/file paths.
        
        Metadata should be used for display only, not for detecting changes.
        """
        if not isinstance(item, dict):
            return item
        
        filtered = {}
        for key, value in item.items():
            # Skip the metadata key entirely
            if key == "metadata":
                continue
            # Recursively filter nested dicts
            if isinstance(value, dict):
                filtered[key] = self._filter_metadata(value)
            # Recursively filter lists of dicts
            elif isinstance(value, list):
                filtered[key] = [
                    self._filter_metadata(v) if isinstance(v, dict) else v
                    for v in value
                ]
            else:
                filtered[key] = value
        
        return filtered


class AttributeComparisonStrategy(ComparisonStrategy):
    """Strategy for comparing attributes (dictionaries)."""
    
    def can_handle(self, data_type: str, data: Dict[str, Any]) -> bool:
        """Check if this is a dictionary type."""
        return isinstance(data, dict)
    
    def compare(self, old_attrs: Dict, new_attrs: Dict) -> List[ChangeRecord]:
        """
        Compare attribute dictionaries.
        
        Args:
            old_attrs: Original attributes
            new_attrs: New attributes
            
        Returns:
            List of ChangeRecord objects
        """
        # Filter out metadata before comparison
        old_filtered = self._filter_metadata(old_attrs or {})
        new_filtered = self._filter_metadata(new_attrs or {})
        # Keep original (metadata-bearing) structures for display payloads.
        old_display = old_attrs or {}
        new_display = new_attrs or {}

        # Normalize singleton-vs-list shape drift before diffing so repeated
        # statements represented as list in one side and singleton in the other
        # still produce meaningful iterable/value deltas.
        old_filtered, new_filtered = self._align_list_shapes(old_filtered, new_filtered)
        old_display, new_display = self._align_list_shapes(old_display, new_display)
        
        diff = DeepDiff(old_filtered, new_filtered, verbose_level=2, view="tree")
        def _is_constraint(path: str) -> bool:
            """Check if path is a constraint keyword, stripping any namespace prefix.

            Extension keywords from vendor modules are stored with their namespace
            prefix (e.g. 'smiv2:max-access') but CONSTRAINT_KEYWORDS only contains
            the bare local name ('max-access').  Strip the prefix before checking so
            that Phase-2-added constraint rules are correctly applied.
            """
            bare = path.split(':', 1)[-1] if ':' in path else path
            return bare in constants.CONSTRAINT_KEYWORDS or path in constants.CONSTRAINT_KEYWORDS

        changes = []
        touched_paths = set()

        for change in diff.get("type_changes", []):
            path = self._extract_path(change.path())
            touched_paths.add(path)
            node_type = 'constraint' if _is_constraint(path) else 'attribute'
            changes.append(ChangeRecord(
                change_type=ChangeType.CHANGED,
                path=path,
                node_type=node_type,
                old_value=getattr(change, "t1", None),
                new_value=getattr(change, "t2", None)
            ))
        
        for change in diff.get("values_changed", []):
            path = self._extract_path(change.path())
            touched_paths.add(path)
            # Prioritize dynamic constraint classification
            node_type = 'constraint' if _is_constraint(path) else 'attribute'
            changes.append(ChangeRecord(
                change_type=ChangeType.CHANGED,
                path=path,
                node_type=node_type,
                old_value=change.t1,
                new_value=change.t2
            ))
        
        for change in diff.get("dictionary_item_added", []):
            path = self._extract_path(change.path())
            touched_paths.add(path)
            node_type = 'constraint' if _is_constraint(path) else 'attribute'
            raw_new = self._get_value_at_deepdiff_path(new_display, change.path())
            changes.append(ChangeRecord(
                change_type=ChangeType.ADDED,
                path=path,
                node_type=node_type,
                new_value=raw_new if raw_new is not None else getattr(change, "t2", None)
            ))
        
        for change in diff.get("dictionary_item_removed", []):
            path = self._extract_path(change.path())
            touched_paths.add(path)
            node_type = 'constraint' if _is_constraint(path) else 'attribute'
            raw_old = self._get_value_at_deepdiff_path(old_display, change.path())
            changes.append(ChangeRecord(
                change_type=ChangeType.DELETED,
                path=path,
                node_type=node_type,
                old_value=raw_old if raw_old is not None else getattr(change, "t1", None)
            ))

        # Handle list item deltas (DeepDiff iterable_item_added/removed).
        # Without this, extension lists like default-value can disappear when one
        # element is removed and another is modified.
        iterable_added = {}
        iterable_removed = {}

        for change in diff.get("iterable_item_added", []):
            path = self._extract_path(change.path())
            raw_new = self._get_value_at_deepdiff_path(new_display, change.path())
            iterable_added.setdefault(path, []).append(raw_new if raw_new is not None else getattr(change, "t2", None))

        for change in diff.get("iterable_item_removed", []):
            path = self._extract_path(change.path())
            raw_old = self._get_value_at_deepdiff_path(old_display, change.path())
            iterable_removed.setdefault(path, []).append(raw_old if raw_old is not None else getattr(change, "t1", None))

        for path in sorted(set(iterable_added.keys()) | set(iterable_removed.keys())):
            added_items = list(iterable_added.get(path, []))
            removed_items = list(iterable_removed.get(path, []))
            node_type = 'constraint' if _is_constraint(path) else 'attribute'

            # If scalar-level diff already captured this path, avoid synthetic
            # CHANGED pairing but still surface unmatched add/delete entries.
            if path in touched_paths:
                for old_item in removed_items:
                    changes.append(ChangeRecord(
                        change_type=ChangeType.DELETED,
                        path=path,
                        node_type=node_type,
                        old_value=old_item,
                    ))
                for new_item in added_items:
                    changes.append(ChangeRecord(
                        change_type=ChangeType.ADDED,
                        path=path,
                        node_type=node_type,
                        new_value=new_item,
                    ))
                continue

            # Pair removed/added items by best structural similarity and report as CHANGED.
            # Remaining unmatched items are ADDED/DELETED.
            while added_items and removed_items:
                best_i = 0
                best_j = 0
                best_score = -1.0
                for i, old_item in enumerate(removed_items):
                    for j, new_item in enumerate(added_items):
                        score = self._item_similarity_score(old_item, new_item)
                        if score > best_score:
                            best_score = score
                            best_i = i
                            best_j = j

                old_item = removed_items.pop(best_i)
                new_item = added_items.pop(best_j)
                changes.append(ChangeRecord(
                    change_type=ChangeType.CHANGED,
                    path=path,
                    node_type=node_type,
                    old_value=old_item,
                    new_value=new_item,
                ))

            for old_item in removed_items:
                changes.append(ChangeRecord(
                    change_type=ChangeType.DELETED,
                    path=path,
                    node_type=node_type,
                    old_value=old_item,
                ))

            for new_item in added_items:
                changes.append(ChangeRecord(
                    change_type=ChangeType.ADDED,
                    path=path,
                    node_type=node_type,
                    new_value=new_item,
                ))
        
        return changes

    def _align_list_shapes(self, old_value: Any, new_value: Any) -> tuple[Any, Any]:
        """Recursively align list/non-list shape mismatches for common keys."""
        if isinstance(old_value, dict) and isinstance(new_value, dict):
            old_copy = dict(old_value)
            new_copy = dict(new_value)
            for key in set(old_copy.keys()) & set(new_copy.keys()):
                old_copy[key], new_copy[key] = self._align_list_shapes(old_copy[key], new_copy[key])
            return old_copy, new_copy

        if isinstance(old_value, list) and not isinstance(new_value, list) and new_value is not None:
            return old_value, [new_value]

        if isinstance(new_value, list) and not isinstance(old_value, list) and old_value is not None:
            return [old_value], new_value

        return old_value, new_value

    def _item_similarity_score(self, old_item: Any, new_item: Any) -> float:
        """Compute a lightweight similarity score for list-item pairing."""
        if old_item == new_item:
            return 100.0

        if isinstance(old_item, dict) and isinstance(new_item, dict):
            old_attrs = old_item.get("attributes", {}) if isinstance(old_item.get("attributes", {}), dict) else {}
            new_attrs = new_item.get("attributes", {}) if isinstance(new_item.get("attributes", {}), dict) else {}
            old_cons = old_item.get("constraints", {}) if isinstance(old_item.get("constraints", {}), dict) else {}
            new_cons = new_item.get("constraints", {}) if isinstance(new_item.get("constraints", {}), dict) else {}

            score = 0.0
            if old_cons.get("when") and old_cons.get("when") == new_cons.get("when"):
                score += 70.0
            if old_attrs.get("description") and old_attrs.get("description") == new_attrs.get("description"):
                score += 20.0
            if old_attrs.get("name") and old_attrs.get("name") == new_attrs.get("name"):
                score += 10.0
            return score

        return 0.0
    
    def _extract_path(self, path_str: str) -> str:
        """Extract clean path from DeepDiff path string.

        For nested extension payloads, DeepDiff paths can end with
        ...['attributes']['name'] where 'name' is only the extension argument.
        In that case, return the nearest semantic owner token (prefer extension
        base keyword, e.g. 'default-value').
        """
        matches = re.findall(r"\['([^']+)'\]", path_str)
        if not matches:
            return path_str

        last = matches[-1]
        if last == "name":
            for token in reversed(matches[:-1]):
                if token in {"attributes", "constraints", "children", "metadata"}:
                    continue
                if ":" in token:
                    return token.split(":", 1)[1]
                if token != "name":
                    return token

        return last
    
    def _filter_metadata(self, item: Any) -> Any:
        """
        Filter out metadata from dictionary to avoid comparing line numbers/file paths.
        
        Metadata should be used for display only, not for detecting changes.
        """
        if not isinstance(item, dict):
            return item
        
        filtered = {}
        for key, value in item.items():
            # Skip the metadata key entirely
            if key == "metadata":
                continue
            # Recursively filter nested dicts
            if isinstance(value, dict):
                filtered[key] = self._filter_metadata(value)
            # Recursively filter lists of dicts
            elif isinstance(value, list):
                filtered[key] = [
                    self._filter_metadata(v) if isinstance(v, dict) else v
                    for v in value
                ]
            else:
                filtered[key] = value
        
        return filtered

    def _get_value_at_deepdiff_path(self, root: Any, path_str: str) -> Any:
        """Resolve a DeepDiff path (root['a'][0]['b']) against a python object."""
        if root is None or not isinstance(path_str, str) or not path_str.startswith("root"):
            return None

        cur = root
        token_re = re.compile(r"\['([^']+)'\]|\[(\d+)\]")
        for key_token, idx_token in token_re.findall(path_str):
            if key_token:
                if not isinstance(cur, dict) or key_token not in cur:
                    return None
                cur = cur[key_token]
            else:
                try:
                    idx = int(idx_token)
                except (TypeError, ValueError):
                    return None
                if not isinstance(cur, list) or idx < 0 or idx >= len(cur):
                    return None
                cur = cur[idx]
        return cur
