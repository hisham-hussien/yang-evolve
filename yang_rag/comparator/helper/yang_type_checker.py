"""YANG Type Compatibility Checker - Enhanced Version

This module provides comprehensive functionality to check if YANG type changes are backward-compatible
based on value space analysis according to RFC 7950.

Enhanced Features:
1. Resolution of complex types (typedef, identityref, leafref) to primitive types
2. Comprehensive type compatibility checking with relaxed/narrowed detection
3. Pattern and constraint analysis
4. Support for all modification scenarios (add, change, delete)
5. Union type expansion analysis
6. Range and length constraint comparisons
"""

from typing import Dict, Any, Tuple, Optional, List, Set, Union, Callable
import re
import logging
from enum import Enum

# Import FSM-based regex analyzer for accurate pattern comparison
try:
    from .regex_fsm_analyzer import compare_regex_patterns, INTEREGULAR_AVAILABLE
except ImportError:
    try:
        from regex_fsm_analyzer import compare_regex_patterns, INTEREGULAR_AVAILABLE
    except ImportError:
        INTEREGULAR_AVAILABLE = False
        def compare_regex_patterns(_old: str, _new: str, _dialect: str = 'xsd') -> str:
            return 'error'

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class CompatibilityResult(Enum):
    """Enum for compatibility check results"""
    COMPATIBLE = "backward-compatible"           # Relaxed or same
    INCOMPATIBLE = "non-backward-compatible"     # Narrowed or breaking
    CONDITIONAL = "conditional"                  # Requires context
    UNKNOWN = "unknown"                          # Requires manual review


# Type registry for typedef resolution (to be populated by the calling code)
_TYPEDEF_REGISTRY: Dict[str, Dict[str, Any]] = {}
_IDENTITY_REGISTRY: Dict[str, Set[str]] = {}  # identity -> set of derived identities


def register_typedef(name: str, base_type: str, restrictions: Optional[Dict[str, Any]] = None):
    """Register a typedef for resolution"""
    _TYPEDEF_REGISTRY[name] = {
        "base": base_type,
        "restrictions": restrictions or {}
    }


def register_identity(base: str, derived: Optional[List[str]] = None):
    """Register an identity and its derived identities"""
    if base not in _IDENTITY_REGISTRY:
        _IDENTITY_REGISTRY[base] = set()
    if derived:
        _IDENTITY_REGISTRY[base].update(derived)


def clear_registries():
    """Clear all type registries"""
    _TYPEDEF_REGISTRY.clear()
    _IDENTITY_REGISTRY.clear()


def identity_exists(identity_ref: str) -> bool:
    """
    Check if an identity reference exists in the identity registry.
    
    Args:
        identity_ref: Identity reference (may include module prefix like 'oc-aaa-types:TACACS')
    
    Returns:
        True if identity exists, False otherwise
    """
    # Strip module prefix if present (e.g., "oc-aaa-types:TACACS" -> "TACACS")
    if ':' in identity_ref:
        identity_name = identity_ref.split(':', 1)[1]
    else:
        identity_name = identity_ref
    
    # Check if it's a base identity or derived identity
    if identity_name in _IDENTITY_REGISTRY:
        return True
    
    # Check if it's a derived identity of any base
    for base_identity, derived_set in _IDENTITY_REGISTRY.items():
        if identity_name in derived_set:
            return True
    
    return False


def typedef_exists(typedef_ref: str) -> bool:
    """
    Check if a typedef exists in the typedef registry.
    
    Args:
        typedef_ref: Typedef reference (may include module prefix like 'oc-types:percentage')
    
    Returns:
        True if typedef exists, False otherwise
    """
    # Strip module prefix if present
    if ':' in typedef_ref:
        typedef_name = typedef_ref.split(':', 1)[1]
    else:
        typedef_name = typedef_ref
    
    return typedef_name in _TYPEDEF_REGISTRY


# Define built-in scalar types and their value ranges where applicable.
YANG_BUILTINS: Dict[str, Dict[str, Any]] = {
    "int8":  {"kind": "integer", "min": -(2**7), "max": 2**7 - 1},
    "int16": {"kind": "integer", "min": -(2**15), "max": 2**15 - 1},
    "int32": {"kind": "integer", "min": -(2**31), "max": 2**31 - 1},
    "int64": {"kind": "integer", "min": -(2**63), "max": 2**63 - 1},
    "uint8":  {"kind": "integer", "min": 0, "max": 2**8 - 1},
    "uint16": {"kind": "integer", "min": 0, "max": 2**16 - 1},
    "uint32": {"kind": "integer", "min": 0, "max": 2**32 - 1},
    "uint64": {"kind": "integer", "min": 0, "max": 2**64 - 1},
    "decimal64": {"kind": "decimal64", "fraction-digits": (1, 18)},
    "string": {"kind": "string"},
    "binary": {"kind": "binary"},
    "boolean": {"kind": "boolean", "values": {True, False}},
    "enumeration": {"kind": "enumeration"},
    "bits": {"kind": "bits"},
    "identityref": {"kind": "identityref"},
    "instance-identifier": {"kind": "instance-identifier"},
    "leafref": {"kind": "leafref"},
    "empty": {"kind": "empty"},
    "union": {"kind": "union"},
}


