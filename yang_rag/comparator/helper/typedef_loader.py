"""Load typedefs and identities from YANG files for type resolution.

This module provides utilities to extract typedef and identity definitions
from YANG files and register them with the type checker for proper resolution.
"""

import logging
from typing import Optional, Dict, Any, List, Set
from pathlib import Path

try:
    from pyang import repository, context, statements
    PYANG_AVAILABLE = True
except ImportError:
    PYANG_AVAILABLE = False

try:
    from .yang_type_checker import register_typedef, register_identity, clear_registries
except ImportError:
    from yang_type_checker import register_typedef, register_identity, clear_registries

# NOTE: NodeNormalizer is NOT used here – typedef_loader feeds yang_type_checker
# which stores enum/bit entries as plain name strings for set-based intersection
# and equality checks (dicts are unhashable).  Rich symbolic-entry parsing with
# NodeNormalizer.parse_symbolic_entry is the responsibility of the core comparison
# pipeline (typedef_handler.py → NodeNormalizer), not this loader.

logger = logging.getLogger(__name__)


def load_typedefs_from_file(yang_file: str, search_dirs: Optional[List[str]] = None) -> int:
    """Load all typedef definitions from a YANG file and register them.
    
    Args:
        yang_file: Path to the YANG file
        search_dirs: Optional list of directories to search for imported modules
        
    Returns:
        Number of typedefs loaded
    """
    if not PYANG_AVAILABLE:
        logger.warning("pyang not available, cannot load typedefs")
        return 0
    
    yang_path = Path(yang_file)
    if not yang_path.exists():
        logger.error(f"YANG file not found: {yang_file}")
        return 0
    
    # Setup pyang repository and context
    # FileRepository takes a list of directories in its constructor
    all_dirs = [str(yang_path.parent)]
    if search_dirs:
        all_dirs.extend(search_dirs)
    
    repos = repository.FileRepository(all_dirs[0], no_path_recurse=False)
    for dir_path in all_dirs[1:]:
        repos.dirs.append(dir_path)
    
    ctx = context.Context(repos)
    
    # Parse the module
    try:
        with open(yang_file, 'r', encoding='utf-8') as f:
            text = f.read()
        module = ctx.add_module(yang_file, text)
    except Exception as e:
        logger.error(f"Failed to parse module {yang_file}: {e}")
        return 0
    
    if module is None:
        logger.error(f"Failed to parse YANG module: {yang_file}")
        return 0
    
    # Validate and complete the module
    ctx.validate()
    
    count = 0
    
    # Extract typedefs from all loaded modules (main + imports)
    for mod in ctx.modules.values():
        count += _extract_typedefs_from_statement(mod, ctx)
    
    logger.info(f"Loaded {count} typedefs from {yang_file} and its imports")
    return count


