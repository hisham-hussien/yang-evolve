#!/usr/bin/env python3
"""Extract backward-compatible (and conditional) changes from enriched_report.

Enhancements:
 - Dynamically loads keywords / attributes / constraints from compatibility_rules.xml
 - Parses parenthetical keyword lines e.g. "  5.1 (enum added) <backward-compatible>"
 - Associates subsequent attribute/constraint lines with the last seen parent keyword
 - Emits explicit JSON fields: keyword, attribute, constraint, action, values
 - Handles optional old values "(was ...)" and multiline continuations
"""
import re
import json
import os
import xml.etree.ElementTree as ET
from typing import Set, Dict, List, Optional

RULES_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'compatibility_rules.xml')
INPUT_FILE = os.environ.get("YANG_ENRICHED_FILE", "output/enriched_report.txt")
OUTPUT_FILE = os.environ.get("YANG_COMPAT_LIST_FILE", "output/compatible_list.txt")
LLM_VERIFICATION_FILE = os.environ.get("YANG_LLM_VERIFY_FILE", "output/llm_verification_report.json")

COMPATIBLE_TAGS = {"backward-compatible", "conditional-backward-compatible"}

# Load LLM verification results if available
def load_llm_verifications() -> Dict[str, str]:
    """Load LLM verification report and create path+constraint → verified_compatibility map."""
    def _normalize_llm_decision(raw: str) -> str:
        v = (raw or '').strip().lower()
        if v == 'confirmed':
            return 'confirmed'
        if v in {'conflict', 'conflicted', 'unconfirmed', 'llm_error'}:
            return 'conflict'
        return ''

    llm_decisions = {}
    if not os.path.exists(LLM_VERIFICATION_FILE):
        return llm_decisions
    
    try:
        with open(LLM_VERIFICATION_FILE, 'r') as f:
            data = json.load(f)
            for item in data.get('items', []):
                change = item.get('change', {})
                verification = item.get('verification', {})
                
                # Create a key from path + constraint name
                path = change.get('yang_path', '')
                constraint = change.get('keyword_or_constraint', '')
                action = change.get('action', '')
                key = f"{path}|{constraint}|{action}"
                
                # Store the LLM's verified compatibility decision
                verified_compat = _normalize_llm_decision(
                    verification.get('verified_compatibility', '')
                )
                if verified_compat:
                    llm_decisions[key] = verified_compat
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: could not load LLM verification report: {e}")
    
    return llm_decisions

LLM_DECISIONS = load_llm_verifications()

def load_rule_sets(rules_path: str) -> tuple[Set[str], Set[str], Set[str], Dict[str, List[str]], Dict[str, List[str]]]:
    kws: Set[str] = set()
    attrs: Set[str] = set()
    constraints: Set[str] = set()
    list_attr_separators: Dict[str, List[str]] = {}
    list_constraint_separators: Dict[str, List[str]] = {}
    try:
        tree = ET.parse(rules_path)
        root = tree.getroot()
        for rule in root.findall('rule'):
            # Updated to use 'structurals/structural' instead of 'keywords/keyword'
            for kw in rule.findall('structurals/structural'):
                if kw.text:
                    kws.add(kw.text.strip())
            for at in rule.findall('attributes/attribute'):
                if at.text:
                    name = at.text.strip()
                    attrs.add(name)
                    # capture list-type separators (may have multiple rules)
                    if at.attrib.get('type') == 'list':
                        sep = at.attrib.get('separator')
                        if name not in list_attr_separators:
                            list_attr_separators[name] = []
                        if sep is not None and str(sep) not in list_attr_separators[name]:
                            list_attr_separators[name].append(str(sep))
            for ct in rule.findall('constraints/constraint'):
                if ct.text:
                    name = ct.text.strip()
                    constraints.add(name)
                    if ct.attrib.get('type') == 'list':
                        sep = ct.attrib.get('separator')
                        if name not in list_constraint_separators:
                            list_constraint_separators[name] = []
                        if sep is not None and str(sep) not in list_constraint_separators[name]:
                            list_constraint_separators[name].append(str(sep))
    except (ET.ParseError, OSError) as e:  # pragma: no cover (resilient; warn only)
        print(f"Warning: could not parse rules file {rules_path}: {e}")
    # Always include 'enum' & 'attribute' tokens used in report formatting
    kws.update({"enum"})
    return kws, attrs, constraints, list_attr_separators, list_constraint_separators

KEYWORDS, ATTRIBUTES, CONSTRAINTS, LIST_ATTR_SEPARATORS, LIST_CONSTRAINT_SEPARATORS = load_rule_sets(RULES_FILE)
LLM_DECISIONS = load_llm_verifications()

# Regex primitives
LEVEL_RE = re.compile(r'^\s*(\d+(?:\.\d+)*\.?)')
# Updated PAREN_KEYWORD_RE to handle metadata like [old_line: 892, new_line: 898] and [file: ...]
# Fixed to allow whitespace before each bracket, not just the first one
PAREN_KEYWORD_RE = re.compile(r'^\s*(\d+(?:\.\d+)*\.?)\s*\(([^)]+)\)(?:(?:\s*\[[^\]]+\])+)?(?:\s*<(.*)>)?\s*$')
# Updated ATTR_LINE_RE to make XML tags optional (for multiline descriptions)
# Example: "3.2 attribute added: ['name'] -> value <backward-compatible>"
# Example: "3.2 attribute added: ['name'] -> value" (without tag, tag may come on continuation line)
ATTR_LINE_RE = re.compile(r"^\s*(\d+(?:\.\d+)*\.?)\s+(attribute|constraint)\s+(added|changed|deleted):\s+(.*?)\s*(<.*>)?\s*$", re.IGNORECASE)
# Additional regex to detect potential attribute/constraint lines even without tags (used for continuation detection)
ATTR_START_RE = re.compile(r"^\s*(\d+(?:\.\d+)*\.?)\s+(attribute|constraint)\s+(added|changed|deleted):", re.IGNORECASE)
# Example:  3.2 attribute added: ['name'] -> value <backward-compatible>
ARROW_SPLIT_RE = re.compile(r"\s*->\s*")
WAS_RE = re.compile(r'\(was\s+(.*)\)\s*$', re.IGNORECASE | re.DOTALL)
WAS_START_RE = re.compile(r'\(was\s+.*', re.IGNORECASE)

