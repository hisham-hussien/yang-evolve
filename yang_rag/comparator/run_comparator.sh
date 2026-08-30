
#!/usr/bin/env bash
set -euo pipefail

# run_comparator.sh
# Recursively resolve YANG import/include dependencies from REPO_ROOT into per-commit export dirs
# Ensures that if a copied file is a submodule, its parent module (belongs-to) is also copied.

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Defaults (adjust if your layout differs)
WRAPPER="yang_comparator.sh"
WRAPPER_PATH=""
TOOL_OUTPUT_DIR="output"
REPORTS_ROOT="yang_comparator_reports"

# Default paths are resolved relative to the project root (one level above yang_rag/)
PROJECT_ROOT_DEFAULT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
EXPORTS_PREFIX_DEFAULT="${PROJECT_ROOT_DEFAULT}/workspace/exports"
REPO_ROOT_DEFAULT="${PROJECT_ROOT_DEFAULT}/workspace/repo"

EXPORTS_PREFIX="${EXPORTS_PREFIX_DEFAULT}"
REPO_ROOT="${REPO_ROOT_DEFAULT}"
COMMIT_TO_RUN=""
EXTRA_FLAGS=""

print_usage() {
  cat <<EOF
Usage: $0 [--commit <commit>] [--wrapper <path>] [--tool-output <path>] [--reports-dir <path>] [--exports-prefix <prefix>] [--repo-root <path>] [--<flag>]

--commit <commit>         Process a single commit (reads <prefix>/<commit>/manifest.csv).
--wrapper <path>          Path to comparator wrapper (default: ${WRAPPER}).
--tool-output <path>      Path to comparator output directory (default: ${TOOL_OUTPUT_DIR}).
--reports-dir <path>      Where to copy archived outputs (default: ${REPORTS_ROOT}).
--exports-prefix <pre>    Base prefix for exports (default: ${EXPORTS_PREFIX}).
--repo-root <path>        Where to search for dependency YANG files (default: ${REPO_ROOT}).
--<flag>                  Enable specific compatibility mode (e.g., --rfc7950, --strict, --vendor-specific).
EOF
  exit 1
}

# parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit) COMMIT_TO_RUN="$2"; shift 2;;
    --wrapper) WRAPPER_PATH="$2"; shift 2;;
    --tool-output) TOOL_OUTPUT_DIR="$2"; shift 2;;
    --reports-dir) REPORTS_ROOT="$2"; shift 2;;
    --exports-prefix) EXPORTS_PREFIX="$2"; shift 2;;
    --repo-root) REPO_ROOT="$2"; shift 2;;
    -h|--help) print_usage;;
    --*) EXTRA_FLAGS="$EXTRA_FLAGS $1"; shift;;
    *) echo "Unknown arg: $1"; print_usage;;
  esac
done

# resolve wrapper path if not provided
if [ -n "${WRAPPER_PATH:-}" ] && [ -f "$WRAPPER_PATH" ]; then
  : # use given
else
  for c in "../yang_comparator/yang_comparator.sh" "../yang_comparator/yang_comparator" "./yang_comparator/yang_comparator.sh" "./yang_comparator.sh" "$WRAPPER"; do
    if [ -f "$c" ]; then
      WRAPPER_PATH="$c"
      break
    fi
  done
fi

if [ -z "${WRAPPER_PATH:-}" ] || [ ! -f "$WRAPPER_PATH" ]; then
  echo "ERROR: comparator wrapper not found. Provide --wrapper /path/to/yang_comparator.sh or place it at ../yang_comparator/yang_comparator.sh"
  exit 2
fi

echo "Using comparator wrapper: $WRAPPER_PATH"
echo "Repo root: $REPO_ROOT"
echo "Exports prefix: $EXPORTS_PREFIX"
echo "Reports dir: $REPORTS_ROOT"
if [ -n "$EXTRA_FLAGS" ]; then
  echo "Extra flags: $EXTRA_FLAGS"
fi