def _extract_typedefs_from_statement(stmt: Any, ctx: Any) -> int:
    """Recursively extract typedefs from a statement."""
    count = 0
    
    if stmt.keyword == 'typedef':
        typedef_name = stmt.arg
        
        # Find the type statement
        type_stmt = None
        for substmt in stmt.substmts:
            if substmt.keyword == 'type':
                type_stmt = substmt
                break
        
        if type_stmt:
            base_type = type_stmt.arg
            restrictions = {}
            
            # Extract restrictions from type statement and convert to type checker format
            for type_substmt in type_stmt.substmts:
                if type_substmt.keyword == 'range':
                    # Parse range and extract min/max
                    ranges = _parse_range(type_substmt.arg)
                    if ranges:
                        # Take the overall min and max from all ranges
                        all_mins = [r[0] for r in ranges if r[0] not in ('min', 'max')]
                        all_maxs = [r[1] for r in ranges if r[1] not in ('min', 'max')]
                        if all_mins:
                            restrictions['min'] = min(all_mins)
                        if all_maxs:
                            restrictions['max'] = max(all_maxs)
                        restrictions['range'] = ranges  # Also keep original for reference
                        
                elif type_substmt.keyword == 'length':
                    # Parse length and extract min_len/max_len
                    lengths = _parse_range(type_substmt.arg)
                    if lengths:
                        all_mins = [l[0] for l in lengths if l[0] not in ('min', 'max')]
                        all_maxs = [l[1] for l in lengths if l[1] not in ('min', 'max')]
                        if all_mins:
                            restrictions['min_len'] = min(all_mins)
                        if all_maxs:
                            restrictions['max_len'] = max(all_maxs)
                        restrictions['length'] = lengths  # Also keep original
                        
                elif type_substmt.keyword == 'pattern':
                    if 'patterns' not in restrictions:
                        restrictions['patterns'] = []
                    restrictions['patterns'].append(type_substmt.arg)
                    
                elif type_substmt.keyword == 'fraction-digits':
                    restrictions['fraction-digits'] = int(type_substmt.arg)

                elif type_substmt.keyword == 'enum':
                    # Store the plain name string so that yang_type_checker and
                    # check_compatibility can put enum names into sets for intersection
                    # / equality checks (dicts are unhashable and would break set()).
                    restrictions.setdefault('values', []).append(type_substmt.arg)

                elif type_substmt.keyword == 'bit':
                    # Same rationale as enum — plain name string required by type-checker.
                    restrictions.setdefault('bits', []).append(type_substmt.arg)
            
            # Register the typedef
            register_typedef(typedef_name, base_type, restrictions if restrictions else None)
            count += 1
            logger.debug(f"Registered typedef: {typedef_name} -> {base_type} {restrictions}")
    
    # Recursively process child statements
    if hasattr(stmt, 'substmts'):
        for substmt in stmt.substmts:
            count += _extract_typedefs_from_statement(substmt, ctx)
    
    return count


def _parse_range(range_str: str) -> List[tuple]:
    """Parse YANG range/length string into list of (min, max) tuples.
    
    Examples:
        "0..7" -> [(0, 7)]
        "1..63 | 128..255" -> [(1, 63), (128, 255)]
        "min..10 | 20..max" -> [('min', 10), (20, 'max')]
    """
    ranges = []
    for part in range_str.split('|'):
        part = part.strip()
        if '..' in part:
            min_val, max_val = part.split('..')
            min_val = min_val.strip()
            max_val = max_val.strip()
            
            # Convert to int if possible, keep as string for 'min'/'max'
            if min_val not in ('min', 'max'):
                try:
                    min_val = int(min_val)
                except ValueError:
                    pass
            if max_val not in ('min', 'max'):
                try:
                    max_val = int(max_val)
                except ValueError:
                    pass
            
            ranges.append((min_val, max_val))
        else:
            # Single value (rare but valid)
            try:
                val = int(part)
                ranges.append((val, val))
            except ValueError:
                ranges.append((part, part))
    
    return ranges


def load_identities_from_file(yang_file: str, search_dirs: Optional[List[str]] = None) -> int:
    """Load all identity definitions from a YANG file and register them.
    
    Args:
        yang_file: Path to the YANG file
        search_dirs: Optional list of directories to search for imported modules
        
    Returns:
        Number of identities loaded
    """
    if not PYANG_AVAILABLE:
        logger.warning("pyang not available, cannot load identities")
        return 0
    
    yang_path = Path(yang_file)
    if not yang_path.exists():
        logger.error(f"YANG file not found: {yang_file}")
        return 0
    
    # Setup pyang repository and context
    # FileRepository takes a list of directories in its constructor
    all_dirs = [str(yang_path.parent)]
    if search_dirs:
        all_dirs.extend(search_dirs)
    
    repos = repository.FileRepository(all_dirs[0], no_path_recurse=False)
    for dir_path in all_dirs[1:]:
        repos.dirs.append(dir_path)
    
    ctx = context.Context(repos)
    
    # Parse the module
    try:
        with open(yang_file, 'r', encoding='utf-8') as f:
            text = f.read()
        module = ctx.add_module(yang_file, text)
    except Exception as e:
        logger.error(f"Failed to parse module {yang_file}: {e}")
        return 0
    
    if module is None:
        logger.error(f"Failed to parse YANG module: {yang_file}")
        return 0
    
    # Validate and complete the module
    ctx.validate()
    
    # Build identity hierarchy
    identity_bases: Dict[str, Set[str]] = {}  # base -> set of derived
    
    count = 0
    
    # Extract identities from all loaded modules (main + imports)
    for mod in ctx.modules.values():
        count += _extract_identities_from_statement(mod, identity_bases)
    
    # Register all identities
    for base, derived in identity_bases.items():
        register_identity(base, list(derived))
    
    logger.info(f"Loaded {count} identities from {yang_file} and its imports")
    return count