def numeric_descriptor(name: str, min_val: Optional[int] = None, max_val: Optional[int] = None) -> Dict[str, Any]:
    """Return a descriptor dict for a named numeric builtin (int*/uint*)."""
    info = YANG_BUILTINS.get(name)
    if not info or info["kind"] != "integer":
        raise ValueError(f"No numeric builtin named {name}")
    
    # Use provided constraints if available, otherwise use default range
    return {
        "base": name,
        "kind": "integer",
        "min": min_val if min_val is not None else info["min"],
        "max": max_val if max_val is not None else info["max"]
    }


def enum_descriptor(values: List[str]) -> Dict[str, Any]:
    """Create a descriptor for enumeration type with given values."""
    return {"base": "enumeration", "kind": "enumeration", "values": set(values)}


def bits_descriptor(bits_map: Dict[str, int]) -> Dict[str, Any]:
    """Create a descriptor for bits type with given bit names and positions."""
    return {"base": "bits", "kind": "bits", "bits": dict(bits_map)}


def decimal64_descriptor(fraction_digits: int, min_val: Optional[float] = None, max_val: Optional[float] = None) -> Dict[str, Any]:
    """Create a descriptor for decimal64 type."""
    if not (1 <= fraction_digits <= 18):
        raise ValueError("fraction-digits must be 1..18 per RFC 7950")
    return {
        "base": "decimal64",
        "kind": "decimal64",
        "fraction-digits": fraction_digits,
        "min": min_val,
        "max": max_val
    }


def string_descriptor(min_len: Optional[int] = None, max_len: Optional[int] = None, 
                     patterns: Optional[List[str]] = None) -> Dict[str, Any]:
    """Create a descriptor for string type with optional constraints."""
    return {
        "base": "string",
        "kind": "string",
        "min_len": min_len,
        "max_len": max_len,
        "patterns": patterns or []
    }