# -------------------------
# Token extractor using sed (robust & portable)
gather_needed_tokens() {
  local yangfile="$1"
  [ -f "$yangfile" ] || return 0
  sed -nE "s/^[[:space:]]*(import|include)[[:space:]]+['\"]?([A-Za-z0-9_.-]+).*/\\2/p" "$yangfile" | sed 's/[[:space:]]*$//'
}

# Find exact token in repo, fallback to *token*.yang
find_dep_in_repo() {
  local token="$1"
  local found
  found=$(find "$REPO_ROOT" -type f -name "${token}.yang" -print -quit 2>/dev/null || true)
  if [ -n "$found" ]; then printf '%s' "$found"; return 0; fi
  found=$(find "$REPO_ROOT" -type f -iname "*${token}*.yang" -print -quit 2>/dev/null || true)
  if [ -n "$found" ]; then printf '%s' "$found"; return 0; fi
  return 1
}

# Copy a file into target dir only if missing.
copy_if_missing() {
  local src="$1" dstdir="$2"
  mkdir -p "$dstdir"
  local dst="$dstdir/$(basename "$src")"
  if [ ! -f "$dst" ]; then cp "$src" "$dst"; fi
}

# Detect if a file is a submodule and if so return its 'belongs-to' module name (or empty).
# Usage: detect_submodule_parent <file>
detect_submodule_parent() {
  local file="$1"
  [ -f "$file" ] || { printf ''; return 0; }
  # check if file contains a 'submodule' statement
  if grep -Eq '^[[:space:]]*submodule[[:space:]]+' "$file"; then
    # extract belongs-to line: belongs-to MODULE_NAME { ...
    # The pattern: belongs-to <module-name>
    parent=$(sed -nE "s/^[[:space:]]*belongs-to[[:space:]]+([A-Za-z0-9_.-]+).*/\\1/p" "$file" | head -n1 || true)
    printf '%s' "${parent:-}"
    return 0
  fi
  printf ''
  return 0
}

# run wrapper (executable or via bash/sh)
run_wrapper() {
  local basename="$1" old_dir="$2" new_dir="$3"
  if [ -x "$WRAPPER_PATH" ]; then
    "$WRAPPER_PATH" "$basename" "$basename" "$old_dir" "$new_dir" $EXTRA_FLAGS
    return $?
  else
    if command -v bash >/dev/null 2>&1; then
      bash "$WRAPPER_PATH" "$basename" "$basename" "$old_dir" "$new_dir" $EXTRA_FLAGS
      return $?
    else
      sh "$WRAPPER_PATH" "$basename" "$basename" "$old_dir" "$new_dir" $EXTRA_FLAGS
      return $?
    fi
  fi
}