def is_top_level_path(line: str) -> bool:
    return bool(line and not line.startswith(' ') and not line[0].isdigit())


def _parse_bracket_metadata(text: str) -> dict:
    """Parse the first [...] bracket in text and return a dict of keys.

    Handles formats like:
      [file: X, line: Y]
      [file: X, old_line: Y, new_line: Z]
      [old_file: X, new_file: Y, old_line: A, new_line: B]
    Returns lower-cased keys mapped to stripped values (strings or ints where obvious).
    """
    m = re.search(r'\[([^\]]+)\]', text)
    if not m:
        return {}
    content = m.group(1)
    parts = [p.strip() for p in content.split(',') if p.strip()]
    out = {}
    for part in parts:
        if ':' not in part:
            continue
        k, v = part.split(':', 1)
        k = k.strip().lower()
        v = v.strip()
        # try to coerce numeric line values to int
        if k in ('line', 'old_line', 'new_line'):
            try:
                out[k] = int(v)
            except ValueError:
                out[k] = v
        else:
            out[k] = v
    return out

def parse_parenthetical(line: str):
    m = PAREN_KEYWORD_RE.match(line)
    if not m:
        return None
    level, inner, tag = m.groups()

    level_stripped = level.strip()
    parts = inner.strip().split()
    if len(parts) < 2:
        return None
    keyword, action = parts[0], parts[-1]

    # Accept uncovered structural-like keywords (not present in XML rules) as
    # parent keywords when they carry standard structural actions.
    if keyword not in KEYWORDS and action.lower() not in {'added', 'changed', 'deleted'}:
        return None

    # If it's a valid keyword, treat it as a parent regardless of trailing dot
    # This allows nested structural elements like "7.3 (type added)" to be recognized
    # Extract ALL tags from the multi-tag format using findall to correctly handle
    # multiple <tag> tokens like "<backward-compatible> <confirmed>".
    # The regex group captures everything between the outermost < and >, so we must
    # re-parse with findall rather than splitting the raw captured string.
    raw_tag_section = (tag or '').strip()
    # Re-extract individual tag names from the original line's tag section
    # by searching for all <word> patterns in the raw captured group.
    # The captured group for "<backward-compatible> <confirmed>" is
    # "backward-compatible> <confirmed" — so we search the original line instead.
    all_tag_words = [t.lower() for t in re.findall(r'<([^>]+)>', line)]

    tag_str = ' '.join(all_tag_words)
    llm_assistance_decision = None
    if any(t in all_tag_words for t in ('confirmed', 'conflict', 'conflicted', 'unconfirmed')):
        # Has LLM verification - will be populated later from LLM_DECISIONS map
        llm_assistance_decision = 'pending_lookup'
    elif 'needs-deep-analysis' in all_tag_words:
        # Marked for LLM but not yet verified
        llm_assistance_decision = None

    # Primary tag is the first compatibility tag (backward-compatible / non-backward-compatible / etc.)
    primary_tag = all_tag_words[0] if all_tag_words else ''
    # Return all_tag_words so callers can build a complete all_tags list
    return level_stripped, keyword, action.lower(), primary_tag, llm_assistance_decision, all_tag_words

def parse_attr_constraint(line: str):
    m = ATTR_LINE_RE.match(line)
    if not m:
        return None
    level, kind, action, tail, tags_with_brackets = m.groups()
    
    # Extract tags from format "<tag1> <tag2>" -> ["tag1", "tag2"]
    # tags_with_brackets may be None if no tags present on this line (multiline descriptions)
    import re as re_mod
    tags = []
    if tags_with_brackets:
        tags = re_mod.findall(r'<([^>]+)>', tags_with_brackets)
        tags = [t.strip().lower() for t in tags]
    
    # Extract LLM decision status from tags
    llm_assistance_decision = None
    if 'confirmed' in tags or 'conflict' in tags or 'conflicted' in tags or 'unconfirmed' in tags:
        # Has LLM verification - will be populated later from LLM_DECISIONS map
        llm_assistance_decision = 'pending_lookup'
    elif 'needs-deep-analysis' in tags:
        # Marked for LLM but not yet verified
        llm_assistance_decision = None
    
    # Primary tag is the first tag (may be empty if no tags on this line)
    primary_tag = tags[0] if tags else ''
    
    # Store all tags (including not-found, needs-deep-analysis, etc.)
    all_tags = tags if tags else []
    
    # Separate attribute token and value(s) around '->'
    # tail format: ["['name']"] -> value (was old)
    if '->' in tail:
        split_parts = ARROW_SPLIT_RE.split(tail, 1)
        if len(split_parts) == 2:
            left, right = split_parts
        else:
            # Defensive fallback: malformed spacing around arrow should never
            # crash report parsing.
            left, right = tail.split('->', 1)
    else:
        left, right = tail, ''
    left = left.strip()
    right = right.strip()
    # Extract old value if present
    old_value = None
    mwas = WAS_RE.search(right)
    if mwas:
        old_value = mwas.group(1).strip()
        right = right[:mwas.start()].rstrip().rstrip(' .;,')
    return {
        'level': level.strip(),
        'kind': kind.lower(),
        'action': action.lower(),
        'raw_attribute': left,
        'new_value': right or None,
        'old_value': old_value,
        'tag': primary_tag,
        'all_tags': all_tags,  # Preserve all tags for not-found, etc.
        'llm_assistance_decision': llm_assistance_decision
    }