def _extract_identities_from_statement(stmt: Any, identity_bases: Dict[str, Set[str]]) -> int:
    """Recursively extract identities from a statement."""
    count = 0
    
    if stmt.keyword == 'identity':
        identity_name = stmt.arg
        
        # Find base statement
        base_name = None
        for substmt in stmt.substmts:
            if substmt.keyword == 'base':
                base_name = substmt.arg
                break
        
        if base_name:
            if base_name not in identity_bases:
                identity_bases[base_name] = set()
            identity_bases[base_name].add(identity_name)
            count += 1
            logger.debug(f"Registered identity: {identity_name} derives from {base_name}")
        else:
            # Base identity (no parent)
            if identity_name not in identity_bases:
                identity_bases[identity_name] = set()
            count += 1
            logger.debug(f"Registered base identity: {identity_name}")
    
    # Recursively process child statements
    if hasattr(stmt, 'substmts'):
        for substmt in stmt.substmts:
            count += _extract_identities_from_statement(substmt, identity_bases)
    
    return count


def load_types_from_directory(directory: str, pattern: str = "*.yang") -> tuple:
    """Load all typedefs and identities from YANG files in a directory.
    
    Args:
        directory: Directory containing YANG files
        pattern: File pattern to match (default: "*.yang")
        
    Returns:
        Tuple of (typedef_count, identity_count)
    """
    dir_path = Path(directory)
    if not dir_path.exists() or not dir_path.is_dir():
        logger.error(f"Directory not found: {directory}")
        return (0, 0)
    
    yang_files = list(dir_path.glob(pattern))
    if not yang_files:
        logger.warning(f"No YANG files found in {directory} matching {pattern}")
        return (0, 0)
    
    typedef_count = 0
    identity_count = 0
    
    # Get search directories (parent and subdirs for imports)
    search_dirs = [str(dir_path)]
    for subdir in dir_path.iterdir():
        if subdir.is_dir():
            search_dirs.append(str(subdir))
    
    for yang_file in yang_files:
        try:
            typedef_count += load_typedefs_from_file(str(yang_file), search_dirs)
            identity_count += load_identities_from_file(str(yang_file), search_dirs)
        except Exception as e:
            logger.error(f"Error loading types from {yang_file}: {e}")
    
    logger.info(f"Total loaded: {typedef_count} typedefs, {identity_count} identities from {len(yang_files)} files")
    return (typedef_count, identity_count)


# Example usage
if __name__ == "__main__":
    import sys
    
    logging.basicConfig(level=logging.INFO)
    
    if len(sys.argv) < 2:
        print("Usage: python typedef_loader.py <yang_file_or_directory>")
        sys.exit(1)
    
    path = sys.argv[1]
    path_obj = Path(path)
    
    if path_obj.is_file():
        print(f"Loading typedefs from file: {path}")
        typedef_count = load_typedefs_from_file(path)
        identity_count = load_identities_from_file(path)
        print(f"Loaded {typedef_count} typedefs and {identity_count} identities")
    elif path_obj.is_dir():
        print(f"Loading typedefs from directory: {path}")
        typedef_count, identity_count = load_types_from_directory(path)
        print(f"Loaded {typedef_count} typedefs and {identity_count} identities")
    else:
        print(f"Error: {path} is not a valid file or directory")
        sys.exit(1)