# resolve_dependencies_recursive: keep resolving until all found or no progress
# It now also ensures that if a copied file is a submodule, the parent module is copied as well.
# Workaround: ignore token "the" (case-insensitive)
resolve_dependencies_recursive() {
  local old_dir="$1"; local new_dir="$2"; local commit="$3"; local basename="$4"; local report_dir="$5"

  local -a queue=()
  declare -A queued=()
  declare -A unresolved=()
  declare -A resolved=()

  # seed with primary files (if present)
  if [ -f "${old_dir}/${basename}" ]; then queue+=("${old_dir}/${basename}"); queued["${old_dir}/${basename}"]=1; fi
  if [ -f "${new_dir}/${basename}" ]; then queue+=("${new_dir}/${basename}"); queued["${new_dir}/${basename}"]=1; fi

  # include any .yang already present under old/new
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ -f "$f" ] && [ -z "${queued[$f]:-}" ]; then queue+=("$f"); queued["$f"]=1; fi
  done < <(find "$old_dir" "$new_dir" -type f -name '*.yang' 2>/dev/null || true)

  local i=0 made_progress found tok file newf_old newf_new parent parent_found tok_lc

  while : ; do
    made_progress=false
    while [ "$i" -lt "${#queue[@]}" ]; do
      file="${queue[i]}"
      i=$((i+1))
      # gather tokens (imports/includes)
      mapfile -t tokens < <(gather_needed_tokens "$file")
      for tok in "${tokens[@]}"; do
        [ -n "$tok" ] || continue

        # Workaround: ignore spurious token "the" (case-insensitive)
        tok_lc="$(printf '%s' "$tok" | tr '[:upper:]' '[:lower:]')"
        if [ "$tok_lc" = "the" ]; then
          continue
        fi

        if [ -n "${resolved[$tok]:-}" ]; then continue; fi
        if [ -f "${old_dir}/${tok}.yang" ] || [ -f "${new_dir}/${tok}.yang" ]; then
          resolved["$tok"]=1
          continue
        fi

        # attempt to find in repo
        found="$(find_dep_in_repo "$tok" || true)"
        if [ -n "$found" ]; then
          echo "    Resolved dependency '${tok}' -> $found"
          copy_if_missing "$found" "$old_dir"
          copy_if_missing "$found" "$new_dir"
          resolved["$tok"]=1
          made_progress=true

          # If the found file is a submodule, try to also copy its parent module
          parent=$(detect_submodule_parent "$found" || true)
          if [ -n "$parent" ]; then
            # skip spurious 'the' if that shows up
            parent_lc="$(printf '%s' "$parent" | tr '[:upper:]' '[:lower:]')"
            if [ "$parent_lc" != "the" ]; then
              parent_found="$(find_dep_in_repo "$parent" || true)"
              if [ -n "$parent_found" ]; then
                echo "      Detected submodule; also copying parent module '${parent}' -> $parent_found"
                copy_if_missing "$parent_found" "$old_dir"
                copy_if_missing "$parent_found" "$new_dir"
                # add parent to queue so its imports are scanned
                new_parent_old="${old_dir}/$(basename "$parent_found")"
                new_parent_new="${new_dir}/$(basename "$parent_found")"
                if [ -f "$new_parent_old" ] && [ -z "${queued[$new_parent_old]:-}" ]; then queue+=("$new_parent_old"); queued["$new_parent_old"]=1; fi
                if [ -f "$new_parent_new" ] && [ -z "${queued[$new_parent_new]:-}" ]; then queue+=("$new_parent_new"); queued["$new_parent_new"]=1; fi
              else
                echo "      Could not locate parent module '${parent}' in repo"
                unresolved["$parent"]=1
              fi
            fi
          fi

          # add the found file(s) to queue for further scanning
          newf_old="${old_dir}/$(basename "$found")"
          newf_new="${new_dir}/$(basename "$found")"
          if [ -f "$newf_old" ] && [ -z "${queued[$newf_old]:-}" ]; then queue+=("$newf_old"); queued["$newf_old"]=1; fi
          if [ -f "$newf_new" ] && [ -z "${queued[$newf_new]:-}" ]; then queue+=("$newf_new"); queued["$newf_new"]=1; fi

        else
          unresolved["$tok"]=1
        fi
      done
    done

    # If progress was made, some unresolved tokens may now be satisfied; prune them
    if [ "$made_progress" = true ]; then
      for tok in "${!unresolved[@]}"; do
        if [ -f "${old_dir}/${tok}.yang" ] || [ -f "${new_dir}/${tok}.yang" ]; then
          unset 'unresolved[$tok]'
          resolved["$tok"]=1
        fi
      done
      continue
    fi

    break
  done

  # portable count unresolved
  unresolved_count=0
  for _ in "${!unresolved[@]}"; do unresolved_count=$((unresolved_count + 1)); done

  # extra safety: drop spurious "the" if present
  if [ "${unresolved_count:-0}" -gt 0 ] && [ -n "${unresolved[the]:-}" ]; then unset 'unresolved[the]'; fi
  unresolved_count=0
  for _ in "${!unresolved[@]}"; do unresolved_count=$((unresolved_count + 1)); done

  if [ "$unresolved_count" -eq 0 ]; then
    return 0
  fi

  mkdir -p "$report_dir"
  {
    echo "MISSING DEPENDENCIES for ${basename} (commit ${commit}):"
    for tok in "${!unresolved[@]}"; do echo "$tok"; done
    echo
    echo "Searched repo root: $REPO_ROOT"
  } > "${report_dir}/MISSING_DEPS.txt"

  return 1
}