def find_uses_with_when(yang_file_path: str, when_value: str, search_start_line: int = 1) -> Optional[int]:
    """
    Find a 'uses' statement that has a 'when' constraint with the specified value.
    
    This handles the case where a 'when' constraint is added via a uses statement refinement,
    rather than being defined in the helper file where the grouping is declared.
    
    Args:
        yang_file_path: Path to the YANG file
        when_value: The when constraint value to match
        search_start_line: Line number to start searching from (1-indexed)
    
    Returns:
        Line number where the uses statement with matching when is found, or None
    """
    if not yang_file_path or not os.path.exists(yang_file_path):
        return None
    
    try:
        with open(yang_file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Search for 'uses' statements followed by 'when' with matching value
        i = search_start_line - 1
        while i < len(lines):
            line = lines[i].strip()
            
            # Check if this is a 'uses' statement
            if line.startswith('uses '):
                # Check if the next few lines contain the when constraint
                for j in range(i + 1, min(i + 10, len(lines))):
                    next_line = lines[j].strip()
                    if next_line.startswith('when '):
                        # Extract the when value (remove quotes and semicolon)
                        when_match = next_line.replace('when ', '').strip(' ";')
                        if when_match == when_value:
                            return i + 1  # Return 1-indexed line number of uses statement
                        break  # Found a when, but doesn't match - move to next uses
                    elif next_line.startswith('}'):
                        break  # End of uses block
            
            i += 1
        
        return None
    except (IOError, OSError):
        return None

def find_item_in_main_file(yang_file_path: str, item_type: str, item_name: str,
                           item_value: str = None, path: str = None,
                           start_line: int = 1) -> Optional[int]:
    """
    Generic function to find where an item from a helper file is actually used in the main file.
    
    This handles various YANG constructs:
    - Constraints (when, must) added via uses refinements
    - Containers/lists from groupings
    - Type references to identities/typedefs
    - Augments
    
    Args:
        yang_file_path: Path to the YANG file to search
        item_type: Type of item ('when', 'must', 'container', 'list', 'type', etc.)
        item_name: Name of the item (constraint name, keyword name, etc.)
        item_value: Value of the item (for matching, e.g., when condition, type name)
        path: The YANG path from the report (helps narrow down search)
        start_line: 1-based line number to start searching from (default: 1).
                    Pass the parent node's new line number so that duplicate constraint
                    values resolve to the correct occurrence for each parent.
    
    Returns:
        Line number where the item appears in the main file, or None if not found
    """
    if not yang_file_path or not os.path.exists(yang_file_path):
        return None

    start_idx_global = max(0, (start_line or 1) - 1)

    try:
        with open(yang_file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Strategy 1: For 'when' constraints with a value, find the uses statement with matching when
        if item_type == 'when' and item_value:
            # Find uses statement that has this when constraint
            i = start_idx_global
            while i < len(lines):
                line = lines[i].strip()
                if line.startswith('uses '):
                    # Check next few lines for matching when
                    for j in range(i + 1, min(i + 15, len(lines))):
                        next_line = lines[j].strip()
                        if next_line.startswith('when '):
                            when_match = next_line.replace('when ', '').strip(' ";')
                            if when_match == item_value:
                                # Found the uses block, now return the exact when line
                                return j + 1  # Return the when line itself
                            break
                        elif next_line.startswith('}') or next_line.startswith('grouping '):
                            break
                i += 1
        
        # Strategy 2: For 'must' constraints with a value, find within uses block or directly
        elif item_type == 'must' and item_value:
            for i, line in enumerate(lines[start_idx_global:], start=start_idx_global):
                stripped = line.strip()
                if stripped.startswith('must '):
                    must_match = stripped.replace('must ', '').strip(' ";')
                    if must_match == item_value or item_value in must_match:
                        return i + 1
        
        # Strategy 3: For containers/lists, find the uses statement that brings them in
        elif item_type in ['container', 'list', 'leaf', 'leaf-list'] and path:
            # Extract the likely grouping/container name from path (last component)
            path_parts = path.split('/')
            container_name = path_parts[-1] if path_parts else None
            
            if container_name:
                # Search for direct declaration first (from start_line)
                for i, line in enumerate(lines[start_idx_global:], start=start_idx_global):
                    stripped = line.strip()
                    if stripped.startswith(f'{item_type} {container_name}'):
                        return i + 1
                
                # If not found, look for uses statement that might contain it
                for i, line in enumerate(lines[start_idx_global:], start=start_idx_global):
                    if 'uses ' in line and container_name.replace('-', '') in line.lower().replace('-', ''):
                        return i + 1
        
        # Strategy 4: For type references, find where the type is used
        elif item_type == 'type' and item_value:
            for i, line in enumerate(lines[start_idx_global:], start=start_idx_global):
                stripped = line.strip()
                if stripped.startswith('type ') and item_value in stripped:
                    return i + 1
        
        # Strategy 5: Generic search - find any line starting with "item_type item_value"
        # Search from start_line so duplicate constraint values resolve correctly per parent.
        if item_value:
            search_pattern = f'{item_type} {item_value}'
            for i, line in enumerate(lines[start_idx_global:], start=start_idx_global):
                if line.strip().startswith(search_pattern):
                    return i + 1
        
        return None
        
    except Exception as e:
        return None

def find_attribute_line_number(yang_file_path: str, parent_line_number: int, 
                                attribute_name: str, attribute_type: str = 'constraint') -> Optional[int]:
    """
    Find the specific line number where an attribute/constraint appears within a parent keyword definition.
    
    Args:
        yang_file_path: Path to the YANG file
        parent_line_number: Line number of the parent keyword (e.g., leaf, container)
        attribute_name: Name of the attribute (e.g., 'must', 'type', 'default')
        attribute_type: Type of attribute ('constraint' or 'attribute')
    
    Returns:
        The line number where the attribute appears, or None if not found
    """
    if not yang_file_path or not os.path.exists(yang_file_path):
        return None
    
    try:
        with open(yang_file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Start searching from the parent line number (1-indexed)
        start_idx = parent_line_number - 1
        if start_idx < 0 or start_idx >= len(lines):
            return None
        
        # Search within a reasonable range (e.g., next 100 lines)
        # Stop at closing brace that matches the opening level
        indent_level = len(lines[start_idx]) - len(lines[start_idx].lstrip())
        
        for i in range(start_idx, min(start_idx + 100, len(lines))):
            line = lines[i]
            stripped = line.strip()
            
            # Stop if we hit a closing brace at or before the parent indent level
            if stripped == '}' and (len(line) - len(line.lstrip())) <= indent_level:
                break
            
            # Look for the attribute name at the start of the line (after whitespace)
            # Match patterns like: "must ...", "type ...", "default ...", etc.
            # Also match when attribute is alone on line (e.g., "description" followed by newline)
            if stripped.startswith(f"{attribute_name} ") or stripped.startswith(f"{attribute_name}\t") or stripped == attribute_name:
                return i + 1  # Return 1-indexed line number
        
        return None
    except (IOError, OSError):
        return None

def main():
    # Re-read paths from environment at runtime (not at import time)
    input_file = os.environ.get("YANG_ENRICHED_FILE", "output/enriched_report.txt")
    output_file = os.environ.get("YANG_COMPAT_LIST_FILE", "output/compatible_list.txt")

    # Reload LLM decisions at runtime so the env var YANG_LLM_VERIFY_FILE is
    # respected even when it was set AFTER this module was imported.
    # The module-level LLM_DECISIONS is loaded at import time and may be empty
    # if the env var was not yet set (e.g., when called from cli.py after LLM
    # verification has already written the report).
    global LLM_DECISIONS
    LLM_DECISIONS = load_llm_verifications()
    
    try:
        with open(input_file, 'r', encoding='utf-8') as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        print(f"Input file not found: {input_file}")
        return

    results = []
    current_path = None
    current_parent_keyword = None  # e.g., enum after "(enum added)"
    current_parent_level = None    # e.g., '2.'
    current_parent_tag = None  # Track parent's compatibility tag for inheritance
    pending_attr = None  # hold an attribute/constraint record awaiting possible continuation lines
    current_helper_file = None  # Track helper file for typedef/identity changes (legacy single-file)
    current_helper_file_old = None
    current_helper_file_new = None
    current_line_number = None  # Track line number (when both old and new are the same)
    current_line_number_old = None  # Track old line number
    current_line_number_new = None  # Track new line number

    def finalize_pending():
        nonlocal pending_attr
        if not pending_attr:
            return
        # Merge accumulated continuation lines into value fields
        if pending_attr.get('__continuations'):
            cont_lines = pending_attr['__continuations']
            
            # Extract any XML tags from continuation lines
            import re as re_mod
            all_tags = []
            cleaned_cont_lines = []
            for cont_line in cont_lines:
                # Extract tags and remove them from the line
                found_tags = re_mod.findall(r'<([^>]+)>', cont_line)
                if found_tags:
                    all_tags.extend([t.strip().lower() for t in found_tags])
                    # Remove tags from the line
                    cleaned_line = re_mod.sub(r'<[^>]+>', '', cont_line).strip()
                    if cleaned_line:
                        cleaned_cont_lines.append(cleaned_line)
                else:
                    cleaned_cont_lines.append(cont_line.strip())
            
            # Update tag if we found one in continuations and don't have one yet
            if all_tags and not pending_attr.get('tag'):
                pending_attr['tag'] = all_tags[0]  # Use first tag found
            
            # Update all_tags field with all found tags
            if all_tags:
                # Merge with existing all_tags if present
                existing_tags = pending_attr.get('all_tags', [])
                pending_attr['all_tags'] = list(set(existing_tags + all_tags))
            
            cont_text = ' '.join(cleaned_cont_lines)
            # If the base new_value already contains '(was ' but lacks closing ')', append continuation first
            base_new = pending_attr.get('new_value') or pending_attr.get('value') or ''
            combined = (base_new + ' ' + cont_text).strip() if base_new else cont_text
            # Now attempt to split old/new even if '(was' started earlier
            old_val = None
            if '(was ' in combined:
                # Find the index where '(was' starts
                idx = combined.lower().rfind('(was')
                before = combined[:idx].rstrip()
                after = combined[idx:]
                m_close = re.match(r'\(was\s+(.*)\)\s*$', after, re.IGNORECASE | re.DOTALL)
                if m_close:
                    old_val = m_close.group(1).strip()
                    cont_main = before
                else:
                    # If not closed yet, leave combined as new_value
                    cont_main = combined
            else:
                cont_main = combined
            # Append continuation main text to new_value/value appropriately
            if pending_attr['action'] == 'added':
                base_key = 'value' if 'value' in pending_attr else 'new_value'
            else:
                base_key = 'new_value'
            if cont_main:
                pending_attr[base_key] = cont_main.strip()
            # Assign old value if found and not already set
            if old_val and 'old_value' not in pending_attr and pending_attr['action'] == 'changed':
                pending_attr['old_value'] = old_val
        # Clean internal keys
        pending_attr.pop('__continuations', None)
        results.append(pending_attr)
        pending_attr = None

    for raw in lines:
        line = raw.rstrip('\n')
        stripped = line.rstrip()
        if not stripped:
            continue

        # Continuation line heuristic: any indented line that is not a parenthetical keyword or attr/constraint line
        # Use ATTR_START_RE to detect potential attribute lines even without tags at the end
        if pending_attr and line.startswith(' '):
            if not PAREN_KEYWORD_RE.match(stripped) and not ATTR_START_RE.match(stripped):
                pending_attr.setdefault('__continuations', []).append(stripped)
                continue

        if is_top_level_path(stripped):
            finalize_pending()
            # Extract file and line numbers from path line if present
            # Format: [file: X, line: Y] or [file: X, old_line: Y, new_line: Z]
            current_line_number = None
            current_line_number_old = None
            current_line_number_new = None
            current_helper_file = None
            
            # Parse bracket metadata (supports file OR old_file/new_file and line variants)
            meta = _parse_bracket_metadata(stripped)
            if meta:
                # populate helper file fields (keep legacy single 'file' for compatibility)
                current_helper_file_new = meta.get('new_file')
                current_helper_file_old = meta.get('old_file')
                current_helper_file = meta.get('file') or current_helper_file_new or current_helper_file_old
                # set line numbers
                if 'line' in meta:
                    current_line_number = meta.get('line')
                    current_line_number_old = None
                    current_line_number_new = None
                else:
                    current_line_number = None
                    current_line_number_old = meta.get('old_line')
                    current_line_number_new = meta.get('new_line')
                # Remove metadata from path
                current_path = re.sub(r'\s*\[[^\]]*\]', '', stripped).strip()
            else:
                current_path = stripped.strip()
            current_parent_keyword = None
            continue

        # Parenthetical keyword line
        pk = parse_parenthetical(stripped)
        if pk:
            finalize_pending()
            level, keyword, action, tag, llm_assistance_decision, all_tag_words = pk
            
            # Extract metadata from parenthetical line (file and line numbers in combined format)
            # Format: [file: X, line: Y] or [file: X, old_line: Y, new_line: Z]
            paren_line_number = None
            paren_line_number_old = None
            paren_line_number_new = None
            paren_helper_file = None
            
            # Parse bracket metadata (supports file OR old_file/new_file and line variants)
            meta = _parse_bracket_metadata(stripped)
            if meta:
                paren_helper_file_new = meta.get('new_file')
                paren_helper_file_old = meta.get('old_file')
                paren_helper_file = meta.get('file') or paren_helper_file_new or paren_helper_file_old
                if 'line' in meta:
                    paren_line_number = meta.get('line')
                else:
                    paren_line_number_old = meta.get('old_line')
                    paren_line_number_new = meta.get('new_line')
            
            # If parenthetical line has metadata, update current tracking variables
            if paren_line_number is not None or paren_line_number_old is not None or paren_line_number_new is not None:
                if paren_line_number is not None:
                    current_line_number = paren_line_number
                    current_line_number_old = None
                    current_line_number_new = None
                else:
                    current_line_number = None
                    current_line_number_old = paren_line_number_old
                    current_line_number_new = paren_line_number_new
            
            # IMPORTANT: Always update current_helper_file when found, even if parent is non-compatible
            # This ensures child compatible attributes inherit the correct file
            if paren_helper_file:
                current_helper_file = paren_helper_file
                # also populate old/new helper file vars when available
                current_helper_file_old = paren_helper_file_old if 'paren_helper_file_old' in locals() else current_helper_file_old
                current_helper_file_new = paren_helper_file_new if 'paren_helper_file_new' in locals() else current_helper_file_new
            
            # Also update current_parent_keyword and current_parent_level BEFORE the tag check
            # so that child attributes know their parent context
            current_parent_keyword = keyword
            current_parent_level = level
            current_parent_tag = tag  # Track parent tag for child inheritance
            
            # Extract parent keywords with compatible tags OR unmarked (no tag)
            # Skip only non-compatible tagged parents
            if not tag or tag in COMPATIBLE_TAGS:
                record = {
                    'level': level,
                    'path': current_path or '',
                    'keyword': keyword,
                    'action': action
                }
                # Add tags for categorization.
                # Use all_tag_words (full list from parse_parenthetical) so that secondary
                # tags like <confirmed> or <conflict> are preserved in all_tags and can be
                # detected by the llm_assistance_decision lookup below.
                if tag:
                    record['tag'] = tag
                    record['all_tags'] = all_tag_words  # includes primary + secondary tags (e.g. confirmed)
                
                # Add line numbers if available
                if current_line_number is not None:
                    record['line_number'] = current_line_number
                elif current_line_number_old is not None or current_line_number_new is not None:
                    if current_line_number_old is not None:
                        record['line_number_old'] = current_line_number_old
                    if current_line_number_new is not None:
                        record['line_number_new'] = current_line_number_new
                # Add file if available
                # Attach helper file provenance: if we have both old and new file, emit both keys
                if current_helper_file_old and current_helper_file_new:
                    record['old_file'] = current_helper_file_old
                    record['new_file'] = current_helper_file_new
                elif current_helper_file:
                    record['file'] = current_helper_file
                # Look up actual LLM decision if available.
                # Priority 1: the all_tags list already contains the outcome
                #   ('confirmed' or 'conflict') — use it directly.
                #   This is the most reliable source because it comes from the
                #   verified enriched report itself.
                # Priority 2: fall back to the LLM_DECISIONS map (keyed by
                #   path|constraint|action) for cases where the tag lookup fails.
                if llm_assistance_decision == 'pending_lookup':
                    # Check all_tags first (most reliable)
                    all_tags_list = record.get('all_tags', [])
                    tag_decision = None
                    for outcome_tag in ('confirmed', 'conflict'):
                        if outcome_tag in all_tags_list:
                            tag_decision = outcome_tag
                            break
                    if tag_decision:
                        record['llm_assistance_decision'] = tag_decision
                    else:
                        # Fall back to LLM_DECISIONS map lookup
                        lookup_key = f"{current_path}|{keyword}|{action}"
                        llm_decision = LLM_DECISIONS.get(lookup_key)
                        record['llm_assistance_decision'] = llm_decision if llm_decision else 'none'
                elif llm_assistance_decision:
                    record['llm_assistance_decision'] = llm_assistance_decision
                results.append(record)
            # current_parent_keyword and current_parent_level already set above before tag check
            continue

        # Attribute / constraint line
        parsed = parse_attr_constraint(stripped)
        if not parsed:
            continue
        
        # Allow child attributes to inherit parent's tag if they don't have their own
        # But track if the item originally had no tag (unmarked)
        original_tag = parsed['tag']
        effective_tag = original_tag if original_tag else (current_parent_tag if 'current_parent_tag' in locals() else '')
        
        # Extract items with compatible tags OR unmarked items (no tag)
        # Skip only non-compatible tagged items
        if effective_tag and effective_tag not in COMPATIBLE_TAGS:
            # Has a tag but it's not compatible (likely non-backward-compatible)
            continue
        # If no tag (unmarked) or has compatible tag, continue processing

        # New attribute begins; finalize previous pending
        finalize_pending()
        record = {
            'level': parsed['level'],
            'path': current_path or '',
            'action': parsed['action']
        }
        # Copy tags for later categorization
        # IMPORTANT: Only use the original tag from the enriched report, NOT inherited tags
        # This ensures unmarked items (no tag in enriched report) remain unmarked
        if parsed.get('all_tags'):
            record['all_tags'] = parsed['all_tags']
        # Don't add inherited tags - keep item unmarked if it had no original tag
        
        if parsed.get('tag'):
            record['tag'] = parsed['tag']
        # Don't add inherited tags - keep item unmarked if it had no original tag
        
        attr_token = parsed['raw_attribute']
        m_tok = re.match(r"^\['([^']+)'\]$", attr_token)
        if m_tok:
            attr_token = m_tok.group(1)
        # Skip symbolic keyword attribute lines (enum/bit) — these are noise lines
        # that duplicate structural (enum added) reporting. They should not appear
        # in the compatibility list as separate attribute entries.
        if parsed['kind'] == 'attribute' and attr_token in {'enum', 'bit'}:
            continue
        if parsed['kind'] == 'attribute':
            record['attribute'] = attr_token
        else:
            record['constraint'] = attr_token
        if current_parent_keyword:
            record['keyword'] = current_parent_keyword
        # Add LLM assistance decision field if present.
        # Priority 1: all_tags already contains the outcome tag ('confirmed' or
        #   'conflict') — use it directly.  This is the most
        #   reliable source because it comes from the verified enriched report.
        # Priority 2: fall back to the LLM_DECISIONS map lookup.
        if parsed.get('llm_assistance_decision') == 'pending_lookup':
            # Check all_tags first (most reliable)
            all_tags_list = parsed.get('all_tags', [])
            tag_decision = None
            for outcome_tag in ('confirmed', 'conflict'):
                if outcome_tag in all_tags_list:
                    tag_decision = outcome_tag
                    break
            if tag_decision:
                record['llm_assistance_decision'] = tag_decision
            else:
                # Fall back to LLM_DECISIONS map lookup
                constraint_name = attr_token
                lookup_key = f"{current_path}|{constraint_name}|{parsed['action']}"
                llm_decision = LLM_DECISIONS.get(lookup_key)
                record['llm_assistance_decision'] = llm_decision if llm_decision else 'none'
        elif parsed.get('llm_assistance_decision'):
            record['llm_assistance_decision'] = parsed['llm_assistance_decision']
        
        # Add file(s) if we have them from the parenthetical line
        if current_helper_file_old and current_helper_file_new:
            record['old_file'] = current_helper_file_old
            record['new_file'] = current_helper_file_new
        elif current_helper_file:
            record['file'] = current_helper_file
        
        # Add line numbers if available
        # For attributes/constraints, try to find the exact line number where the attribute appears
        attribute_line_old = None
        attribute_line_new = None
        attribute_line_single = None
        
        # Determine YANG file paths
        # If the record has a file field (helper file), construct the path from OLD_DIR/NEW_DIR
        # Otherwise, use the main YANG files from environment variables
        old_yang_file = os.environ.get("YANG_OLD_FILE")
        new_yang_file = os.environ.get("YANG_NEW_FILE")
        
        if current_helper_file:
            # Use helper file paths constructed from the directories
            old_dir = os.path.dirname(old_yang_file) if old_yang_file else None
            new_dir = os.path.dirname(new_yang_file) if new_yang_file else None
            
            if old_dir:
                old_yang_file = os.path.join(old_dir, current_helper_file)
            if new_dir:
                new_yang_file = os.path.join(new_dir, current_helper_file)
        
        # Try to find exact attribute line numbers
        if parsed['action'] == 'changed' and current_line_number_old is not None and current_line_number_new is not None:
            # For changed attributes, look in both old and new files
            if old_yang_file and os.path.exists(old_yang_file):
                attribute_line_old = find_attribute_line_number(old_yang_file, current_line_number_old, attr_token, parsed['kind'])
            if new_yang_file and os.path.exists(new_yang_file):
                attribute_line_new = find_attribute_line_number(new_yang_file, current_line_number_new, attr_token, parsed['kind'])
        elif parsed['action'] == 'added' and current_line_number_new is not None:
            # For added attributes, look in new file
            if new_yang_file and os.path.exists(new_yang_file):
                attribute_line_single = find_attribute_line_number(new_yang_file, current_line_number_new, attr_token, parsed['kind'])
        elif parsed['action'] == 'deleted' and current_line_number_old is not None:
            # For deleted attributes, look in old file
            if old_yang_file and os.path.exists(old_yang_file):
                attribute_line_single = find_attribute_line_number(old_yang_file, current_line_number_old, attr_token, parsed['kind'])
        elif current_line_number is not None:
            # Single line number (try new file first, then old file)
            if new_yang_file and os.path.exists(new_yang_file):
                attribute_line_single = find_attribute_line_number(new_yang_file, current_line_number, attr_token, parsed['kind'])
            elif old_yang_file and os.path.exists(old_yang_file):
                attribute_line_single = find_attribute_line_number(old_yang_file, current_line_number, attr_token, parsed['kind'])
        
        # Use found attribute line numbers if available, otherwise fall back to parent keyword line numbers.
        # IMPORTANT: Respect the constraint/attribute action when assigning line numbers:
        #   - 'added'   → only line_number_new (constraint only exists in new file)
        #   - 'deleted' → only line_number_old (constraint only exists in old file)
        #   - 'changed' → both line_number_old and line_number_new
        # Without this, an 'added' constraint would incorrectly inherit line_number_old
        # from the parent entry (which has both old and new line numbers because the
        # parent node itself was 'changed').
        child_action = parsed.get('action', '')
        if attribute_line_single is not None:
            record['line_number'] = attribute_line_single
        elif attribute_line_old is not None or attribute_line_new is not None:
            if attribute_line_old is not None:
                record['line_number_old'] = attribute_line_old
            if attribute_line_new is not None:
                record['line_number_new'] = attribute_line_new
        elif current_line_number is not None:
            record['line_number'] = current_line_number
        elif current_line_number_old is not None or current_line_number_new is not None:
            if child_action == 'added':
                # Added constraint only exists in new file → only new line number
                if current_line_number_new is not None:
                    record['line_number'] = current_line_number_new
                elif current_line_number_old is not None:
                    record['line_number'] = current_line_number_old
            elif child_action == 'deleted':
                # Deleted constraint only exists in old file → only old line number
                if current_line_number_old is not None:
                    record['line_number'] = current_line_number_old
                elif current_line_number_new is not None:
                    record['line_number'] = current_line_number_new
            else:
                # Changed or unknown → keep both old and new line numbers
                if current_line_number_old is not None:
                    record['line_number_old'] = current_line_number_old
                if current_line_number_new is not None:
                    record['line_number_new'] = current_line_number_new
        
        # Populate initial value fields
        if parsed['action'] == 'added':
            if parsed['new_value']:
                record['value'] = parsed['new_value']
        elif parsed['action'] == 'changed':
            if parsed['new_value']:
                record['new_value'] = parsed['new_value']
            if parsed['old_value']:
                record['old_value'] = parsed['old_value']
        else:
            # For deleted, normalize to 'value' for consistency with 'added'
            if parsed['new_value']:
                record['value'] = parsed['new_value']
            if parsed['old_value']:
                record['old_value'] = parsed['old_value']
        pending_attr = record

    # End loop finalize
    finalize_pending()

    # Generic file handling: Find where items from helper files are actually used in main file
    # When file is present and action is 'added', the line number points to the helper file definition,
    # but we want the line where it's actually declared/used in the current file.
    #
    # ALSO: For ADDED constraints (when, must), even without file, the line number may point
    # to the container definition (e.g., line 104 in helper file where container is defined),
    # but we want the line where the constraint is ACTUALLY added via uses refinement (e.g., line 258)
    for rec in results:
        # If a helper file is present (file OR old_file/new_file), try to find the actual
        # declaration in the main YANG file. Prefer new file for added/changed items.
        if (rec.get('file') or rec.get('new_file') or rec.get('old_file')) and rec.get('action') == 'added':
            item_type = rec.get('constraint') or rec.get('attribute') or rec.get('keyword')
            item_value = rec.get('value', '')
            path = rec.get('path', '')

            # Determine which main file to search
            yang_file = None
            # prefer new file for added items
            yang_file = os.environ.get("YANG_NEW_FILE")

            if item_type and yang_file and os.path.exists(yang_file):
                parent_line = (
                    rec.get('line_number_new')
                    or rec.get('line_number')
                    or 1
                )
                exact_line = find_item_in_main_file(
                    yang_file,
                    item_type,
                    item_type,
                    item_value if item_value else None,
                    path if path else None,
                    start_line=parent_line,
                )

                if exact_line:
                    rec['line_number'] = exact_line
                    rec.pop('line_number_old', None)
                    rec.pop('line_number_new', None)
                    # Remove helper-file provenance since we resolved to the main file
                    rec.pop('file', None)
                    rec.pop('old_file', None)
                    rec.pop('new_file', None)
        
        # CRITICAL FIX: For constraints/attributes, always search for exact line
        # This handles cases where the parent container line number points to a definition location,
        # but the actual constraint/attribute is added elsewhere (e.g., via uses refinement)
        # 
        # This applies to ALL constraints/attributes, even if line_number is already set,
        # because the existing line_number might be inherited from parent and point to wrong location
        if rec.get('constraint') or rec.get('attribute'):
            item_type = rec.get('constraint') or rec.get('attribute')
            item_value = rec.get('value') or rec.get('new_value', '')
            path = rec.get('path', '')
            
            # Determine which file to search based on action
            yang_file = None
            if rec.get('action') == 'added':
                yang_file = os.environ.get("YANG_NEW_FILE")
            elif rec.get('action') == 'deleted':
                yang_file = os.environ.get("YANG_OLD_FILE")
            elif rec.get('action') == 'changed':
                # For changed, prefer new file
                yang_file = os.environ.get("YANG_NEW_FILE")
            
            if item_type and yang_file and os.path.exists(yang_file) and item_value:
                parent_line = (
                    rec.get('line_number_new')
                    or rec.get('line_number')
                    or 1
                )
                exact_line = find_item_in_main_file(
                    yang_file,
                    item_type,
                    item_type,
                    item_value,
                    path if path else None,
                    start_line=parent_line,
                )
                
                if exact_line:
                    # Update to use the exact line number (overwrite any existing line_number)
                    rec['line_number'] = exact_line
                    rec.pop('line_number_old', None)
                    rec.pop('line_number_new', None)

    # Post-process to catch any missed inline '(was ...)' splits residing in new_value
    WAS_INLINE_SPLIT = re.compile(r"(.*)\(was\s+(.*)\)$", re.IGNORECASE)
    for rec in results:
        if rec.get('action') == 'changed' and 'old_value' not in rec and rec.get('new_value'):
            nv = rec['new_value']
            if '(was ' in nv:
                m = WAS_INLINE_SPLIT.match(nv.strip())
                if m:
                    new_part, old_part = m.groups()
                    rec['new_value'] = new_part.strip().rstrip('. ')
                    rec['old_value'] = old_part.strip().rstrip(') ').rstrip('. ')
                else:
                    # Fallback manual split at last occurrence
                    idx = nv.lower().rfind('(was ')
                    if idx != -1 and nv.strip().endswith(')'):
                        new_part = nv[:idx].rstrip(' .')
                        old_part = nv[idx+5:].rstrip().rstrip(') ').rstrip()
                        if new_part and old_part:
                            rec['new_value'] = new_part
                            rec['old_value'] = old_part
        # Also normalize trailing spaces
        if 'new_value' in rec and isinstance(rec['new_value'], str):
            rec['new_value'] = rec['new_value'].strip()
        if 'old_value' in rec and isinstance(rec['old_value'], str):
            rec['old_value'] = rec['old_value'].strip()

    # --- NEW: explode list-attribute changes into per-item additions ---
    def _split_by_separators(text: str, seps: List[str]) -> List[str]:
        if text is None:
            return []
        s = str(text).strip()
        if not s:
            return []
        named = {
            'space': ' ',
            'whitespace': ' ',
            'comma': ',',
            'semicolon': ';',
            'hyphen': '-',
            'underscore': '_',
        }
        lits = [named.get(x.strip().lower(), x) for x in (seps or []) if x is not None]
        if not lits:
            parts = re.split(r"\s+", s)
        else:
            esc = [re.escape(x) for x in lits if x]
            pattern = r"(?:" + r"|".join(esc) + r"|\s+)"
            parts = re.split(pattern, s)
        return [p.strip() for p in parts if p.strip()]

    exploded: List[dict] = []
    parent_emitted: Set[tuple] = set()
    for rec in results:
        # only transform attribute records that are tagged compatible already
        attr_name = rec.get('attribute')
        if attr_name and attr_name in LIST_ATTR_SEPARATORS:
            seps = LIST_ATTR_SEPARATORS.get(attr_name, [])
            action = rec.get('action')
            lvl = rec.get('level', '')
            base_num = re.match(r"^(\d+)", str(lvl) or '')
            base_level = f"{base_num.group(1)}." if base_num else ''
            # Compute delta
            new_items = _split_by_separators(rec.get('new_value') or rec.get('value') or '', seps)
            old_items = _split_by_separators(rec.get('old_value') or '', seps)
            add_items = sorted(set(new_items) - set(old_items))
            # For compatible list: keep only added items
            if action in ('changed', 'added') and add_items:
                # Ensure parent header present with action 'changed'
                pkey = (rec.get('path',''), rec.get('keyword',''), base_level)
                if pkey not in parent_emitted and rec.get('keyword') and base_level:
                    exploded.append({
                        'level': base_level,
                        'path': rec.get('path',''),
                        'keyword': rec.get('keyword'),
                        'action': 'changed'
                    })
                    parent_emitted.add(pkey)
                for it in add_items:
                    exploded.append({
                        'level': lvl,
                        'path': rec.get('path',''),
                        'action': 'added',
                        'attribute': attr_name,
                        'value': it,
                        'keyword': rec.get('keyword')
                    })
                continue  # skip original aggregate record
        # transform constraint list items
        constr_name = rec.get('constraint')
        if constr_name and constr_name in LIST_CONSTRAINT_SEPARATORS:
            seps = LIST_CONSTRAINT_SEPARATORS.get(constr_name, [])
            action = rec.get('action')
            lvl = rec.get('level', '')
            base_num = re.match(r"^(\d+)", str(lvl) or '')
            base_level = f"{base_num.group(1)}." if base_num else ''
            new_items = _split_by_separators(rec.get('new_value') or rec.get('value') or '', seps)
            old_items = _split_by_separators(rec.get('old_value') or '', seps)
            add_items = sorted(set(new_items) - set(old_items))
            if action in ('changed', 'added') and add_items:
                pkey = (rec.get('path',''), rec.get('keyword',''), base_level)
                if pkey not in parent_emitted and rec.get('keyword') and base_level:
                    exploded.append({
                        'level': base_level,
                        'path': rec.get('path',''),
                        'keyword': rec.get('keyword'),
                        'action': 'changed'
                    })
                    parent_emitted.add(pkey)
                for it in add_items:
                    exploded.append({
                        'level': lvl,
                        'path': rec.get('path',''),
                        'action': 'added',
                        'constraint': constr_name,
                        'value': it,
                        'keyword': rec.get('keyword')
                    })
                continue
        exploded.append(rec)

    results = exploded

    with open(output_file, 'w', encoding='utf-8') as fh:
        json.dump(results, fh, indent=4, ensure_ascii=False)
    print(f"compatible changes extracted and saved to {output_file}")

if __name__ == '__main__':
    main()
