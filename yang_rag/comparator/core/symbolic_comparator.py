#!/usr/bin/env python3
"""
Symbolic Entry Comparator for YANG structures.

This module handles comparison of symbolic entries (enum, bit) in YANG schemas.
It detects:
- Added/deleted symbolic entries
- Changed symbolic entry values (e.g., enum value changes)
- Changed symbolic entry attributes (description, status, etc.)
"""

from typing import Dict, List, Optional, Any
from .constants import ChangeType, SYMBOLIC_KEYWORDS, CONSTRAINT_KEYWORDS
from .data_classes import ChangeRecord
from .comparison_strategies import ListComparisonStrategy


class SymbolicComparator:
    """Handles comparison of symbolic entries (enum, bit, etc.)."""
    
    def __init__(self):
        """Initialize the symbolic comparator."""
        self.list_strategy = ListComparisonStrategy()
    
    def compare_symbolic_entries(self, old_symbolic: List[Dict], new_symbolic: List[Dict],
                                symbolic_key: str, parent_path: str,
                                parent_node_type: str = "node") -> List[ChangeRecord]:
        """
        Compare symbolic entries (enum, bit, etc.) and detect all changes.
        
        This method detects:
        1. Added/deleted entries
        2. Value changes (e.g., enum value changed from 1 to 0)
        3. Attribute changes (description, status, etc.)
        
        Returns one ChangeRecord per symbolic entry for structural reporting.
        Each record has a full path like: parent_path/ENTRY_NAME
        
        Args:
            old_symbolic: List of old symbolic entries
            new_symbolic: List of new symbolic entries
            symbolic_key: The symbolic type key ('enum', 'bit', etc.)
            parent_path: Path to the parent node
            parent_node_type: Type of the parent node
            
        Returns:
            List of ChangeRecords, one per symbolic entry with changes
        """
        # Debug output
        print(f"\n[SymbolicComparator] Called for {parent_path}")
        print(f"  Symbolic key: {symbolic_key}")
        print(f"  Old: {len(old_symbolic)}, New: {len(new_symbolic)}")
        
        if 'duplex' in parent_path.lower():
            print(f"\n[SymbolicComparator] *** DUPLEX MODE DETECTED ***")
            print(f"  Old {symbolic_key}s: {len(old_symbolic)} entries")
            for e in old_symbolic:
                print(f"    - {e.get('name')}: value={e.get('value')}, full={e}")
            print(f"  New {symbolic_key}s: {len(new_symbolic)} entries")
            for e in new_symbolic:
                print(f"    - {e.get('name')}: value={e.get('value')}, full={e}")
        
        # Use list comparison strategy to get initial changes
        symbolic_changes = self.list_strategy.compare(old_symbolic, new_symbolic)
        
        # Debug: show what changes were detected
        print(f"  Detected {len(symbolic_changes)} changes")
        if 'duplex' in parent_path.lower():
            for change in symbolic_changes:
                print(f"    - {change.change_type.value}: {change.path}, details={len(change.details) if change.details else 0}")
        
        # Process changes and create one ChangeRecord per symbolic entry
        result_records = []
        
        for change in symbolic_changes:
            if not change.is_meaningful():
                continue
            
            if change.change_type == ChangeType.ADDED:
                # Symbolic entry was added - create separate record
                new_entry = change.new_value
                new_metadata = new_entry.get('metadata', {}) if isinstance(new_entry, dict) else {}
                entry_name = new_entry.get('name') if isinstance(new_entry, dict) else '<unknown>'
                entry_value = new_entry.get('value') if isinstance(new_entry, dict) else None
                
                # Create full path: parent_path/ENTRY_NAME
                entry_path = f"{parent_path}/{entry_name}"
                
                # Create attribute details for this entry
                attribute_details = [{
                    "type": "attribute_added",
                    "path": "name",
                    "old": None,
                    "new": entry_name
                }]
                
                if entry_value is not None:
                    attribute_details.append({
                        "type": "attribute_added",
                        "path": "value",
                        "old": None,
                        "new": entry_value
                    })
                
                # Add other attributes if present
                for attr_key in ['description', 'status', 'reference']:
                    if attr_key in new_entry:
                        attribute_details.append({
                            "type": "attribute_added",
                            "path": attr_key,
                            "old": None,
                            "new": new_entry[attr_key]
                        })
                
                # Store metadata in new_value itself (it's already there from parsing)
                result_records.append(ChangeRecord(
                    change_type=ChangeType.ADDED,
                    path=entry_path,
                    node_type=symbolic_key,
                    old_value=None,
                    new_value=new_entry,
                    details=attribute_details
                ))
                
            elif change.change_type == ChangeType.DELETED:
                # Symbolic entry was deleted - create separate record
                old_entry = change.old_value
                old_metadata = old_entry.get('metadata', {}) if isinstance(old_entry, dict) else {}
                entry_name = old_entry.get('name') if isinstance(old_entry, dict) else '<unknown>'
                entry_value = old_entry.get('value') if isinstance(old_entry, dict) else None
                
                # Create full path: parent_path/ENTRY_NAME
                entry_path = f"{parent_path}/{entry_name}"
                
                # Store metadata in old_value itself (it's already there from parsing)
                result_records.append(ChangeRecord(
                    change_type=ChangeType.DELETED,
                    path=entry_path,
                    node_type=symbolic_key,
                    old_value=old_entry,
                    new_value=None,
                    details=[]
                ))
                
            elif change.change_type == ChangeType.CHANGED:
                # Symbolic entry was changed - extract specific changes
                change_details = self._extract_symbolic_change_details(
                    change.old_value, change.new_value, symbolic_key
                )
                if change_details:
                    old_entry = change.old_value
                    new_entry = change.new_value
                    
                    # Extract metadata for proper formatting
                    old_metadata = old_entry.get('metadata', {}) if isinstance(old_entry, dict) else {}
                    new_metadata = new_entry.get('metadata', {}) if isinstance(new_entry, dict) else {}
                    
                    # Get the entry name and value
                    entry_name = old_entry.get('name') if isinstance(old_entry, dict) else '<unknown>'
                    old_value = old_entry.get('value') if isinstance(old_entry, dict) else None
                    new_value = new_entry.get('value') if isinstance(new_entry, dict) else None
                    
                    # Create full path: parent_path/ENTRY_NAME
                    entry_path = f"{parent_path}/{entry_name}"
                    
                    # _extract_symbolic_change_details already sets detail['type']
                    # for every entry, covering all add/delete/change cases for
                    # both constraints and plain attributes generically.
                    attribute_details = []
                    for detail in change_details:
                        attribute_details.append({
                            "type": detail["type"],
                            "path": detail.get('attribute'),
                            "old": detail.get('old'),
                            "new": detail.get('new'),
                            "severity": detail.get('severity', 'info')
                        })
                    
                    # Store metadata in old_value and new_value themselves (already there from parsing)
                    result_records.append(ChangeRecord(
                        change_type=ChangeType.CHANGED,
                        path=entry_path,
                        node_type=symbolic_key,
                        old_value=old_entry,
                        new_value=new_entry,
                        details=attribute_details
                    ))
        
        print(f"[SymbolicComparator] Returning {len(result_records)} separate ChangeRecords")
        return result_records

    
    # Severity hints for well-known attributes; all others default to 'info'.
    _ATTRIBUTE_SEVERITY: Dict[str, str] = {
        "value":    "error",    # Enum/bit value changes are non-backward-compatible
        "position": "warning",
        "status":   "warning",
    }

    def _extract_symbolic_change_details(self, old_entry: Dict, new_entry: Dict,
                                        symbolic_key: str) -> List[Dict]:
        """
        Extract detailed changes for a single symbolic entry.

        Iterates over the union of all keys present in either the old or new
        entry dict (excluding identity fields and internal metadata) and emits
        one detail record per changed key.  The detail type is derived
        generically from whether the attribute is a YANG constraint keyword and
        whether it was added, deleted, or changed:

            is_constraint & old is None          -> constraint_added
            is_constraint & new is None          -> constraint_deleted
            is_constraint & both present         -> constraint_changed
            not constraint & old is None         -> attribute_added
            not constraint & new is None         -> attribute_deleted
            not constraint & both present        -> attribute_changed

        This approach is attribute-agnostic: adding new sub-statements to YANG
        enum/bit entries (e.g. a future ``if-feature`` sub-stmt) is handled
        automatically without touching this method.

        Args:
            old_entry: Old symbolic entry dictionary
            new_entry: New symbolic entry dictionary
            symbolic_key: The symbolic type ('enum', 'bit')

        Returns:
            List of detail dicts, each with keys:
              ``type``, ``path`` (attribute name), ``old``, ``new``, ``severity``
        """
        if not isinstance(old_entry, dict) or not isinstance(new_entry, dict):
            return []

        entry_name = old_entry.get('name', new_entry.get('name', '<unknown>'))

        # Keys that identify the entry itself or carry internal bookkeeping —
        # never reported as individual attribute changes.
        # Also skip symbolic keyword keys (enum, bit) — these are reported as
        # structural (enum added/deleted) lines by the symbolic comparator and
        # should not be duplicated as flat "attribute changed: ['enum']" lines.
        skip_keys = {'name', 'metadata'} | SYMBOLIC_KEYWORDS

        all_keys = (set(old_entry.keys()) | set(new_entry.keys())) - skip_keys

        details = []
        for key in all_keys:
            old_val = old_entry.get(key)
            new_val = new_entry.get(key)

            if old_val == new_val:
                continue  # No change

            is_constraint = key in CONSTRAINT_KEYWORDS

            if old_val is None:
                detail_type = "constraint_added" if is_constraint else "attribute_added"
            elif new_val is None:
                detail_type = "constraint_deleted" if is_constraint else "attribute_deleted"
            else:
                detail_type = "constraint_changed" if is_constraint else "attribute_changed"

            severity = self._ATTRIBUTE_SEVERITY.get(key, "info")

            details.append({
                "type":      detail_type,
                "attribute": key,
                "path":      key,
                "old":       old_val,
                "new":       new_val,
                "severity":  severity,
                "message":   (
                    f"the {key} for {symbolic_key} '{entry_name}' "
                    f"has changed from {old_val} to {new_val}"
                ),
            })

        return details
    
    def compare_symbolic_in_typedef(self, old_symbolic: List[Dict], new_symbolic: List[Dict],
                                   symbolic_key: str, old_type_name: str,
                                   new_type_name: str, node_path: str) -> List[ChangeRecord]:
        """
        Compare symbolic entries within typedef definitions.
        
        Returns separate ChangeRecords for each enum/bit change, so each entry
        gets its own path in the report.
        
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
        symbolic_changes = self.list_strategy.compare(old_symbolic, new_symbolic)
        
        result_records = []
        for change in symbolic_changes:
            if not change.is_meaningful():
                continue
            
            change_type_str = change.change_type.value if isinstance(change.change_type, ChangeType) else change.change_type
            
            # Get entry name for path construction
            entry_name = None
            if change.old_value and isinstance(change.old_value, dict):
                entry_name = change.old_value.get('name')
            if not entry_name and change.new_value and isinstance(change.new_value, dict):
                entry_name = change.new_value.get('name')
            if not entry_name:
                entry_name = change.path  # Fallback
            
            # Construct full path: parent/symbolic_key/ENTRY_NAME
            # Including the symbolic type keyword (e.g. 'enumeration' / 'bits') as an
            # intermediate segment mirrors the structural path produced by the tree
            # traversal (community-type/enumeration/BOTH) and avoids collisions with
            # plain node names.
            entry_path = f"{node_path}/{symbolic_key}/{entry_name}"
            
            if change_type_str == 'added':
                # Create a separate ChangeRecord for this added enum/bit
                result_records.append(ChangeRecord(
                    change_type=ChangeType.ADDED,
                    path=entry_path,
                    node_type=symbolic_key,
                    old_value=None,
                    new_value=change.new_value,
                    details=[]
                ))
            elif change_type_str == 'deleted':
                # Create a separate ChangeRecord for this deleted enum/bit
                result_records.append(ChangeRecord(
                    change_type=ChangeType.DELETED,
                    path=entry_path,
                    node_type=symbolic_key,
                    old_value=change.old_value,
                    new_value=None,
                    details=[]
                ))
            else:
                # Extract detailed attribute changes for changed entries
                change_details = self._extract_symbolic_change_details(
                    change.old_value, change.new_value, symbolic_key
                )
                print(f"[SymbolicComparator] Extracted {len(change_details) if change_details else 0} detail changes for {symbolic_key} '{entry_name}'")
                if change_details:
                    for detail in change_details:
                        print(f"  - {detail.get('attribute')}: {detail.get('old')} -> {detail.get('new')}")
                
                # Convert to detail format expected by report generator.
                # _extract_symbolic_change_details already sets detail['type']
                # for every entry, covering all add/delete/change cases for
                # both constraints and plain attributes generically.
                attribute_details = []
                for detail in change_details:
                    attribute_details.append({
                        "type": detail["type"],
                        "path": detail.get('attribute'),
                        "old": detail.get('old'),
                        "new": detail.get('new'),
                        "severity": detail.get('severity', 'info')
                    })
                
                # Create a separate ChangeRecord for this changed enum/bit
                result_records.append(ChangeRecord(
                    change_type=ChangeType.CHANGED,
                    path=entry_path,
                    node_type=symbolic_key,
                    old_value=change.old_value,
                    new_value=change.new_value,
                    details=attribute_details
                ))
        
        if result_records:
            print(f"[SymbolicComparator] Returning {len(result_records)} separate {symbolic_key} ChangeRecords")
        
        return result_records