# process a single commit
process_commit() {
  local commit="$1"
  # Manifest can be either JSON (preferred) or CSV depending on export tooling.
  local manifest_json="${EXPORTS_PREFIX}/${commit}/manifest.json"
  local manifest_csv="${EXPORTS_PREFIX}/${commit}/manifest.csv"
  local manifest=""
  if [ -f "$manifest_json" ]; then
    manifest="$manifest_json"
  elif [ -f "$manifest_csv" ]; then
    manifest="$manifest_csv"
  else
    echo "WARN: manifest not found at ${manifest_json} or ${manifest_csv}; skipping ${commit}."
    return
  fi

  echo
  echo "=== Processing commit: ${commit} ==="

  # Extract modified entries from manifest
  # Prefer JSON parsing when manifest is JSON; fallback to CSV otherwise.
  if [[ "$manifest" == *.json ]] && command -v python3 >/dev/null 2>&1; then
    modified_entries=$(python3 -c "
import json
import sys
try:
    with open('$manifest', 'r') as f:
        data = json.load(f)
    for item in data:
        if isinstance(item, dict) and item.get('status') == 'modified':
            relpath = item.get('relpath', '')
            basename = item.get('primary_basename', '')
            old_path = item.get('old_primary_path', '')
            new_path = item.get('new_primary_path', '')
            if all([relpath, basename, old_path, new_path]):
                print(f'{relpath}|{basename}|{old_path}|{new_path}')
except Exception as e:
    sys.exit(1)
")
  else
    if [[ "$manifest" == *.csv ]]; then
      # CSV format is expected to have at least: status, relpath, basename, old_path, new_path.
      # We keep this resilient by using python if available, else awk.
      if command -v python3 >/dev/null 2>&1; then
        modified_entries=$(python3 -c "
import csv
import sys
with open('$manifest', newline='') as f:
    r = csv.DictReader(f)
    for row in r:
        if (row.get('status') or '').strip() == 'modified':
            relpath = (row.get('relpath') or '').strip()
            basename = (row.get('primary_basename') or row.get('basename') or '').strip()
            old_path = (row.get('old_primary_path') or row.get('old_path') or '').strip()
            new_path = (row.get('new_primary_path') or row.get('new_path') or '').strip()
            if relpath and basename and old_path and new_path:
                print(f"{relpath}|{basename}|{old_path}|{new_path}")
")
      else
        # Awk fallback (best-effort): assumes header contains the needed column names.
        modified_entries=$(awk -F, 'NR==1{for(i=1;i<=NF;i++){gsub(/\r/,"",$i);h[$i]=i}next} {s=$h["status"]; if($s=="modified"){print $h["relpath"]"|"$h["primary_basename"]"|"$h["old_primary_path"]"|"$h["new_primary_path"]}}' "$manifest" || true)
      fi
    else
      # Legacy grep/sed approach for JSON-like content
      modified_entries=$(grep -B2 -A5 '"status": "modified"' "$manifest" | grep -E '"(relpath|primary_basename|old_primary_path|new_primary_path)":' | sed 's/.*": *"\([^"]*\)".*/\1/' | paste -d'|' - - - -)
    fi
  fi
  
  if [ -z "$modified_entries" ]; then
    echo "No modified files found in ${commit}"
    return
  fi

  # Process each modified file
  echo "$modified_entries" | while IFS='|' read -r relpath basename old_path new_path; do
    [ -z "$relpath" ] && continue
    echo "Processing modified file: ${basename}"

    echo "Processing modified file: ${basename}"

    module_dir="$(dirname "$relpath")"
    old_dir="${EXPORTS_PREFIX}/${commit}/old/${module_dir}"
    new_dir="${EXPORTS_PREFIX}/${commit}/new/${module_dir}"
    mkdir -p "$old_dir" "$new_dir"

    # NOTE: Store each module under a unique folder to avoid collisions between
    # files with the same basename in different subdirectories.
    #
    # By default we DO NOT write to the legacy "${commit}/${basename}" location
    # because it is inherently collision-prone and can retain stale/bogus data.
    # If you have downstream tooling that still expects basename folders, you
    # can opt-in by exporting:
    #   ENABLE_LEGACY_BASENAME_ARCHIVE=1
    module_report="${REPORTS_ROOT}/${commit}/${relpath}"
    legacy_module_report="${REPORTS_ROOT}/${commit}/${basename}"
    mkdir -p "$module_report"
    printf '%s\n' "$relpath" > "${module_report}/RELATIVE_PATH.txt"
    if [ "${ENABLE_LEGACY_BASENAME_ARCHIVE:-0}" = "1" ]; then
      mkdir -p "$legacy_module_report"
      printf '%s\n' "$relpath" > "${legacy_module_report}/RELATIVE_PATH.txt"
    fi

    # clear previous tool output
    if [ -d "$TOOL_OUTPUT_DIR" ]; then
      rm -rf "${TOOL_OUTPUT_DIR:?}"/*
    else
      mkdir -p "$TOOL_OUTPUT_DIR"
    fi
    # Extra safety: remove any known, stale artifacts that could be copied from
    # previous runs if a later pipeline stage fails to regenerate them.
    rm -f "$TOOL_OUTPUT_DIR/final_report.json" \
          "$TOOL_OUTPUT_DIR/compatible_list.txt" \
          "$TOOL_OUTPUT_DIR/non_compatible_list.txt" \
          "$TOOL_OUTPUT_DIR/report.txt" \
          "$TOOL_OUTPUT_DIR/filtered_report.txt" \
          "$TOOL_OUTPUT_DIR/enriched_report_llm.txt" \
          "$TOOL_OUTPUT_DIR/enriched_report_llm_verified.txt" || true

    echo "  Resolving dependencies for ${basename}..."

    # Resolve dependencies and run comparator
    if resolve_dependencies_recursive "$old_dir" "$new_dir" "$commit" "$basename" "$module_report"; then
      echo "  All dependencies resolved; running comparator..."
      if run_wrapper "$basename" "$old_dir" "$new_dir"; then
        echo "  Comparator finished OK for ${basename}"
      else
        echo "  Comparator returned non-zero for ${basename}"
      fi
    else
      echo "  Missing dependencies for ${basename}; see ${module_report}/MISSING_DEPS.txt"
      echo "  Attempting to run comparator anyway..."
      if run_wrapper "$basename" "$old_dir" "$new_dir"; then
        echo "  Comparator finished OK for ${basename} (with missing dependencies)"
      else
        echo "  Comparator returned non-zero for ${basename} (with missing dependencies)"
      fi
    fi

    # archive comparator output (if any)
    if [ -d "$TOOL_OUTPUT_DIR" ] && [ "$(ls -A "$TOOL_OUTPUT_DIR")" ]; then
      if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete "$TOOL_OUTPUT_DIR"/ "$module_report"/
        if [ "${ENABLE_LEGACY_BASENAME_ARCHIVE:-0}" = "1" ]; then
          rsync -a --delete "$TOOL_OUTPUT_DIR"/ "$legacy_module_report"/
        fi
      else
        cp -a "$TOOL_OUTPUT_DIR"/. "$module_report"/ || true
        if [ "${ENABLE_LEGACY_BASENAME_ARCHIVE:-0}" = "1" ]; then
          cp -a "$TOOL_OUTPUT_DIR"/. "$legacy_module_report"/ || true
        fi
      fi
    else
      : > "${module_report}/NO_OUTPUT_PRODUCED"
      if [ "${ENABLE_LEGACY_BASENAME_ARCHIVE:-0}" = "1" ]; then
        : > "${legacy_module_report}/NO_OUTPUT_PRODUCED"
      fi
    fi

  done

  echo "=== Finished commit: ${commit} ==="
}

# main
mkdir -p "$REPORTS_ROOT"

if [ -n "$COMMIT_TO_RUN" ]; then
  process_commit "$COMMIT_TO_RUN"
else
  index_file="${EXPORTS_PREFIX}/commits_index.csv"
  if [ ! -f "$index_file" ]; then
    echo "ERROR: commits_index.csv not found at ${index_file}"
    exit 2
  fi
  tail -n +2 "$index_file" | cut -d, -f1 | while read -r c; do
    [ -n "$c" ] && process_commit "$c"
  done
fi

echo
echo "All done. Reports under: ${REPORTS_ROOT}"