def union_descriptor(members: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Create a descriptor for union type with given member types."""
    return {"base": "union", "kind": "union", "members": members}


def boolean_descriptor() -> Dict[str, Any]:
    """Create a descriptor for boolean type."""
    return {"base": "boolean", "kind": "boolean", "values": {True, False}}


def empty_descriptor() -> Dict[str, Any]:
    """Create a descriptor for empty type."""
    return {"base": "empty", "kind": "empty"}


def binary_descriptor(min_len: Optional[int] = None, max_len: Optional[int] = None) -> Dict[str, Any]:
    """Create a descriptor for binary type."""
    return {"base": "binary", "kind": "binary", "min_len": min_len, "max_len": max_len}


def leafref_descriptor(path: str, target_type: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create a descriptor for leafref type."""
    desc = {"base": "leafref", "kind": "leafref", "path": path}
    if target_type:
        desc["target_type"] = target_type
    return desc


def identityref_descriptor(base: str, derived_identities: Optional[Set[str]] = None) -> Dict[str, Any]:
    """Create a descriptor for identityref type."""
    desc = {"base": "identityref", "kind": "identityref", "identity_base": base}
    if derived_identities:
        desc["derived_identities"] = derived_identities
    return desc


def instance_identifier_descriptor(require_instance: bool = True) -> Dict[str, Any]:
    """Create a descriptor for instance-identifier type."""
    return {
        "base": "instance-identifier",
        "kind": "instance-identifier",
        "require_instance": require_instance
    }


def resolve_typedef(type_name: str, max_depth: int = 10) -> Dict[str, Any]:
    """
    Resolve a typedef to its base primitive type with accumulated restrictions.
    
    Args:
        type_name: The typedef name to resolve (may include prefix like "oc-mpls:bandwidth-kbps")
        max_depth: Maximum resolution depth to prevent infinite loops
        
    Returns:
        Type descriptor with resolved base type and accumulated restrictions
    """
    if max_depth <= 0:
        logger.warning(f"Maximum typedef resolution depth reached for {type_name}")
        return {"base": type_name, "kind": "typedef", "resolved": False}
    
    # Strip prefix if present (e.g., "oc-mpls:bandwidth-kbps" -> "bandwidth-kbps")
    # Typedefs are registered without prefix, but may be referenced with prefix
    unprefixed_name = type_name.split(':')[-1] if ':' in type_name else type_name
    
    # Check if it's a built-in type
    if unprefixed_name in YANG_BUILTINS:
        info = YANG_BUILTINS[unprefixed_name]
        if info["kind"] == "integer":
            return numeric_descriptor(unprefixed_name)
        elif unprefixed_name == "string":
            return string_descriptor()
        elif unprefixed_name == "boolean":
            return boolean_descriptor()
        elif unprefixed_name == "empty":
            return empty_descriptor()
        elif unprefixed_name == "binary":
            return binary_descriptor()
        else:
            return {"base": unprefixed_name, "kind": info["kind"]}
    
    # Check in typedef registry (try both prefixed and unprefixed)
    if unprefixed_name in _TYPEDEF_REGISTRY:
        typedef_info = _TYPEDEF_REGISTRY[unprefixed_name]
        base_type = typedef_info["base"]
        restrictions = typedef_info.get("restrictions", {})
        
        # Recursively resolve base type
        base_descriptor = resolve_typedef(base_type, max_depth - 1)
        
        # Merge restrictions
        merged = base_descriptor.copy()
        for key, value in restrictions.items():
            if value is None:
                continue  # Skip None values
            if key in ["min", "max", "min_len", "max_len"]:
                # For numeric/length constraints, take the more restrictive value
                if key.startswith("min"):
                    existing = merged.get(key, float('-inf'))
                    # Only compare if existing is not None
                    if existing is not None:
                        merged[key] = max(existing, value)
                    else:
                        merged[key] = value
                else:  # max
                    existing = merged.get(key, float('inf'))
                    # Only compare if existing is not None
                    if existing is not None:
                        merged[key] = min(existing, value)
                    else:
                        merged[key] = value
            elif key == "patterns":
                # Accumulate patterns
                merged.setdefault("patterns", []).extend(value)
            elif key == "values":
                # For enums, take intersection if both exist
                if key in merged:
                    merged[key] = merged[key].intersection(set(value))
                else:
                    merged[key] = set(value)
            else:
                merged[key] = value
        
        merged["original_typedef"] = type_name
        return merged
    
    # Unknown typedef - return as-is with flag
    return {"base": type_name, "kind": "typedef", "qualified": ":" in type_name, "resolved": False}


def resolve_identityref(base_identity: str) -> Set[str]:
    """
    Resolve an identityref to get all possible identity values (base + derived).
    
    Args:
        base_identity: The base identity name
        
    Returns:
        Set of all possible identity values
    """
    result = {base_identity}
    
    # Add all derived identities recursively
    def add_derived(identity: str):
        if identity in _IDENTITY_REGISTRY:
            for derived in _IDENTITY_REGISTRY[identity]:
                if derived not in result:
                    result.add(derived)
                    add_derived(derived)
    
    add_derived(base_identity)
    return result


def parse_range_constraint(range_str: str) -> List[Tuple[Union[int, float], Union[int, float]]]:
    """
    Parse a YANG range constraint string into list of (min, max) tuples.
    Example: "1..10 | 20..30" -> [(1, 10), (20, 30)]
    """
    ranges = []
    for part in range_str.split('|'):
        part = part.strip()
        if '..' in part:
            min_val, max_val = part.split('..')
            ranges.append((parse_numeric_value(min_val.strip()), parse_numeric_value(max_val.strip())))
        else:
            val = parse_numeric_value(part)
            ranges.append((val, val))
    return ranges


def parse_numeric_value(val_str: str) -> Union[int, float]:
    """Parse a numeric value string, handling 'min' and 'max' keywords."""
    val_str = val_str.strip()
    if val_str == 'min':
        return float('-inf')
    elif val_str == 'max':
        return float('inf')
    else:
        try:
            if '.' in val_str:
                return float(val_str)
            else:
                return int(val_str)
        except ValueError:
            logger.warning(f"Could not parse numeric value: {val_str}")
            return 0


def is_range_superset(old_ranges: List[Tuple[Union[int, float], Union[int, float]]], 
                      new_ranges: List[Tuple[Union[int, float], Union[int, float]]]) -> bool:
    """
    Check if new_ranges is a superset of old_ranges (i.e., relaxed constraint).
    """
    # For each point in old_ranges, check if it's covered by new_ranges
    for old_min, old_max in old_ranges:
        covered = False
        for new_min, new_max in new_ranges:
            if new_min <= old_min and new_max >= old_max:
                covered = True
                break
        if not covered:
            return False
    return True


def compare_patterns(old_patterns: List[str], new_patterns: List[str]) -> Tuple[Optional[bool], str]:
    """
    Compare pattern constraints using FSM-based analysis.
    Returns (is_relaxed, explanation)
    - True: new patterns are more relaxed (superset of matches)
    - False: new patterns are more restrictive
    - None: Cannot determine automatically
    
    YANG semantics: Multiple patterns are AND'ed (all must match).
    Therefore: Adding a pattern is narrowing, removing a pattern is relaxing.
    """
    if not old_patterns and not new_patterns:
        return True, "no patterns in either version"
    
    if not old_patterns and new_patterns:
        return False, "new version adds pattern restrictions"
    
    if old_patterns and not new_patterns:
        return True, "new version removes all pattern restrictions"
    
    # Both have patterns - use FSM-based analysis if available
    if len(old_patterns) == 1 and len(new_patterns) == 1:
        # Single pattern comparison - use FSM analyzer for precise result
        old_pat = old_patterns[0]
        new_pat = new_patterns[0]
        
        if old_pat == new_pat:
            return True, "patterns unchanged"
        
        if INTEREGULAR_AVAILABLE:
            result = compare_regex_patterns(old_pat, new_pat)
            
            if result == 'relaxed':
                return True, f"pattern relaxed (FSM-verified): old accepts ⊂ new accepts"
            elif result == 'narrowed':
                return False, f"pattern narrowed (FSM-verified): new accepts ⊂ old accepts"
            elif result == 'equivalent':
                return True, f"patterns equivalent (FSM-verified)"
            elif result == 'incomparable':
                return False, f"patterns incomparable (non-backward-compatible): overlapping but neither subset"
            else:  # error
                logger.warning(f"FSM pattern analysis failed for: '{old_pat}' -> '{new_pat}'")
                return None, f"pattern analysis failed - requires manual review"
        else:
            # Fallback: heuristic comparison
            logger.info("FSM analyzer not available, using heuristic pattern comparison")
            return None, f"pattern changed: '{old_pat}' -> '{new_pat}' (install 'interegular' for precise analysis)"
    
    # Multiple patterns: conservative analysis
    # In YANG, multiple patterns are AND'ed, so:
    # - Removing patterns = relaxing (fewer constraints)
    # - Adding patterns = narrowing (more constraints)
    old_set = set(old_patterns)
    new_set = set(new_patterns)
    
    if old_set == new_set:
        return True, "patterns unchanged"
    
    if new_set.issubset(old_set):
        removed = old_set - new_set
        return True, f"patterns relaxed: removed {len(removed)} constraint(s) - {removed}"
    
    if old_set.issubset(new_set):
        added = new_set - old_set
        return False, f"patterns narrowed: added {len(added)} constraint(s) - {added}"
    
    # Both added and removed patterns - complex change
    added = new_set - old_set
    removed = old_set - new_set
    return None, f"pattern set changed: +{len(added)} -{len(removed)} (requires manual review)"


def is_superset_value_space(old: Dict[str, Any], new: Dict[str, Any],
                            strict_mode: bool = False) -> Tuple[Optional[bool], str]:
    """
    Return (is_superset_or_compatible, explanation).
    - True  => new is compatible (value-space superset or allowed change)
    - False => incompatible change (narrowed/breaking)
    - None  => unknown / requires semantic check

    Args:
        old: Type descriptor for the old type
        new: Type descriptor for the new type
        strict_mode: When True (--rfc7950 / strict flag), union *expansion*
                     (adding a new member type) is treated as non-backward-compatible.
                     RFC 7950 §7.4: existing receivers may not understand the new
                     member type value, so adding members is breaking in strict mode.
    """
    logger.debug(f"Comparing types: old={old}, new={new}")
    
    # Resolve typedefs if present
    if old.get("kind") == "typedef" and not old.get("resolved", True):
        old = resolve_typedef(old.get("base", "unknown"))
    if new.get("kind") == "typedef" and not new.get("resolved", True):
        new = resolve_typedef(new.get("base", "unknown"))
    
    old_kind = old.get("kind")
    new_kind = new.get("kind")
    
    # Handle integer types
    if old_kind == "integer" and new_kind == "integer":
        old_min, old_max = old.get("min", float('-inf')), old.get("max", float('inf'))
        new_min, new_max = new.get("min", float('-inf')), new.get("max", float('inf'))
        ok = (new_min <= old_min) and (new_max >= old_max)
        explanation = f"integer range: old[{old_min}, {old_max}], new[{new_min}, {new_max}]"
        return ok, explanation
    
    # Handle decimal64 types
    if old_kind == "decimal64" and new_kind == "decimal64":
        # RFC: any change to fraction-digits is non-backward-compatible
        if old.get("fraction-digits") != new.get("fraction-digits"):
            return False, f"fraction-digits changed {old.get('fraction-digits')} -> {new.get('fraction-digits')} (RFC: incompatible)"
        
        # Compare numeric bounds
        old_min, old_max = old.get("min"), old.get("max")
        new_min, new_max = new.get("min"), new.get("max")
        
        if old_min is None and old_max is None:
            return True, "fraction-digits equal, no bounds specified"
        
        min_ok = new_min is None or (old_min is not None and new_min <= old_min)
        max_ok = new_max is None or (old_max is not None and new_max >= old_max)
        ok = min_ok and max_ok
        return ok, f"decimal64 bounds: old[{old_min},{old_max}], new[{new_min},{new_max}]"
    
    # Handle string types
    if old_kind == "string" and new_kind == "string":
        old_min, old_max = old.get("min_len"), old.get("max_len")
        new_min, new_max = new.get("min_len"), new.get("max_len")
        
        # Check length constraints
        if old_min is None and old_max is None:
            if new_min is not None or new_max is not None:
                return False, "old unbounded string; new adds length restrictions -> incompatible"
            length_ok = True
        else:
            min_ok = new_min is None or (old_min is None) or (new_min <= old_min)
            max_ok = new_max is None or (old_max is None) or (new_max >= old_max)
            length_ok = min_ok and max_ok
        
        # Check patterns
        pattern_ok, pattern_explanation = compare_patterns(old.get("patterns", []), new.get("patterns", []))
        
        if pattern_ok is None:
            return None, pattern_explanation
        
        ok = length_ok and pattern_ok
        explanation = f"string length old[{old_min},{old_max}], new[{new_min},{new_max}]; {pattern_explanation}"
        return ok, explanation
    
    # Handle binary types
    if old_kind == "binary" and new_kind == "binary":
        old_min, old_max = old.get("min_len"), old.get("max_len")
        new_min, new_max = new.get("min_len"), new.get("max_len")
        
        if old_min is None and old_max is None:
            if new_min is not None or new_max is not None:
                return False, "old unbounded binary; new adds length restrictions -> incompatible"
        
        min_ok = new_min is None or (old_min is None) or (new_min <= old_min)
        max_ok = new_max is None or (old_max is None) or (new_max >= old_max)
        ok = min_ok and max_ok
        return ok, f"binary length: old[{old_min},{old_max}], new[{new_min},{new_max}]"
    
    # Handle enumeration types
    if old_kind == "enumeration" and new_kind == "enumeration":
        old_vals = old.get("values", set())
        new_vals = new.get("values", set())
        ok = old_vals.issubset(new_vals)
        added = new_vals - old_vals
        removed = old_vals - new_vals
        explanation = f"enum: old={sorted(old_vals)}, new={sorted(new_vals)}"
        if added:
            explanation += f" (added: {sorted(added)})"
        if removed:
            explanation += f" (removed: {sorted(removed)})"
        return ok, explanation
    
    # Handle bits types
    if old_kind == "bits" and new_kind == "bits":
        old_bits = set(old.get("bits", {}).keys())
        new_bits = set(new.get("bits", {}).keys())
        
        # Check if all old bits are present in new
        ok = old_bits.issubset(new_bits)
        
        # Also check that existing bit positions haven't changed
        if ok:
            old_bits_dict = old.get("bits", {})
            new_bits_dict = new.get("bits", {})
            for bit_name in old_bits:
                if old_bits_dict[bit_name] != new_bits_dict.get(bit_name):
                    return False, f"bit position changed for '{bit_name}': {old_bits_dict[bit_name]} -> {new_bits_dict.get(bit_name)}"
        
        added = new_bits - old_bits
        removed = old_bits - new_bits
        explanation = f"bits: old={sorted(old_bits)}, new={sorted(new_bits)}"
        if added:
            explanation += f" (added: {sorted(added)})"
        if removed:
            explanation += f" (removed: {sorted(removed)})"
        return ok, explanation
    
    # Handle union types
    if old_kind == "union" and new_kind == "union":
        old_members = old.get("members", [])
        new_members = new.get("members", [])
        
        # Check if every old member type is covered by at least one new member type
        for old_member in old_members:
            found = False
            for new_member in new_members:
                res, _ = is_superset_value_space(old_member, new_member, strict_mode=strict_mode)
                if res is True:
                    found = True
                    break
            if not found:
                return False, "union contracted: at least one old member type not covered by new union"

        # Under strict/rfc7950 mode, adding new member types is NBC.
        # Existing receivers may not understand a value encoded as the new type,
        # so expansion is treated as breaking (RFC 7950 §7.4).
        if strict_mode and len(new_members) > len(old_members):
            added_count = len(new_members) - len(old_members)
            return False, (
                f"union expanded (strict/rfc7950 mode): {added_count} member type(s) added — "
                f"existing receivers may not understand new type values (RFC 7950 §7.4)"
            )
        
        return True, f"union expanded or preserved: old has {len(old_members)} members, new has {len(new_members)} members"
    
    # Handle boolean types
    if old_kind == "boolean" and new_kind == "boolean":
        return True, "boolean has fixed value space {true,false}"
    
    # Handle empty types
    if old_kind == "empty" and new_kind == "empty":
        return True, "empty type unchanged (fixed value space)"
    
    # Handle empty → string/other type changes (generally incompatible)
    if old_kind == "empty":
        if new_kind in ["string", "boolean"]:
            return False, f"empty -> {new_kind}: incompatible (empty has special semantics: presence/absence of leaf, not a value)"
        else:
            return False, f"empty -> {new_kind}: incompatible type change (empty is presence indicator)"
    
    # Handle other type → empty changes (incompatible)
    if new_kind == "empty":
        return False, f"{old_kind} -> empty: incompatible (empty is presence indicator, not a value container)"
    
    # Handle leafref types
    if old_kind == "leafref" and new_kind == "leafref":
        old_path = old.get("path", "")
        new_path = new.get("path", "")
        
        if old_path == new_path:
            return True, "leafref path unchanged"
        
        # If paths differ, check target types if available
        old_target = old.get("target_type")
        new_target = new.get("target_type")
        
        if old_target and new_target:
            return is_superset_value_space(old_target, new_target)
        
        return None, f"leafref path changed: {old_path} -> {new_path} (requires semantic analysis)"
    
    # Handle leafref → string relaxation (constraint removal is backward-compatible)
    if old_kind == "leafref" and new_kind == "string":
        # Leafref enforces referential integrity (values must exist at the referenced path)
        # String accepts any string value without validation
        # This is a constraint relaxation: all valid leafref values (which are strings) remain valid
        # Therefore: backward-compatible (non-breaking)
        return True, "leafref -> string: constraint relaxation (referential integrity removed, all old values remain valid)"
    
    # Handle identityref → string relaxation (constraint removal is backward-compatible)
    if old_kind == "identityref" and new_kind == "string":
        # Identityref restricts values to defined identity names
        # String accepts any string value
        # This is a constraint relaxation: all valid identityref values remain valid as strings
        return True, "identityref -> string: constraint relaxation (identity restriction removed, all old values remain valid)"
    
    # Handle enumeration → string relaxation (constraint removal is backward-compatible)
    if old_kind == "enumeration" and new_kind == "string":
        # Enumeration restricts values to defined enum values
        # String accepts any string value
        # This is a constraint relaxation: all valid enum values remain valid as strings
        return True, "enumeration -> string: constraint relaxation (enum restriction removed, all old values remain valid)"
    
    # Handle identityref types
    if old_kind == "identityref" and new_kind == "identityref":
        old_base = old.get("identity_base", "")
        new_base = new.get("identity_base", "")
        
        if old_base == new_base:
            return True, "identityref base unchanged"
        
        # Check if new base is an ancestor of old base (relaxed)
        old_identities = resolve_identityref(old_base)
        new_identities = resolve_identityref(new_base)
        
        if old_identities.issubset(new_identities):
            return True, f"identityref relaxed: old base '{old_base}' identities are subset of new base '{new_base}'"
        elif new_identities.issubset(old_identities):
            return False, f"identityref narrowed: new base '{new_base}' is more restrictive than old base '{old_base}'"
        else:
            return None, f"identityref base changed: {old_base} -> {new_base} (incompatible hierarchies - requires review)"
    
    # Handle instance-identifier types
    if old_kind == "instance-identifier" and new_kind == "instance-identifier":
        old_require = old.get("require_instance", True)
        new_require = new.get("require_instance", True)
        
        # Changing from require-instance=true to false is relaxing
        # Changing from false to true is tightening
        if old_require and not new_require:
            return True, "instance-identifier relaxed: require-instance changed from true to false"
        elif not old_require and new_require:
            return False, "instance-identifier tightened: require-instance changed from false to true"
        else:
            return True, "instance-identifier unchanged"
    
    # Cross-type changes
    # Changing from a more specific type to union containing that type is relaxing
    if new_kind == "union":
        new_members = new.get("members", [])
        for member in new_members:
            res, _ = is_superset_value_space(old, member)
            if res is True:
                return True, f"type changed to union containing compatible member: old={old_kind}, new=union"
        return False, f"type changed to union but no compatible member found: old={old_kind}"
    
    # Fallback: if same base and kind, assume compatible
    if old.get("base") == new.get("base") and old_kind == new_kind:
        return True, "identical type"
    
    # Special case: Both are unresolved typedefs
    # If both sides are typedef kind (even if unresolved), check if they have the same base
    # This handles cases where typedefs from standard modules aren't loaded but refer to the same type
    if old_kind == "typedef" and new_kind == "typedef":
        old_base = old.get("base", "")
        new_base = new.get("base", "")
        
        # If exact same typedef name, they're compatible
        if old_base == new_base:
            return True, f"same typedef: {old_base}"
        
        # Try to resolve both and compare their base types
        old_resolved = resolve_typedef(old_base) if old_base else old
        new_resolved = resolve_typedef(new_base) if new_base else new
        
        # After resolution, if both have the same primitive base type, they're compatible
        old_resolved_kind = old_resolved.get("kind")
        new_resolved_kind = new_resolved.get("kind")
        old_resolved_base = old_resolved.get("base")
        new_resolved_base = new_resolved.get("base")
        
        # If resolved to same primitive type, compatible
        if old_resolved_kind == new_resolved_kind and old_resolved_kind != "typedef":
            # Both resolved to a primitive type (not still typedef)
            # Recursively check compatibility of the resolved types
            resolved_result, resolved_explanation = is_superset_value_space(old_resolved, new_resolved)
            if resolved_result is True:
                return True, f"typedef change: both resolve to compatible {old_resolved_kind} types"
            elif resolved_result is False:
                return False, f"typedef change: resolved types are incompatible - {resolved_explanation}"
            else:
                return None, f"typedef change: {resolved_explanation}"
        
        # If one or both still unresolved, assume compatible if both are typedefs
        # (conservative: allow typedef-to-typedef changes when we can't fully resolve)
        return None, f"typedef change: {old_base} -> {new_base} (unable to fully resolve - requires manual review)"
    
    # Different types entirely - generally incompatible
    return False, f"incompatible type change: {old_kind or old.get('base')} -> {new_kind or new.get('base')}"


def check_type_compatibility(old_type: Union[str, Dict[str, Any]], 
                            new_type: Union[str, Dict[str, Any]],
                            strict_mode: bool = False) -> Tuple[CompatibilityResult, str]:
    """
    Check if a type change from old_type to new_type is backward-compatible.
    
    Args:
        old_type: The original type (string name or descriptor dict)
        new_type: The new type (string name or descriptor dict)
        strict_mode: When True (--rfc7950 / strict flag), applies RFC 7950 strict
                     backward-compatibility rules — in particular, union *expansion*
                     (adding new member types) is treated as non-backward-compatible.
        
    Returns:
        Tuple of (CompatibilityResult, explanation)
    """
    # Helper function to strip module prefix from type name
    def strip_prefix(type_name: str) -> str:
        """Strip module prefix from type name (e.g., 'oc-sr:sr-sid-type' -> 'sr-sid-type')"""
        return type_name.split(':')[-1] if ':' in type_name else type_name
    
    # Early check: If both are strings and resolve to same unprefixed name, they're identical
    # This handles cases like "oc-sr:sr-sid-type" vs "oc-srt:sr-sid-type" where the typedef
    # is defined in the same module but referenced through different import prefixes
    if isinstance(old_type, str) and isinstance(new_type, str):
        old_unprefixed = strip_prefix(old_type)
        new_unprefixed = strip_prefix(new_type)
        if old_unprefixed == new_unprefixed:
            # Same typedef name (ignoring module prefix) - this is not a type change
            return CompatibilityResult.COMPATIBLE, f"identical type (prefix difference only): {old_unprefixed}"
    
    # Helper function to normalize type descriptors
    def normalize_type(typ):
        if isinstance(typ, str):
            return resolve_typedef(typ)
        elif isinstance(typ, dict):
            # If it's a simple dict with 'name' key, convert it to proper descriptor
            if 'name' in typ and 'kind' not in typ:
                type_name = typ['name']
                # Start with resolving the type name
                desc = resolve_typedef(type_name)
                # Overlay any constraints from the dict
                for key, value in typ.items():
                    if key != 'name':
                        if key == 'range' and isinstance(value, list) and value:
                            # Convert range list to min/max
                            all_mins = [r[0] for r in value if isinstance(r, tuple) and r[0] not in ('min', 'max')]
                            all_maxs = [r[1] for r in value if isinstance(r, tuple) and r[1] not in ('min', 'max')]
                            if all_mins:
                                desc['min'] = min(all_mins)
                            if all_maxs:
                                desc['max'] = max(all_maxs)
                        elif key in ('min', 'max', 'min_len', 'max_len', 'fraction-digits'):
                            desc[key] = value
                        elif key == 'pattern':
                            desc.setdefault('patterns', []).extend(value if isinstance(value, list) else [value])
                        elif key == 'enum':
                            desc['values'] = set(value) if isinstance(value, list) else {value}
                return desc
            else:
                return typ
        else:
            return typ
    
    # Convert types to normalized descriptors
    old_desc = normalize_type(old_type)
    new_desc = normalize_type(new_type)
    
    result, explanation = is_superset_value_space(old_desc, new_desc, strict_mode=strict_mode)
    
    if result is True:
        return CompatibilityResult.COMPATIBLE, explanation
    elif result is False:
        return CompatibilityResult.INCOMPATIBLE, explanation
    else:
        return CompatibilityResult.UNKNOWN, explanation


def check_type_addition(new_type: Union[str, Dict[str, Any]]) -> Tuple[CompatibilityResult, str]:
    """
    Check if adding a new type is backward-compatible.
    Generally, adding a type is compatible.
    """
    return CompatibilityResult.COMPATIBLE, "adding a new type is backward-compatible"


def check_type_deletion(old_type: Union[str, Dict[str, Any]]) -> Tuple[CompatibilityResult, str]:
    """
    Check if deleting a type is backward-compatible.
    Generally, deleting a type is incompatible if it's referenced elsewhere.
    """
    return CompatibilityResult.INCOMPATIBLE, "deleting a type is non-backward-compatible if it's still referenced"


# Example usage and comprehensive test suite
if __name__ == "__main__":
    print("=" * 80)
    print("YANG Type Compatibility Checker - Enhanced Version")
    print("=" * 80)
    print()
    
    # Test 1: Numeric type widening (compatible)
    print("Test 1: Numeric type widening")
    result, explanation = check_type_compatibility("int32", "int64")
    print(f"  int32 -> int64: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    # Test 2: Numeric type narrowing (incompatible)
    print("Test 2: Numeric type narrowing")
    result, explanation = check_type_compatibility("int64", "int32")
    print(f"  int64 -> int32: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    # Test 3: Enum expansion (compatible)
    print("Test 3: Enum expansion")
    old_enum = enum_descriptor(["A", "B"])
    new_enum = enum_descriptor(["A", "B", "C"])
    result, explanation = check_type_compatibility(old_enum, new_enum)
    print(f"  enum[A,B] -> enum[A,B,C]: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    # Test 4: Enum contraction (incompatible)
    print("Test 4: Enum contraction")
    old_enum = enum_descriptor(["A", "B", "C"])
    new_enum = enum_descriptor(["A", "B"])
    result, explanation = check_type_compatibility(old_enum, new_enum)
    print(f"  enum[A,B,C] -> enum[A,B]: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    # Test 5: String length relaxation (compatible)
    print("Test 5: String length relaxation")
    old_str = string_descriptor(min_len=1, max_len=63)
    new_str = string_descriptor(min_len=1, max_len=255)
    result, explanation = check_type_compatibility(old_str, new_str)
    print(f"  string[1..63] -> string[1..255]: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    # Test 6: String length restriction (incompatible)
    print("Test 6: String length restriction")
    old_str = string_descriptor(min_len=1, max_len=255)
    new_str = string_descriptor(min_len=1, max_len=63)
    result, explanation = check_type_compatibility(old_str, new_str)
    print(f"  string[1..255] -> string[1..63]: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    # Test 7: Union expansion (compatible)
    print("Test 7: Union expansion")
    old_union = union_descriptor([numeric_descriptor("int32")])
    new_union = union_descriptor([numeric_descriptor("int32"), string_descriptor()])
    result, explanation = check_type_compatibility(old_union, new_union)
    print(f"  union[int32] -> union[int32, string]: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    # Test 8: Type to union expansion (compatible)
    print("Test 8: Type to union expansion")
    old_type = numeric_descriptor("int32")
    new_union = union_descriptor([numeric_descriptor("int64"), string_descriptor()])
    result, explanation = check_type_compatibility(old_type, new_union)
    print(f"  int32 -> union[int64, string]: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    # Test 9: Decimal64 fraction-digits change (incompatible)
    print("Test 9: Decimal64 fraction-digits change")
    old_dec = decimal64_descriptor(fraction_digits=2)
    new_dec = decimal64_descriptor(fraction_digits=4)
    result, explanation = check_type_compatibility(old_dec, new_dec)
    print(f"  decimal64[fd=2] -> decimal64[fd=4]: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    # Test 10: Bits expansion (compatible)
    print("Test 10: Bits expansion")
    old_bits = bits_descriptor({"flag1": 0, "flag2": 1})
    new_bits = bits_descriptor({"flag1": 0, "flag2": 1, "flag3": 2})
    result, explanation = check_type_compatibility(old_bits, new_bits)
    print(f"  bits[flag1,flag2] -> bits[flag1,flag2,flag3]: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    # Test 11: Register and resolve typedef
    print("Test 11: Typedef resolution")
    register_typedef("port-number", "uint16")
    register_typedef("tcp-port", "port-number", {"min": 1, "max": 65535})
    
    result, explanation = check_type_compatibility("tcp-port", "uint32")
    print(f"  tcp-port -> uint32: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    # Test 12: Identityref base change
    print("Test 12: Identityref hierarchy")
    register_identity("address-family", ["ipv4", "ipv6"])
    register_identity("inet-address-family", ["inet-ipv4", "inet-ipv6"])
    
    old_idref = identityref_descriptor("ipv4")
    new_idref = identityref_descriptor("address-family")
    result, explanation = check_type_compatibility(old_idref, new_idref)
    print(f"  identityref[ipv4] -> identityref[address-family]: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    # Test 13: Leafref to string relaxation (compatible)
    print("Test 13: Leafref to string constraint relaxation")
    old_leafref = leafref_descriptor("../../name")
    new_string = string_descriptor()
    result, explanation = check_type_compatibility(old_leafref, new_string)
    print(f"  leafref -> string: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    # Test 14: Identityref to string relaxation (compatible)
    print("Test 14: Identityref to string constraint relaxation")
    old_idref = identityref_descriptor("address-family")
    new_string = string_descriptor()
    result, explanation = check_type_compatibility(old_idref, new_string)
    print(f"  identityref -> string: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    # Test 15: Enumeration to string relaxation (compatible)
    print("Test 15: Enumeration to string constraint relaxation")
    old_enum = enum_descriptor(["option1", "option2", "option3"])
    new_string = string_descriptor()
    result, explanation = check_type_compatibility(old_enum, new_string)
    print(f"  enumeration -> string: {result.value}")
    print(f"  Explanation: {explanation}")
    print()
    
    print("=" * 80)
    print("Test suite completed successfully!")
    print("=" * 80)
