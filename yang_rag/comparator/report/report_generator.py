"""
Report Generator for YANG Comparison Results.

This module generates human-readable reports from comparison results,
formatting changes with proper hierarchy and context.
"""

import os
import json
import io
from typing import Dict, List, Any

from yang_rag.comparator.core import ChangeRecord, ChangeType
from yang_rag.comparator.core import constants as core_constants


class ReportGenerator:
    """Generates human-readable reports from comparison results."""
    
    @staticmethod
    def generate_report(comparison_results: Dict[str, List[ChangeRecord]], 
                       output_file: str = "output/report.txt",
                       module_prefix: str = ""):
        """Generate a formatted report file."""
        os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
        
        with open(output_file, "w", encoding="utf-8") as f:
            for path in sorted(comparison_results.keys()):
                changes = comparison_results[path]
                if not any(change.is_meaningful() for change in changes):
                    continue
                
                # Add module prefix when needed, but avoid duplicating it.
                if module_prefix and path:
                    full_path = path if str(path).startswith(f"{module_prefix}/") else f"{module_prefix}/{path}"
                else:
                    full_path = module_prefix or path
                level = path.count("/") + 1

                # Buffer per-path output so we can skip orphan parent lines.
                path_buf = io.StringIO()
                detached_structural_changes = ReportGenerator._write_path_changes(
                    path_buf,
                    changes,
                    level,
                    full_path,
                    path,
                )

                path_output = path_buf.getvalue()
                if path_output:
                    f.write(path_output)

                for detached in detached_structural_changes:
                    detached_path = detached["path"]
                    f.write(f"{detached_path}\n")
                    detached_level = detached_path.count("/") + 1
                    ReportGenerator._write_detached_union_change(
                        f,
                        detached["detail"],
                        detached_level,
                    )
    
    @staticmethod
    def _write_path_changes(file_handle, changes: List[ChangeRecord], base_level: int, parent_path: str, parent_raw_path: str = ""):
        """Write all changes for a specific path."""
        main_index = 0
        detached_structural_changes = []
        path_written = False

        def _ensure_parent_path_written():
            nonlocal path_written
            if not path_written:
                file_handle.write(f"{parent_path}\n")
                path_written = True

        def _is_name_path(path_value: Any) -> bool:
            if path_value == "name":
                return True
            if isinstance(path_value, str):
                pv = path_value.strip()
                if pv == "name" or pv == "['name']" or pv == '["name"]':
                    return True
                simplified = pv.replace("[", "").replace("]", "").replace("'", "").replace('"', "").strip()
                if simplified == "name" or simplified.endswith(".name"):
                    return True
            if isinstance(path_value, (list, tuple)) and len(path_value) == 1:
                return str(path_value[0]).strip(" '\"") == "name"
            if isinstance(path_value, (list, tuple)):
                return any(str(p).strip(" '\"") == "name" for p in path_value)
            return False
        
        # Group changes by type to handle them appropriately
        node_changes = [c for c in changes if c.change_type in [ChangeType.ADDED, ChangeType.DELETED, ChangeType.CHANGED, ChangeType.RENAMED]]

        # Safety dedup at report layer: comparator output can occasionally
        # contain semantically identical changed records that differ only in
        # detail ordering. Collapse those so report.txt does not emit duplicate
        # path blocks.
        def _norm_val(v: Any):
            if isinstance(v, dict):
                ignored = {'path', 'type'}
                return tuple(sorted((str(k), _norm_val(val)) for k, val in v.items() if k not in ignored))
            if isinstance(v, list):
                return tuple(_norm_val(x) for x in v)
            return str(v)

        def _details_sig(details: List[Dict[str, Any]]) -> tuple:
            rows = []
            for d in details or []:
                row = []
                for k in sorted(d.keys()):
                    if k == 'path':
                        continue
                    row.append((k, _norm_val(d.get(k))))
                rows.append(tuple(row))
            return tuple(sorted(rows, key=repr))

        deduped_node_changes: List[ChangeRecord] = []
        seen_change_sigs = set()
        for ch in node_changes:
            sig = (
                str(ch.change_type),
                str(ch.node_type or ''),
                _details_sig(ch.details),
            )
            if sig in seen_change_sigs:
                continue
            seen_change_sigs.add(sig)
            deduped_node_changes.append(ch)
        node_changes = deduped_node_changes
        
        for change in node_changes:
            if not change.is_meaningful():
                continue
            if change.change_type in [ChangeType.CHANGED, ChangeType.RENAMED] and not change.details:
                continue

            # For uncovered structural add/delete records, emit a detached structural
            # path (e.g., .../prefix-length/default-value/126) similar to regular
            # structural nodes instead of collapsing under the parent leaf path.
            detached_path = ReportGenerator._build_detached_structural_path(parent_path, change)
            if detached_path:
                detached_level = detached_path.count("/") + 1
                metadata_str = ReportGenerator._format_metadata(change.old_value, change.new_value)
                file_handle.write(f"{detached_path}\n")
                if change.change_type == ChangeType.ADDED:
                    file_handle.write(f"  {detached_level}. ({change.node_type} added){metadata_str}\n")
                    ReportGenerator._write_detached_structural_payload(
                        file_handle,
                        detached_level,
                        change.new_value,
                        action="added",
                    )
                elif change.change_type == ChangeType.DELETED:
                    file_handle.write(f"  {detached_level}. ({change.node_type} deleted){metadata_str}\n")
                    ReportGenerator._write_detached_structural_payload(
                        file_handle,
                        detached_level,
                        change.old_value,
                        action="deleted",
                    )
                # Detached add/delete already reported above.
                continue

            # General rule: if the change already carries a concrete absolute
            # path different from the current parent bucket, render it under
            # its own path section.
            change_path = str(getattr(change, "path", "") or "")
            if change_path and "/" in change_path and change_path != str(parent_path):
                # Rebase detached change path to the same module-prefix root as
                # the current rendered parent path.
                if parent_raw_path and str(parent_path).endswith(str(parent_raw_path)):
                    path_prefix = str(parent_path)[: -len(str(parent_raw_path))]
                    if path_prefix and not change_path.startswith(path_prefix):
                        change_path = f"{path_prefix}{change_path}"

                detached_level = change_path.count("/") + 1
                metadata_str = ReportGenerator._format_metadata(change.old_value, change.new_value)
                file_handle.write(f"{change_path}\n")
                if change.change_type == ChangeType.ADDED:
                    file_handle.write(f"  {detached_level}. ({change.node_type} added){metadata_str}\n")
                    ReportGenerator._write_added_node_details(file_handle, change, detached_level, main_index)
                elif change.change_type == ChangeType.DELETED:
                    file_handle.write(f"  {detached_level}. ({change.node_type} deleted){metadata_str}\n")
                else:
                    file_handle.write(f"  {detached_level}. ({change.node_type} changed){metadata_str}\n")
                    if change.details:
                        ReportGenerator._write_change_details(
                            file_handle,
                            change.details,
                            detached_level,
                            main_index,
                            change_path,
                            detached_structural_changes,
                        )
                continue

            # For uncovered structural changed records, detach by changed name so
            # multiple structural instances are not collapsed under one parent path.
            known_structurals = core_constants.RULE_STRUCTURAL_KEYWORDS or core_constants.DEFAULT_STRUCTURAL_KEYWORDS
            if (
                change.change_type == ChangeType.CHANGED
                and str(change.node_type or "")
                and str(change.node_type) not in known_structurals
                and isinstance(change.details, list)
            ):
                name_detail = None
                for d in change.details:
                    if (
                        isinstance(d, dict)
                        and d.get("type") == "attribute_changed"
                        and _is_name_path(d.get("path"))
                        and d.get("new") is not None
                    ):
                        name_detail = d
                        break
                if name_detail is not None:
                    changed_name = ReportGenerator._format_value(name_detail.get("new"))
                    non_name_detail_count = sum(
                        1
                        for d in change.details
                        if isinstance(d, dict) and not (d.get("type") == "attribute_changed" and _is_name_path(d.get("path")))
                    )
                    # If current parent_path is already detached for this
                    # structural instance, do not append again.
                    already_detached_suffix = f"/{change.node_type}/{changed_name}"
                    marker = f"/{change.node_type}/"
                    if str(parent_path).endswith(already_detached_suffix):
                        detached_changed_path = parent_path
                    elif str(parent_path).endswith(f"/{change.node_type}"):
                        detached_changed_path = parent_path
                    elif marker in str(parent_path):
                        # Keep existing anchored path (typically old instance
                        # key) rather than rewriting to the new name.
                        detached_changed_path = parent_path
                    else:
                        detached_changed_path = f"{parent_path}/{change.node_type}/{changed_name}"
                    detached_level = detached_changed_path.count("/") + 1
                    metadata_str = ReportGenerator._format_metadata(change.old_value, change.new_value)
                    if detached_changed_path != parent_path:
                        file_handle.write(f"{detached_changed_path}\n")
                    else:
                        _ensure_parent_path_written()
                    file_handle.write(f"  {detached_level}. ({change.node_type} changed){metadata_str}\n")
                    ReportGenerator._write_change_details(
                        file_handle,
                        change.details,
                        detached_level,
                        main_index,
                        parent_path,
                        detached_structural_changes,
                    )
                    continue

            # Generic structural-instance path correction:
            # if a changed record is emitted under one structural instance path but
            # the details indicate the changed instance name is different, detach it
            # to the name-derived path.
            if (
                change.change_type == ChangeType.CHANGED
                and isinstance(change.details, list)
                and str(change.node_type or "")
            ):
                def _value_name(v: Any):
                    if isinstance(v, dict):
                        attrs = v.get("attributes") if isinstance(v.get("attributes"), dict) else {}
                        return attrs.get("name")
                    return None

                name_detail = None
                for d in change.details:
                    if (
                        isinstance(d, dict)
                        and d.get("type") == "attribute_changed"
                        and _is_name_path(d.get("path"))
                        and d.get("new") is not None
                    ):
                        name_detail = d
                        break

                # Avoid wrapper-only renames (e.g., dynamic-default name change)
                # which should stay at their original wrapper path.
                non_name_detail_count = sum(
                    1
                    for d in change.details
                    if isinstance(d, dict) and not (d.get("type") == "attribute_changed" and _is_name_path(d.get("path")))
                )

                node_type = str(change.node_type)
                marker = f"/{node_type}/"
                changed_name_raw = None
                if name_detail is not None:
                    changed_name_raw = name_detail.get("new")
                if changed_name_raw is None:
                    changed_name_raw = _value_name(change.new_value)

                if changed_name_raw is not None and marker in str(parent_path):
                    changed_name = ReportGenerator._format_value(changed_name_raw)
                    head, _tail = str(parent_path).rsplit(marker, 1)
                    current_name = _tail
                    detached_changed_path = f"{head}{marker}{changed_name}"
                    if str(changed_name) != str(current_name) and detached_changed_path != parent_path:
                        detached_level = detached_changed_path.count("/") + 1
                        metadata_str = ReportGenerator._format_metadata(change.old_value, change.new_value)
                        file_handle.write(f"{detached_changed_path}\n")
                        file_handle.write(f"  {detached_level}. ({change.node_type} changed){metadata_str}\n")
                        ReportGenerator._write_change_details(
                            file_handle,
                            change.details,
                            detached_level,
                            main_index,
                            parent_path,
                            detached_structural_changes,
                        )
                        continue
                
            main_index += 1
            
            # Extract metadata for the main change line
            metadata_str = ReportGenerator._format_metadata(change.old_value, change.new_value)

            # Generic helper: check if a details list has at least one attribute or
            # constraint detail that will be displayed inline (not detached).
            # Structural details (keyword-level changes) are detached and reported
            # as separate child entries — if ALL details are structural, the parent
            # line would be empty noise and should be suppressed.
            _ATTR_CONSTR_PREFIXES = ('attribute_', 'constraint_')

            def _has_displayable_details(details):
                return any(
                    str(d.get('type', '')).startswith(_ATTR_CONSTR_PREFIXES)
                    for d in (details or [])
                )

            # Write the main change line
            if change.change_type == ChangeType.ADDED:
                # Suppress empty '(X added)' lines that have no attribute/constraint
                # details to display (all details are structural child entries).
                _added_attrs = (change.new_value or {}).get('attributes', {}) if isinstance(change.new_value, dict) else {}
                _added_constraints = (change.new_value or {}).get('constraints', {}) if isinstance(change.new_value, dict) else {}
                if _added_attrs or _added_constraints or not change.details:
                    _ensure_parent_path_written()
                    file_handle.write(f"  {base_level}. ({change.node_type} added){metadata_str}\n")
                    ReportGenerator._write_added_node_details(file_handle, change, base_level, main_index)
                else:
                    # No direct attributes/constraints — only structural children.
                    # Still need to call _write_added_node_details to process detached
                    # structural changes, but don't write the parent header.
                    ReportGenerator._write_added_node_details(file_handle, change, base_level, main_index)
            
            elif change.change_type == ChangeType.DELETED:
                _ensure_parent_path_written()
                file_handle.write(f"  {base_level}. ({change.node_type} deleted){metadata_str}\n")
            
            elif change.change_type == ChangeType.CHANGED:
                # Suppress empty '(X changed)' lines that have no attribute/constraint
                # details to display (all details are structural child entries).
                if _has_displayable_details(change.details) or not change.details:
                    _ensure_parent_path_written()
                    file_handle.write(f"  {base_level}. ({change.node_type} changed){metadata_str}\n")
                if change.details:
                    ReportGenerator._write_change_details(
                        file_handle,
                        change.details,
                        base_level,
                        main_index,
                        parent_path,
                        detached_structural_changes,
                    )
            
            elif change.change_type == ChangeType.RENAMED:
                # Only write explicit rename line if no detailed change record already conveys info
                # Legacy 'renamed' now reported as standard changed with separate path detail; just emit header
                _ensure_parent_path_written()
                file_handle.write(f"  {base_level}. ({change.node_type} changed){metadata_str}\n")
                if change.details:
                    ReportGenerator._write_change_details(
                        file_handle,
                        change.details,
                        base_level,
                        main_index,
                        parent_path,
                        detached_structural_changes,
                    )

        return detached_structural_changes

    @staticmethod
    def _build_detached_structural_path(parent_path: str, change: ChangeRecord) -> str:
        """Build detached path for uncovered structural add/delete entries."""
        if change.change_type not in (ChangeType.ADDED, ChangeType.DELETED):
            return ""

        # Keep known built-in structural keywords in the regular flow.
        known_structurals = core_constants.RULE_STRUCTURAL_KEYWORDS or core_constants.DEFAULT_STRUCTURAL_KEYWORDS
        node_type = str(change.node_type or "")
        if not node_type or node_type in known_structurals:
            return ""

        payload = change.new_value if change.change_type == ChangeType.ADDED else change.old_value
        if not isinstance(payload, dict):
            return ""

        attrs = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
        name = attrs.get("name")
        if name is None or str(name) == "":
            return ""

        return f"{parent_path}/{node_type}/{name}"

    @staticmethod
    def _write_detached_structural_payload(file_handle, level: int, payload: Any, action: str):
        """Write full attributes/constraints for detached structural add/delete."""
        if not isinstance(payload, dict):
            return

        attrs = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
        cons = payload.get("constraints") if isinstance(payload.get("constraints"), dict) else {}

        idx = 0
        for key, val in attrs.items():
            if key in ("metadata", "children"):
                continue
            idx += 1
            file_handle.write(
                f"     {level}.{idx} attribute {action}: ['{key}'] -> {ReportGenerator._format_value(val)}\n"
            )

        for key, val in cons.items():
            idx += 1
            file_handle.write(
                f"     {level}.{idx} constraint {action}: ['{key}'] -> {ReportGenerator._format_value(val)}\n"
            )
    
    @staticmethod
    def _format_metadata(old_value: Any, new_value: Any) -> str:
        """Format metadata (line numbers and file paths) for report output.
        
        Returns a string like ' [file: module.yang, line: 123]' or
        ' [file: module.yang, old_line: 123, new_line: 456]'
        """
        old_metadata = None
        new_metadata = None
        
        if isinstance(old_value, dict) and "metadata" in old_value:
            old_metadata = old_value["metadata"]
        if isinstance(new_value, dict) and "metadata" in new_value:
            new_metadata = new_value["metadata"]
        
        if not old_metadata and not new_metadata:
            return ""
        
        parts = []
        
        # Handle file paths first (file comes before line numbers)
        old_file = old_metadata.get("file") if old_metadata else None
        new_file = new_metadata.get("file") if new_metadata else None
        
        if old_file is not None and new_file is not None:
            if old_file == new_file:
                parts.append(f"file: {old_file}")
            else:
                parts.append(f"old_file: {old_file}, new_file: {new_file}")
        elif old_file is not None:
            parts.append(f"file: {old_file}")
        elif new_file is not None:
            parts.append(f"file: {new_file}")
        
        # Handle line numbers after file
        old_line = old_metadata.get("line") if old_metadata else None
        new_line = new_metadata.get("line") if new_metadata else None
        
        if old_line is not None and new_line is not None:
            if old_line == new_line:
                parts.append(f"line: {old_line}")
            else:
                parts.append(f"old_line: {old_line}, new_line: {new_line}")
        elif old_line is not None:
            parts.append(f"line: {old_line}")
        elif new_line is not None:
            parts.append(f"line: {new_line}")
        
        # Include type_name metadata when present (for 'type' keyword nodes).
        # This allows the compatibility engine to determine the actual type
        # (e.g., 'leafref', 'int32') from the report line metadata, enabling
        # correct rule matching (e.g., parent="leafref" for XPath path rules).
        # Follows the same pattern as file/line: same value → type_name: X,
        # different values → old_type_name: X, new_type_name: Y.
        old_type_name = old_metadata.get("type_name") if old_metadata else None
        new_type_name = new_metadata.get("type_name") if new_metadata else None
        if old_type_name is not None and new_type_name is not None:
            if old_type_name == new_type_name:
                parts.append(f"type_name: {old_type_name}")
            else:
                parts.append(f"old_type_name: {old_type_name}, new_type_name: {new_type_name}")
        elif old_type_name is not None:
            parts.append(f"type_name: {old_type_name}")
        elif new_type_name is not None:
            parts.append(f"type_name: {new_type_name}")

        # Combine all parts into a single bracket
        return f" [{', '.join(parts)}]" if parts else ""
    
    @staticmethod
    def _write_added_node_details(file_handle, change: ChangeRecord, level: int, main_index: int):
        """Write detailed information about added nodes."""
        if not isinstance(change.new_value, dict):
            return
        
        attributes = change.new_value.get("attributes", {})
        sub_index = 0

        # Special-case: enumeration type nodes (node_type == 'type' and collected enum entries as an 'enum' attribute list)
        # The normalization puts enum statements into attributes['enum'] as a list of dicts each having an 'attributes' sub-dict.
        if change.node_type == 'type' and 'enum' in attributes and isinstance(attributes.get('enum'), list):
            enum_entries = attributes.get('enum') or []
            # Emit one (enum added) header per enum entry with flattened attribute children
            for enum_entry in enum_entries:
                sub_index += 1
                # Extract metadata for enum entry
                enum_metadata_str = ""
                if isinstance(enum_entry, dict) and "metadata" in enum_entry:
                    metadata = enum_entry["metadata"]
                    meta_parts = []
                    if "line" in metadata:
                        meta_parts.append(f"[line: {metadata['line']}]")
                    if "file" in metadata:
                        meta_parts.append(f"[file: {metadata['file']}]")
                    enum_metadata_str = " " + " ".join(meta_parts) if meta_parts else ""
                
                file_handle.write(f"     {level}.{sub_index} (enum added){enum_metadata_str}\n")
                enum_attrs = {}
                if isinstance(enum_entry, dict):
                    # Try both direct attributes and nested attributes structure
                    enum_attrs = enum_entry.get('attributes', {}) or enum_entry
                # Child attributes: description then name then value (if present)
                child_idx = 0
                for attr_key in ["description", "name", "value"]:
                    if attr_key in enum_attrs:
                        child_idx += 1
                        attr_val = ReportGenerator._format_value(enum_attrs[attr_key])
                        file_handle.write(f"        {level}.{sub_index}.{child_idx} attribute added: ['{attr_key}'] -> {attr_val}\n")
            # After all enum headers, print the 'name' attribute of the type itself (e.g., enumeration)
            if 'name' in attributes:
                sub_index += 1
                name_val = ReportGenerator._format_value(attributes['name'])
                file_handle.write(f"     {level}.{sub_index} attribute added: ['name'] -> {name_val}\n")
            # We're done handling this type node; do not fall through to generic attribute logic
            return

        # Special-case: bits type nodes (node_type == 'type' and collected bit entries as a 'bit' attribute list)
        # The normalization puts bit statements into attributes['bit'] as a list of dicts.
        if change.node_type == 'type' and 'bit' in attributes and isinstance(attributes.get('bit'), list):
            bit_entries = attributes.get('bit') or []
            # Emit one (bit added) header per bit entry with flattened attribute children
            for bit_entry in bit_entries:
                sub_index += 1
                file_handle.write(f"     {level}.{sub_index} (bit added)\n")
                bit_attrs = {}
                if isinstance(bit_entry, dict):
                    # Try both direct attributes and nested attributes structure
                    bit_attrs = bit_entry.get('attributes', {}) or bit_entry
                # Child attributes: description then name then position (if present)
                child_idx = 0
                for attr_key in ["description", "name", "position"]:
                    if attr_key in bit_attrs:
                        child_idx += 1
                        attr_val = ReportGenerator._format_value(bit_attrs[attr_key])
                        file_handle.write(f"        {level}.{sub_index}.{child_idx} attribute added: ['{attr_key}'] -> {attr_val}\n")
            # After all bit headers, print the 'name' attribute of the type itself (e.g., bits)
            if 'name' in attributes:
                sub_index += 1
                name_val = ReportGenerator._format_value(attributes['name'])
                file_handle.write(f"     {level}.{sub_index} attribute added: ['name'] -> {name_val}\n")
            # We're done handling this type node; do not fall through to generic attribute logic
            return
        
        # Write attributes, treating dict/list values as nested keyword blocks
        for key, val in attributes.items():
            # Skip synthetic 'keyword' entries
            if key == 'keyword':
                continue
            
            # Skip structural child keywords - they are reported separately with their own paths
            # to avoid duplicate reporting (once nested here, once as separate structural element)
            if key in core_constants.STRUCTURAL_CHILD_KEYWORDS:
                continue
            
            sub_index += 1

            # (Legacy path) Special-case: old representation where 'type' attribute itself carried enum list
            if key == 'type' and isinstance(val, dict) and 'enum' in val:
                enums = val.get('enum', [])
                # Emit each enum entry similar to the new node_type == 'type' path
                for enum_entry in enums:
                    file_handle.write(f"     {level}.{sub_index} (enum added)\n")
                    attr_idx = 0
                    if isinstance(enum_entry, dict):
                        # Try both direct attributes and nested attributes structure
                        enum_attrs = enum_entry.get('attributes', {}) or enum_entry
                        for attr_key in ["description", "name", "value"]:
                            if attr_key in enum_attrs:
                                attr_idx += 1
                                attr_val = ReportGenerator._format_value(enum_attrs[attr_key])
                                file_handle.write(f"        {level}.{sub_index}.{attr_idx} attribute added: ['{attr_key}'] -> {attr_val}\n")
                    # Advance sub_index for next enum header
                    sub_index += 1
                # After loop, write the type name if present (avoid double increment from for-loop header)
                if 'name' in val:
                    file_handle.write(f"     {level}.{sub_index} attribute added: ['name'] -> {ReportGenerator._format_value(val['name'])}\n")
                continue

            # (Legacy path) Special-case: old representation where 'type' attribute itself carried bit list
            if key == 'type' and isinstance(val, dict) and 'bit' in val:
                bits = val.get('bit', [])
                # Emit each bit entry similar to the new node_type == 'type' path
                for bit_entry in bits:
                    file_handle.write(f"     {level}.{sub_index} (bit added)\n")
                    attr_idx = 0
                    if isinstance(bit_entry, dict):
                        # Try both direct attributes and nested attributes structure
                        bit_attrs = bit_entry.get('attributes', {}) or bit_entry
                        for attr_key in ["description", "name", "position"]:
                            if attr_key in bit_attrs:
                                attr_idx += 1
                                attr_val = ReportGenerator._format_value(bit_attrs[attr_key])
                                file_handle.write(f"        {level}.{sub_index}.{attr_idx} attribute added: ['{attr_key}'] -> {attr_val}\n")
                    # Advance sub_index for next bit header
                    sub_index += 1
                # After loop, write the type name if present (avoid double increment from for-loop header)
                if 'name' in val:
                    file_handle.write(f"     {level}.{sub_index} attribute added: ['name'] -> {ReportGenerator._format_value(val['name'])}\n")
                continue

            # If the attribute value is a dict, synthesize a header and print its children (skip 'children' and 'metadata' keys)
            if isinstance(val, dict):
                # Extract metadata for the header if present
                metadata_str = ""
                if 'metadata' in val:
                    meta = val['metadata']
                    meta_parts = []
                    if isinstance(meta, dict):
                        if 'file' in meta:
                            meta_parts.append(f"file: {meta['file']}")
                        if 'line' in meta:
                            meta_parts.append(f"line: {meta['line']}")
                    metadata_str = f" [{', '.join(meta_parts)}]" if meta_parts else ""
                
                file_handle.write(f"     {level}.{sub_index} ({key} added){metadata_str}\n")
                child_idx = 0
                for k, v in val.items():
                    if k in ('children', 'metadata'):  # Skip both children and metadata
                        continue
                    # Skip symbolic keywords (enum, bit) — they are reported structurally
                    if k in core_constants.SYMBOLIC_KEYWORDS:
                        continue
                    child_idx += 1
                    line_kind = 'constraint' if k in core_constants.CONSTRAINT_KEYWORDS else 'attribute'
                    file_handle.write(f"        {level}.{sub_index}.{child_idx} {line_kind} added: ['{k}'] -> {ReportGenerator._format_value(v)}\n")

            # If the attribute value is a list, emit a header and print each item's key/value pairs (skip 'children')
            elif isinstance(val, list):
                file_handle.write(f"     {level}.{sub_index} ({key} added)\n")
                item_idx = 0
                for item in val:
                    if isinstance(item, dict):
                        # flatten dict items directly under the list header
                        for ak, av in item.items():
                            if ak == 'children':
                                continue
                            item_idx += 1
                            line_kind = 'constraint' if ak in core_constants.CONSTRAINT_KEYWORDS else 'attribute'
                            file_handle.write(f"        {level}.{sub_index}.{item_idx} {line_kind} added: ['{ak}'] -> {ReportGenerator._format_value(av)}\n")
                    else:
                        item_idx += 1
                        file_handle.write(f"        {level}.{sub_index}.{item_idx} attribute added: ['value'] -> {ReportGenerator._format_value(item)}\n")

            # Primitive values: normal attribute/constraint line
            else:
                val_repr = ReportGenerator._format_value(val)
                line_kind = 'constraint' if key in core_constants.CONSTRAINT_KEYWORDS else 'attribute'
                file_handle.write(f"     {level}.{sub_index} {line_kind} added: ['{key}'] -> {val_repr}\n")
        # Print constraints if present
        constraints = change.new_value.get("constraints", {}) if isinstance(change.new_value, dict) else {}
        for ckey, cval in constraints.items():
            sub_index += 1
            cval_repr = ReportGenerator._format_value(cval)
            file_handle.write(f"     {level}.{sub_index} constraint added: ['{ckey}'] -> {cval_repr}\n")
    
    @staticmethod
    def _write_change_details(
        file_handle,
        details: List[Dict],
        level: int,
        main_index: int,
        parent_path: str = "",
        detached_structural_changes: List[Dict[str, Any]] = None,
    ):
        """Write detailed change information."""
        sub_index = 0
        
        for detail in details:
            detail_type = detail.get("type", "unknown")
            path = detail.get("path", "unknown")
            path_str = str(path)
            
            # Skip redundant 'values' or 'bits' attribute list 
            # These are the list of enum names or bit names, which are redundant when we report individual changes
            # The path typically contains "typedef" and ends with "/values" or "/bits"
            if "typedef" in path_str and (path_str.endswith("/values") or path_str.endswith("/bits")):
                continue
            
            # detail_type format: "<kind>_<action>" e.g. "attribute_added" or "constraint_changed"
            if '_' in detail_type:
                kind, action = detail_type.split('_', 1)
            else:
                kind, action = ('attribute', detail_type)

            # Check for specific symbolic types FIRST before generic handlers
            if detail_type == "enum_changed":
                sub_index += 1
                enum_name = detail.get("name", path)
                
                # Extract metadata for enum
                metadata_parts = []
                if detail.get("old_file") and detail.get("new_file"):
                    metadata_parts.append(f"file: {detail['new_file']}")
                if detail.get("old_line") is not None and detail.get("new_line") is not None:
                    metadata_parts.append(f"old_line: {detail['old_line']}, new_line: {detail['new_line']}")
                metadata_str = f" [{', '.join(metadata_parts)}]" if metadata_parts else ""
                
                # Show value change if present
                value_info = ""
                if detail.get("old_value") is not None and detail.get("new_value") is not None:
                    value_info = f" (value: {detail['old_value']} → {detail['new_value']})"
                
                file_handle.write(f"     {level}.{sub_index} enum changed: ['{enum_name}']{value_info}{metadata_str}\n")
                
                # Write enum attribute changes
                enum_details = detail.get("details", [])
                attr_index = 0
                for enum_change in enum_details:
                    attr_index += 1
                    change_type = enum_change.get("type", "")
                    attr_path = enum_change.get("path", "")
                    
                    if change_type == "attribute_changed":
                        old_val = ReportGenerator._format_value(enum_change.get("old"))
                        new_val = ReportGenerator._format_value(enum_change.get("new"))
                        file_handle.write(f"        {level}.{sub_index}.{attr_index} attribute changed: ['{attr_path}'] -> {new_val} (was {old_val})\n")
                    elif change_type == "attribute_added":
                        new_val = ReportGenerator._format_value(enum_change.get("new"))
                        file_handle.write(f"        {level}.{sub_index}.{attr_index} attribute added: ['{attr_path}'] -> {new_val}\n")
            
            elif detail_type == "enum_added":
                sub_index += 1
                enum_name = detail.get("name", path)
                
                # Extract metadata
                metadata_parts = []
                if detail.get("file"):
                    metadata_parts.append(f"file: {detail['file']}")
                if detail.get("line") is not None:
                    metadata_parts.append(f"line: {detail['line']}")
                metadata_str = f" [{', '.join(metadata_parts)}]" if metadata_parts else ""
                
                # Show value if present
                value_info = ""
                if detail.get("value") is not None:
                    value_info = f" (value: {detail['value']})"
                
                file_handle.write(f"     {level}.{sub_index} enum added: ['{enum_name}']{value_info}{metadata_str}\n")
            
            elif detail_type == "enum_deleted":
                sub_index += 1
                enum_name = detail.get("name", path)
                
                # Extract metadata
                metadata_parts = []
                if detail.get("file"):
                    metadata_parts.append(f"file: {detail['file']}")
                if detail.get("line") is not None:
                    metadata_parts.append(f"line: {detail['line']}")
                metadata_str = f" [{', '.join(metadata_parts)}]" if metadata_parts else ""
                
                # Show value if present
                value_info = ""
                if detail.get("value") is not None:
                    value_info = f" (value: {detail['value']})"
                
                file_handle.write(f"     {level}.{sub_index} enum deleted: ['{enum_name}']{value_info}{metadata_str}\n")

            elif detail_type == "bit_changed":
                sub_index += 1
                bit_name = detail.get("name", path)
                
                # Extract metadata for bit
                metadata_parts = []
                if detail.get("old_file") and detail.get("new_file"):
                    metadata_parts.append(f"file: {detail['new_file']}")
                if detail.get("old_line") is not None and detail.get("new_line") is not None:
                    metadata_parts.append(f"old_line: {detail['old_line']}, new_line: {detail['new_line']}")
                metadata_str = f" [{', '.join(metadata_parts)}]" if metadata_parts else ""
                
                # Show position change if present
                value_info = ""
                if detail.get("old_value") is not None and detail.get("new_value") is not None:
                    value_info = f" (position: {detail['old_value']} → {detail['new_value']})"
                
                file_handle.write(f"     {level}.{sub_index} bit changed: ['{bit_name}']{value_info}{metadata_str}\n")
            
            elif detail_type == "bit_added":
                sub_index += 1
                bit_name = detail.get("name", path)
                
                # Extract metadata
                metadata_parts = []
                if detail.get("file"):
                    metadata_parts.append(f"file: {detail['file']}")
                if detail.get("line") is not None:
                    metadata_parts.append(f"line: {detail['line']}")
                metadata_str = f" [{', '.join(metadata_parts)}]" if metadata_parts else ""
                
                # Show position if present (bits use "position" not "value")
                value_info = ""
                if detail.get("value") is not None:
                    value_info = f" (position: {detail['value']})"
                
                file_handle.write(f"     {level}.{sub_index} bit added: ['{bit_name}']{value_info}{metadata_str}\n")
            
            elif detail_type == "bit_deleted":
                sub_index += 1
                bit_name = detail.get("name", path)
                
                # Extract metadata
                metadata_parts = []
                if detail.get("file"):
                    metadata_parts.append(f"file: {detail['file']}")
                if detail.get("line") is not None:
                    metadata_parts.append(f"line: {detail['line']}")
                metadata_str = f" [{', '.join(metadata_parts)}]" if metadata_parts else ""
                
                # Show position if present
                value_info = ""
                if detail.get("value") is not None:
                    value_info = f" (position: {detail['value']})"
                
                file_handle.write(f"     {level}.{sub_index} bit deleted: ['{bit_name}']{value_info}{metadata_str}\n")

            elif detail_type in ("union_type_changed", "union_type_added", "union_type_deleted") and detached_structural_changes is not None:
                detached_structural_changes.append(
                    {
                        "path": ReportGenerator._build_union_member_path(parent_path, detail),
                        "detail": detail,
                    }
                )
                continue

            elif detail_type == "union_type_changed":
                sub_index += 1
                member_name = detail.get("path", path)
                old_member = detail.get("old") if isinstance(detail.get("old"), dict) else {}
                new_member = detail.get("new") if isinstance(detail.get("new"), dict) else {}

                metadata_parts = []
                old_meta = old_member.get("metadata") if isinstance(old_member.get("metadata"), dict) else {}
                new_meta = new_member.get("metadata") if isinstance(new_member.get("metadata"), dict) else {}
                old_file = old_meta.get("file")
                new_file = new_meta.get("file")
                old_line = old_meta.get("line")
                new_line = new_meta.get("line")

                if old_file and new_file:
                    if old_file == new_file:
                        metadata_parts.append(f"file: {old_file}")
                    else:
                        metadata_parts.append(f"old_file: {old_file}, new_file: {new_file}")
                elif old_file:
                    metadata_parts.append(f"file: {old_file}")
                elif new_file:
                    metadata_parts.append(f"file: {new_file}")

                if old_line is not None and new_line is not None:
                    if old_line == new_line:
                        metadata_parts.append(f"line: {old_line}")
                    else:
                        metadata_parts.append(f"old_line: {old_line}, new_line: {new_line}")
                elif old_line is not None:
                    metadata_parts.append(f"line: {old_line}")
                elif new_line is not None:
                    metadata_parts.append(f"line: {new_line}")

                metadata_str = f" [{', '.join(metadata_parts)}]" if metadata_parts else ""
                # Report union-member deltas as type changes rather than flattened type/union attributes.
                file_handle.write(f"     {level}.{sub_index} (type changed){metadata_str}\n")
                # Emit inner sub-details as individual constraint/attribute lines
                # so check_compatibility.py can apply its range/length/pattern rules
                inner_details = detail.get("details", [])
                emitted_name = False
                for inner_idx, inner in enumerate(inner_details, 1):
                    inner_path = inner.get("path", "")
                    inner_old = ReportGenerator._format_value(inner.get("old"))
                    inner_new = ReportGenerator._format_value(inner.get("new"))
                    inner_type = inner.get("type", "")
                    from .. import core as core_constants_mod
                    _constraint_kws = getattr(core_constants_mod, 'constants', None)
                    # Determine if this is a constraint keyword (range, length, pattern, etc.)
                    try:
                        from ..core.constants import CONSTRAINT_KEYWORDS
                        line_kind = 'constraint' if inner_path in CONSTRAINT_KEYWORDS else 'attribute'
                    except ImportError:
                        line_kind = 'constraint' if inner_path in ('range', 'length', 'pattern', 'fraction-digits') else 'attribute'
                    if inner_type == "attribute_changed":
                        file_handle.write(f"        {level}.{sub_index}.{inner_idx} {line_kind} changed: ['{inner_path}'] -> {inner_new} (was {inner_old})\n")
                        if inner_path == "name":
                            emitted_name = True
                    elif inner_type == "attribute_added":
                        file_handle.write(f"        {level}.{sub_index}.{inner_idx} {line_kind} added: ['{inner_path}'] -> {inner_new}\n")
                        if inner_path == "name":
                            emitted_name = True
                    elif inner_type in ("attribute_removed", "attribute_deleted"):
                        file_handle.write(f"        {level}.{sub_index}.{inner_idx} {line_kind} deleted: ['{inner_path}'] -> {inner_old}\n")
                        if inner_path == "name":
                            emitted_name = True

                if not emitted_name:
                    old_name = old_member.get("name") if isinstance(old_member, dict) else None
                    new_name = new_member.get("name") if isinstance(new_member, dict) else None
                    # Only emit the name fallback when the name actually changed.
                    # When old_name == new_name the type name is unchanged and
                    # emitting it would produce a spurious false-positive diff.
                    if old_name != new_name:
                        fallback_name = new_name or old_name or member_name
                        # Use the next available index to avoid colliding with
                        # already-emitted inner_details lines.
                        fallback_idx = len(inner_details) + 1
                        file_handle.write(f"        {level}.{sub_index}.{fallback_idx} attribute changed: ['name'] -> {fallback_name} (was {old_name if old_name is not None else fallback_name})\n")

            elif detail_type == "union_type_added":
                sub_index += 1
                member_name = detail.get("path", path)
                new_member = detail.get("new") if isinstance(detail.get("new"), dict) else {}

                metadata_parts = []
                new_meta = new_member.get("metadata") if isinstance(new_member.get("metadata"), dict) else {}
                if new_meta.get("file"):
                    metadata_parts.append(f"file: {new_meta['file']}")
                if new_meta.get("line") is not None:
                    metadata_parts.append(f"line: {new_meta['line']}")
                metadata_str = f" [{', '.join(metadata_parts)}]" if metadata_parts else ""

                file_handle.write(f"     {level}.{sub_index} (type changed){metadata_str}\n")
                name_val = ReportGenerator._format_value(new_member.get("name", member_name))
                file_handle.write(f"        {level}.{sub_index}.1 attribute added: ['name'] -> {name_val}\n")

                child_idx = 1
                for attr_key, attr_val in new_member.items():
                    if attr_key in ("name", "metadata", "children", "type"):
                        continue
                    # Skip symbolic keyword keys (enum, bit) — reported as structural lines
                    if attr_key in core_constants.SYMBOLIC_KEYWORDS:
                        continue
                    child_idx += 1
                    line_kind = 'constraint' if attr_key in core_constants.CONSTRAINT_KEYWORDS else 'attribute'
                    file_handle.write(
                        f"        {level}.{sub_index}.{child_idx} {line_kind} added: ['{attr_key}'] -> {ReportGenerator._format_value(attr_val)}\n"
                    )

            elif detail_type == "union_type_deleted":
                sub_index += 1
                member_name = detail.get("path", path)
                old_member = detail.get("old") if isinstance(detail.get("old"), dict) else {}

                metadata_parts = []
                old_meta = old_member.get("metadata") if isinstance(old_member.get("metadata"), dict) else {}
                if old_meta.get("file"):
                    metadata_parts.append(f"file: {old_meta['file']}")
                if old_meta.get("line") is not None:
                    metadata_parts.append(f"line: {old_meta['line']}")
                metadata_str = f" [{', '.join(metadata_parts)}]" if metadata_parts else ""

                file_handle.write(f"     {level}.{sub_index} (type changed){metadata_str}\n")
                name_val = ReportGenerator._format_value(old_member.get("name", member_name))
                file_handle.write(f"        {level}.{sub_index}.1 attribute deleted: ['name'] -> {name_val}\n")

                child_idx = 1
                for attr_key, attr_val in old_member.items():
                    if attr_key in ("name", "metadata", "children", "type"):
                        continue
                    # Skip symbolic keyword keys (enum, bit) — reported as structural lines
                    if attr_key in core_constants.SYMBOLIC_KEYWORDS:
                        continue
                    child_idx += 1
                    line_kind = 'constraint' if attr_key in core_constants.CONSTRAINT_KEYWORDS else 'attribute'
                    file_handle.write(
                        f"        {level}.{sub_index}.{child_idx} {line_kind} deleted: ['{attr_key}'] -> {ReportGenerator._format_value(attr_val)}\n"
                    )

            # Special-case: explode dict-valued 'root' attribute change into individual key-level lines
            elif action == "changed" and kind == "attribute" and path == "root":
                old_val = detail.get("old")
                new_val = detail.get("new")
                old_map = old_val if isinstance(old_val, dict) else {}
                new_map = new_val if isinstance(new_val, dict) else {}
                if old_map or new_map:
                    keys = sorted(set(old_map.keys()) | set(new_map.keys()))
                    for k in keys:
                        o = old_map.get(k, None)
                        n = new_map.get(k, None)
                        if o is None and n is not None:
                            sub_index += 1
                            line_kind = 'constraint' if k in core_constants.CONSTRAINT_KEYWORDS else 'attribute'
                            file_handle.write(f"     {level}.{sub_index} {line_kind} added: ['{k}'] -> {ReportGenerator._format_value(n)}\n")
                        elif o is not None and n is None:
                            sub_index += 1
                            line_kind = 'constraint' if k in core_constants.CONSTRAINT_KEYWORDS else 'attribute'
                            file_handle.write(f"     {level}.{sub_index} {line_kind} deleted: ['{k}'] -> {ReportGenerator._format_value(o)}\n")
                        elif ReportGenerator._format_value(o) != ReportGenerator._format_value(n):
                            sub_index += 1
                            line_kind = 'constraint' if k in core_constants.CONSTRAINT_KEYWORDS else 'attribute'
                            file_handle.write(f"     {level}.{sub_index} {line_kind} changed: ['{k}'] -> {ReportGenerator._format_value(n)} (was {ReportGenerator._format_value(o)})\n")
                    continue

            # Generic attribute/constraint handlers (for non-enum/bit changes)
            elif action == "changed":
                # Skip symbolic keyword keys (enum, bit) — these are reported as structural
                # (enum changed) / (bit changed) lines by the symbolic comparator and should
                # not be duplicated as flat "attribute changed: ['enum']" lines.
                if path in core_constants.SYMBOLIC_KEYWORDS:
                    continue
                sub_index += 1
                old_val = ReportGenerator._format_value(detail.get("old"))
                new_val = ReportGenerator._format_value(detail.get("new"))
                # Classify based on the actual path/key name, not the detail type
                line_kind = 'constraint' if path in core_constants.CONSTRAINT_KEYWORDS else kind
                
                file_handle.write(f"     {level}.{sub_index} {line_kind} changed: ['{path}'] -> {new_val} (was {old_val})\n")
            elif action == "added":
                # Skip symbolic keyword keys (enum, bit) — reported as structural lines
                if path in core_constants.SYMBOLIC_KEYWORDS:
                    continue
                sub_index += 1
                new_val = ReportGenerator._format_value(detail.get("new"))
                # Classify based on the actual path/key name, not the detail type
                line_kind = 'constraint' if path in core_constants.CONSTRAINT_KEYWORDS else kind
                if detail.get("new") is None:
                    file_handle.write(f"     {level}.{sub_index} {line_kind} added: ['{path}']\n")
                else:
                    file_handle.write(f"     {level}.{sub_index} {line_kind} added: ['{path}'] -> {new_val}\n")
            elif action == "deleted":
                # Skip symbolic keyword keys (enum, bit) — reported as structural lines
                if path in core_constants.SYMBOLIC_KEYWORDS:
                    continue
                sub_index += 1
                old_val = ReportGenerator._format_value(detail.get("old"))
                # Classify based on the actual path/key name, not the detail type
                line_kind = 'constraint' if path in core_constants.CONSTRAINT_KEYWORDS else kind
                if detail.get("old") is None:
                    file_handle.write(f"     {level}.{sub_index} {line_kind} deleted: ['{path}']\n")
                else:
                    file_handle.write(f"     {level}.{sub_index} {line_kind} deleted: ['{path}'] -> {old_val}\n")

    @staticmethod
    def _build_union_member_path(parent_path: str, detail: Dict[str, Any]) -> str:
        """Build a standalone report path for detached union member changes."""
        member_name = str(detail.get("path", "unknown"))
        return f"{parent_path}/type/union/{member_name}"

    @staticmethod
    def _write_detached_union_change(file_handle, detail: Dict[str, Any], level: int):
        """Write union type member changes as standalone top-level structural entries."""
        detail_type = detail.get("type", "")

        if detail_type == "union_type_changed":
            old_member = detail.get("old") if isinstance(detail.get("old"), dict) else {}
            new_member = detail.get("new") if isinstance(detail.get("new"), dict) else {}

            metadata_parts = []
            old_meta = old_member.get("metadata") if isinstance(old_member.get("metadata"), dict) else {}
            new_meta = new_member.get("metadata") if isinstance(new_member.get("metadata"), dict) else {}
            old_file = old_meta.get("file")
            new_file = new_meta.get("file")
            old_line = old_meta.get("line")
            new_line = new_meta.get("line")

            if old_file and new_file:
                if old_file == new_file:
                    metadata_parts.append(f"file: {old_file}")
                else:
                    metadata_parts.append(f"old_file: {old_file}, new_file: {new_file}")
            elif old_file:
                metadata_parts.append(f"file: {old_file}")
            elif new_file:
                metadata_parts.append(f"file: {new_file}")

            if old_line is not None and new_line is not None:
                if old_line == new_line:
                    metadata_parts.append(f"line: {old_line}")
                else:
                    metadata_parts.append(f"old_line: {old_line}, new_line: {new_line}")
            elif old_line is not None:
                metadata_parts.append(f"line: {old_line}")
            elif new_line is not None:
                metadata_parts.append(f"line: {new_line}")

            metadata_str = f" [{', '.join(metadata_parts)}]" if metadata_parts else ""
            file_handle.write(f"  {level}. (type changed){metadata_str}\n")

            inner_details = detail.get("details", [])
            emitted_name = False
            for inner_idx, inner in enumerate(inner_details, 1):
                inner_path = inner.get("path", "")
                inner_old = ReportGenerator._format_value(inner.get("old"))
                inner_new = ReportGenerator._format_value(inner.get("new"))
                inner_type = inner.get("type", "")
                line_kind = 'constraint' if inner_path in core_constants.CONSTRAINT_KEYWORDS else 'attribute'
                if inner_type == "attribute_changed":
                    file_handle.write(f"     {level}.1.{inner_idx} {line_kind} changed: ['{inner_path}'] -> {inner_new} (was {inner_old})\n")
                    if inner_path == "name":
                        emitted_name = True
                elif inner_type == "attribute_added":
                    file_handle.write(f"     {level}.1.{inner_idx} {line_kind} added: ['{inner_path}'] -> {inner_new}\n")
                    if inner_path == "name":
                        emitted_name = True
                elif inner_type in ("attribute_removed", "attribute_deleted"):
                    file_handle.write(f"     {level}.1.{inner_idx} {line_kind} deleted: ['{inner_path}'] -> {inner_old}\n")
                    if inner_path == "name":
                        emitted_name = True

            if not emitted_name:
                old_name = old_member.get("name") if isinstance(old_member, dict) else None
                new_name = new_member.get("name") if isinstance(new_member, dict) else None
                # Only emit the name fallback when the name actually changed.
                # When old_name == new_name the type name is unchanged and
                # emitting it would produce a spurious false-positive diff.
                if old_name != new_name:
                    fallback_name = new_name or old_name or "unknown"
                    # Use the next available index to avoid colliding with
                    # already-emitted inner_details lines.
                    fallback_idx = len(inner_details) + 1
                    file_handle.write(f"     {level}.1.{fallback_idx} attribute changed: ['name'] -> {fallback_name} (was {old_name if old_name is not None else fallback_name})\n")

        elif detail_type == "union_type_added":
            member_name = detail.get("path", "unknown")
            new_member = detail.get("new") if isinstance(detail.get("new"), dict) else {}

            metadata_parts = []
            new_meta = new_member.get("metadata") if isinstance(new_member.get("metadata"), dict) else {}
            if new_meta.get("file"):
                metadata_parts.append(f"file: {new_meta['file']}")
            if new_meta.get("line") is not None:
                metadata_parts.append(f"line: {new_meta['line']}")
            metadata_str = f" [{', '.join(metadata_parts)}]" if metadata_parts else ""

            file_handle.write(f"  {level}. (type added){metadata_str}\n")
            name_val = ReportGenerator._format_value(new_member.get("name", member_name))
            file_handle.write(f"     {level}.1.1 attribute added: ['name'] -> {name_val}\n")

            child_idx = 1
            for attr_key, attr_val in new_member.items():
                if attr_key in ("name", "metadata", "children", "type"):
                    continue
                child_idx += 1
                line_kind = 'constraint' if attr_key in core_constants.CONSTRAINT_KEYWORDS else 'attribute'
                file_handle.write(
                    f"     {level}.1.{child_idx} {line_kind} added: ['{attr_key}'] -> {ReportGenerator._format_value(attr_val)}\n"
                )

        elif detail_type == "union_type_deleted":
            member_name = detail.get("path", "unknown")
            old_member = detail.get("old") if isinstance(detail.get("old"), dict) else {}

            metadata_parts = []
            old_meta = old_member.get("metadata") if isinstance(old_member.get("metadata"), dict) else {}
            if old_meta.get("file"):
                metadata_parts.append(f"file: {old_meta['file']}")
            if old_meta.get("line") is not None:
                metadata_parts.append(f"line: {old_meta['line']}")
            metadata_str = f" [{', '.join(metadata_parts)}]" if metadata_parts else ""

            file_handle.write(f"  {level}. (type deleted){metadata_str}\n")
            name_val = ReportGenerator._format_value(old_member.get("name", member_name))
            file_handle.write(f"     {level}.1.1 attribute deleted: ['name'] -> {name_val}\n")

            child_idx = 1
            for attr_key, attr_val in old_member.items():
                if attr_key in ("name", "metadata", "children", "type"):
                    continue
                # Skip symbolic keyword keys (enum, bit) — reported as structural lines
                if attr_key in core_constants.SYMBOLIC_KEYWORDS:
                    continue
                child_idx += 1
                line_kind = 'constraint' if attr_key in core_constants.CONSTRAINT_KEYWORDS else 'attribute'
                file_handle.write(
                    f"     {level}.1.{child_idx} {line_kind} deleted: ['{attr_key}'] -> {ReportGenerator._format_value(attr_val)}\n"
                )
    
    @staticmethod
    def _format_value(value: Any) -> str:
        """Format a value for display in the report."""
        if value is None:
            return "None"
        
        if isinstance(value, str) and '\n' in value:
            lines = value.split('\n')
            if len(lines) > 1:
                # Format multiline strings with proper indentation
                continuation_lines = []
                for line in lines[1:]:
                    continuation_lines.append(' ' * 8 + line)
                return lines[0] + '\n' + '\n'.join(continuation_lines)
        
        try:
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False)
            return str(value)
        except Exception:
            return str(value)
