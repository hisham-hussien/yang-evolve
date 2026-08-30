"""Helpers for uncovered structural/attribute wrapper logic.

These utilities centralize generic behaviors used by comparator cleanup and
promotion logic so extension handling stays concept-based rather than keyword-
specific.
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, Optional, Set, Tuple

from ..core.constants import ChangeType


def is_uncovered_scalar_extension_statement(is_extension_stmt: bool, stmt_has_children: bool) -> bool:
    """Return True when an uncovered extension statement should be treated as scalar attribute."""
    return bool(is_extension_stmt and not stmt_has_children)


def find_single_wrapper_owner(
    old_attrs: Dict,
    new_attrs: Dict,
    candidate_paths: Iterable[str],
    normalize_keyword: Callable[[str], str],
) -> Optional[str]:
    """Return wrapper keyword when changed candidate fields are owned by exactly one wrapper."""
    owners: Set[str] = set()
    normalized_candidates = {normalize_keyword(c) for c in candidate_paths if str(c)}
    if not normalized_candidates:
        return None

    for attrs in (old_attrs, new_attrs):
        if not isinstance(attrs, dict):
            continue
        for wrapper_key, wrapper_value in attrs.items():
            if not isinstance(wrapper_value, dict):
                continue
            wrapper_attrs = wrapper_value.get("attributes") if isinstance(wrapper_value.get("attributes"), dict) else {}
            child_keys = {normalize_keyword(k) for k in wrapper_attrs.keys()}
            if child_keys & normalized_candidates:
                owners.add(normalize_keyword(wrapper_key))

    if len(owners) == 1:
        return next(iter(owners))
    return None


def drop_redundant_wrapper_name_echoes(
    cleaned_pairs: list,
    meta_file_line: Callable,
) -> list:
    """Remove redundant wrapper-only CHANGED echoes when a richer sibling exists.

    Echo pattern:
    - one record has only name old->new where values are path-like
    - another record with same keyword and file/line has path old->new with same values
    """

    def _changed_transitions(change_record) -> Dict[str, Tuple[str, str]]:
        out: Dict[str, Tuple[str, str]] = {}
        if change_record.change_type != ChangeType.CHANGED:
            return out
        for detail in change_record.details or []:
            if detail.get("type") != "attribute_changed":
                continue
            key = str(detail.get("path") or "")
            if not key or "old" not in detail or "new" not in detail:
                continue
            out[key] = (str(detail.get("old")), str(detail.get("new")))
        return out

    redundant_idx: Set[int] = set()

    for i, (_bucket_i, ch_i) in enumerate(cleaned_pairs):
        if ch_i.change_type != ChangeType.CHANGED:
            continue
        transitions_i = _changed_transitions(ch_i)
        if set(transitions_i.keys()) != {"name"}:
            continue

        old_name, new_name = transitions_i["name"]
        if not (old_name.startswith("/") and new_name.startswith("/")):
            continue

        file_i, line_i = meta_file_line(ch_i)
        if file_i is None or line_i is None:
            continue

        for j, (_bucket_j, ch_j) in enumerate(cleaned_pairs):
            if i == j or ch_j.change_type != ChangeType.CHANGED:
                continue
            if str(ch_i.node_type or "") != str(ch_j.node_type or ""):
                continue

            file_j, line_j = meta_file_line(ch_j)
            if file_j != file_i or line_j != line_i:
                continue

            transitions_j = _changed_transitions(ch_j)
            if transitions_j.get("path") == (old_name, new_name):
                redundant_idx.add(i)
                break

    if not redundant_idx:
        return cleaned_pairs

    return [pair for idx, pair in enumerate(cleaned_pairs) if idx not in redundant_idx]
