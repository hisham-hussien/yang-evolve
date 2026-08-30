#!/usr/bin/env python3
"""
Repair keyword skeleton paths in extracted JSON.

Problem observed:
- Some documents have `metadata.keyword_skeleton_path` missing the terminal statement keyword
  (e.g., prefix under import recorded as "module/import" instead of "module/import/prefix").

Fix strategy:
- For each doc, ensure the skeleton path ends with the document's own YANG keyword
  (extension keywords use the base name after prefix, e.g., oc-ext:regexp-posix -> regexp-posix).
- Update:
  - metadata.keyword_skeleton_path
  - embedding_template prefix (the leading skeleton path before " | category:")
  - display_text line "Skeleton Path: ..." for consistency

Usage:
  python code/utils/repair_skeleton_paths.py --input data/yang_pyang_extracted.json --output data/yang_pyang_extracted.json

By default, updates in-place. Use a different --output to write to a new file.
"""
import argparse
import json
from pathlib import Path

ATTRIBUTE_CONSTRAINT_KEYWORDS = {
    'description','reference','units','default','base','namespace','prefix','yang-version','contact',
    'organization','revision','revision-date','argument','value','position','error-message','error-app-tag',
    'path','refine','key','must','when','pattern','range','length','unique','mandatory','min-elements',
    'max-elements','config','ordered-by','presence','if-feature','fraction-digits','require-instance',
    'modifier','yin-element','status'
}

def extract_base_keyword(yang_keyword: str) -> str:
    if not yang_keyword:
        return ''
    if isinstance(yang_keyword, str) and ':' in yang_keyword:
        return yang_keyword.split(':', 1)[1]
    return str(yang_keyword)


def repair_skeleton_path(skeleton_path: str, yang_keyword: str) -> str:
    base_kw = extract_base_keyword(yang_keyword)
    if not base_kw:
        return skeleton_path or 'module'
    parts = (skeleton_path or '').split('/') if skeleton_path else []
    # Ensure 'module' prefix exists
    if not parts or parts[0] != 'module':
        parts = ['module'] + parts
    # If already correct, return
    if parts and parts[-1] == base_kw:
        return '/'.join(parts)
    # Otherwise, append terminal keyword
    parts.append(base_kw)
    return '/'.join(parts)


def maybe_insert_parent(skeleton_path: str, parent_type: str, yang_keyword: str) -> str:
    """If parent_type is available (e.g., 'revision(2017-02-10)'), ensure the parent keyword
    appears before the terminal statement keyword in the skeleton path.
    """
    if not skeleton_path:
        return skeleton_path
    if not parent_type:
        return skeleton_path
    # Extract parent keyword before '(' if present
    parent_kw = str(parent_type).split('(')[0].strip()
    if not parent_kw:
        return skeleton_path
    base_kw = extract_base_keyword(yang_keyword)
    parts = skeleton_path.split('/')
    # Ensure module prefix
    if not parts or parts[0] != 'module':
        parts = ['module'] + parts
    # If terminal matches our statement keyword and last-1 isn't parent_kw, insert it
    if parts and parts[-1] == base_kw:
        if len(parts) < 2 or parts[-2] != parent_kw:
            parts.insert(len(parts)-1, parent_kw)
            return '/'.join(parts)
    return skeleton_path


def update_embedding_template(embedding_template: str, new_skeleton: str) -> str:
    if not embedding_template:
        return embedding_template
    # Format: "<SKELETON> | category: ..."
    try:
        after = embedding_template.split(' | ', 1)[1]
        return f"{new_skeleton} | {after}"
    except Exception:
        return embedding_template


def update_display_text(display_text: str, new_skeleton: str) -> str:
    if not display_text:
        return display_text
    lines = display_text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith('Skeleton Path: '):
            lines[i] = f"Skeleton Path: {new_skeleton}"
            break
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, default=Path('data/yang_pyang_extracted.json'))
    parser.add_argument('--output', type=Path, default=Path('data/yang_pyang_extracted.json'))
    args = parser.parse_args()

    with open(args.input, 'r', encoding='utf-8') as f:
        docs = json.load(f)

    fixed = 0
    total = 0
    for doc in docs:
        total += 1
        meta = doc.get('metadata', {})
        skeleton = meta.get('keyword_skeleton_path')
        yang_kw = meta.get('yang_keyword')
        new_skeleton = repair_skeleton_path(skeleton, yang_kw)
        # If we have parent_type, try to insert it for additional structure fidelity
        new_skeleton = maybe_insert_parent(new_skeleton, meta.get('parent_type'), yang_kw)
        if new_skeleton != skeleton:
            fixed += 1
            # Update metadata
            meta['keyword_skeleton_path'] = new_skeleton
            # Update embedding template prefix
            doc['embedding_template'] = update_embedding_template(doc.get('embedding_template',''), new_skeleton)
            # Update display text line
            doc['display_text'] = update_display_text(doc.get('display_text',''), new_skeleton)

    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(docs, f, indent=2, ensure_ascii=False)

    print(f"Repaired {fixed}/{total} skeleton paths -> {args.output}")


if __name__ == '__main__':
    main()
