import argparse
import os
import sys
import shutil


# Allow running as a module from source tree
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))

# Import package modules (work whether installed or local)
try:
    from . import yang_comparator as comparator_mod
    from .report import filter_report as filter_mod
    from . import check_compatibility as compat_mod
    from .report import generate_compatibility_list as gen_compat_mod
    from .report import generate_non_compatibility_list as gen_noncompat_mod
    from .report import group_by_path as group_mod
    from .report import propagate_nbc_to_children as propagate_nbc_mod
except Exception:  # pragma: no cover
    # Fallback for direct file execution
    import yang_comparator as comparator_mod  # type: ignore
    from report import filter_report as filter_mod  # type: ignore
    import check_compatibility as compat_mod  # type: ignore
    from report import generate_compatibility_list as gen_compat_mod  # type: ignore
    from report import generate_non_compatibility_list as gen_noncompat_mod  # type: ignore
    from report import group_by_path as group_mod  # type: ignore
    from report import propagate_nbc_to_children as propagate_nbc_mod  # type: ignore


def run_pipeline(yang_old: str, yang_new: str, dir_old: str, dir_new: str, out_json: str,
                 compatibility_flag: str = None, xml_rules_path: str = None) -> int:
    # Ensure output directory exists
    out_dir = os.path.dirname(out_json) or "output"
    os.makedirs(out_dir, exist_ok=True)

    # All intermediate files live in out_dir (not hardcoded "output")
    input_report    = os.path.join(out_dir, "report.txt")
    filtered_report = os.path.join(out_dir, "filtered_report.txt")
    compat_out      = os.path.join(out_dir, "enriched_report_llm.txt")
    compatible_list    = os.path.join(out_dir, "compatible_list.txt")
    non_compatible_list = os.path.join(out_dir, "non_compatible_list.txt")
    final_json      = out_json  # final_report.json goes directly to out_json

    # Re-initialize rule sets with the custom XML so CONSTRAINT_KEYWORDS includes
    # any Phase-2-added constraints (e.g. max-access).
    # Priority: explicit xml_rules_path arg > YANG_XML_RULES env var.
    # NOTE: YANG_XML_RULES is cleared by the pyang plugin's finally block after
    # the first run, so the Phase 2 rerun must pass xml_rules_path explicitly.
    custom_xml = xml_rules_path or os.environ.get('YANG_XML_RULES')
    if custom_xml and os.path.isfile(custom_xml):
        comparator_mod._initialize_rule_sets(custom_xml)

    # 1) Compare and generate raw report
    comparator_mod.main(yang_old, yang_new, dir_old, dir_new, output_file=input_report)

    # 2) Filter the report
    filter_mod.filter_and_write_report(input_report, filtered_report)

    # Some long/batch runs may lose the filtered intermediate file due to
    # concurrent workspace activity. Fall back to the raw report so the
    # compatibility stage remains runnable instead of crashing hard.
    comparison_source = filtered_report
    if not os.path.exists(filtered_report):
        if os.path.exists(input_report):
            print(f"[cli] ⚠️  Warning: missing filtered report: {filtered_report}")
            print(f"[cli] Falling back to raw report: {input_report}")
            try:
                shutil.copyfile(input_report, filtered_report)
                comparison_source = filtered_report
            except Exception:
                comparison_source = input_report
        else:
            raise FileNotFoundError(
                f"Neither filtered report nor raw report exists: {filtered_report}, {input_report}"
            )

    # 3) Enrich with compatibility tags (with typedef resolution)
    # Support compatibility_flag (e.g., "rfc7950") from environment or parameter
    if not compatibility_flag:
        compatibility_flag = os.environ.get('YANG_COMPAT_FLAG')
    
    # Use explicit xml_rules_path arg first (survives the pyang plugin's finally-block
    # env-var cleanup), then fall back to YANG_XML_RULES env var, then bundled default.
    rules_xml = xml_rules_path or os.environ.get('YANG_XML_RULES') or os.path.join(PACKAGE_DIR, "compatibility_rules.xml")
    old_yang_path = os.path.join(dir_old, yang_old)
    new_yang_path = os.path.join(dir_new, yang_new)
    search_dirs = [dir_old, dir_new]
    # Determine if LLM verification is active so we can inject <needs-deep-analysis>
    # tags during enrichment for all elements with assistance="true" in the XML rules.
    enable_llm = os.environ.get('ENABLE_LLM_VERIFICATION') == '1'

    compat_mod.enrich_report(comparison_source, rules_xml, compat_out,
                            old_yang_file=old_yang_path,
                            new_yang_file=new_yang_path,
                            search_dirs=search_dirs,
                            compatibility_flag=compatibility_flag,
                            llm_verify=enable_llm)

    # 3.5) Optional LLM verification for complex constraints
    llm_verification_results = None  # Store for final report
    
    if enable_llm:
        try:
            from ..dspy import llm_verification_hook as llm_mod, IS_LITE_MODE, COMPARATOR_MODE
            llm_model = os.environ.get('LLM_MODEL', 'gpt-4o-mini')
            print(f"[cli] 🤖 LLM verification ENABLED (mode: {COMPARATOR_MODE})")
            print(f"[cli] Running LLM verification with model: {llm_model}")
            
            llm_result = llm_mod.verify_enriched_report(
                enriched_report_path=compat_out,
                output_dir=out_dir,
                model_name=llm_model,
                # Use explicit xml_rules_path (survives the pyang plugin's finally-block
                # env-var cleanup) before falling back to YANG_XML_RULES env var.
                xml_rules_path=xml_rules_path or os.environ.get('YANG_XML_RULES'),
                old_yang_file=old_yang_path,
                new_yang_file=new_yang_path,
                search_dirs=search_dirs,
            )

            # Always store the returned result so the final report can reflect
            # that verification was attempted and include any stats or errors.
            llm_verification_results = llm_result

            if llm_result.get('success'):
                # Update compat_out to point to LLM-verified report
                compat_out = llm_result.get('verified_report_path', compat_out)
                print(f"[cli] ✅ LLM verification completed: {llm_result.get('stats', {})}")
            else:
                print(f"[cli] ⚠️  Warning: LLM verification failed: {llm_result.get('error')}")
        except Exception as e:
            # Record the exception in a structured result so callers see the
            # verification was attempted and why it failed.
            llm_verification_results = {
                'success': False,
                'error': str(e),
                'stats': {'total': 0, 'verified': 0},
            }
            print(f"[cli] ⚠️  Warning: LLM verification failed: {e}")
            import traceback
            traceback.print_exc()

    # 3.7) Propagate NBC tags from parent entries to all-BC children in the enriched report.
    #      This ensures that when a structural entry (e.g. "type added") is classified as
    #      <non-backward-compatible> but all its attribute children are <backward-compatible>,
    #      the children are promoted to <non-backward-compatible> so the downstream list
    #      generators count them consistently with the default (non-flag) mode.
    try:
        propagate_nbc_mod.propagate_nbc(compat_out, compat_out)
        print("[cli] propagate_nbc_to_children completed")
    except Exception as e:
        print(f"[cli] Warning: propagate_nbc_to_children failed: {e}")
        import traceback
        traceback.print_exc()

    # 4) Generate compatible and non-compatible lists into out_dir
    # Export YANG file paths for line-number correction (mirrors yang_comparator.sh)
    os.environ["YANG_OLD_FILE"]            = os.path.join(dir_old, yang_old)
    os.environ["YANG_NEW_FILE"]            = os.path.join(dir_new, yang_new)
    os.environ["YANG_ENRICHED_FILE"]       = compat_out
    os.environ["YANG_COMPAT_LIST_FILE"]    = compatible_list
    os.environ["YANG_NON_COMPAT_LIST_FILE"] = non_compatible_list
    os.environ["YANG_LLM_VERIFY_FILE"]     = os.path.join(out_dir, "llm_verification_report.json")
    print(f"[cli] Set YANG_ENRICHED_FILE={compat_out}")
    print(f"[cli] Generating compatibility lists from: {compat_out}")
    gen_compat_mod.main()
    gen_noncompat_mod.main()

    # 5) Group results using full group_by_path pipeline
    #    (categorises into compatible/non_compatible/other-errors/unmarked, strips tag/all_tags)
    # Build an extended search path for pyang: include the immediate YANG dirs plus
    # their parent directories (up to 2 levels) so that sibling modules like
    # openconfig-segment-routing.yang (in a different sub-directory of release/models/)
    # can be resolved when pyang validates the main file.
    _pyang_search_dirs = []
    for _d in (dir_old, dir_new):
        if _d and _d not in _pyang_search_dirs:
            _pyang_search_dirs.append(_d)
        # Add parent dir (e.g. release/models/ when dir is release/models/rib/)
        _parent = os.path.dirname(_d) if _d else None
        if _parent and _parent not in _pyang_search_dirs:
            _pyang_search_dirs.append(_parent)
        # Add grandparent dir (e.g. release/ when dir is release/models/rib/)
        _grandparent = os.path.dirname(_parent) if _parent else None
        if _grandparent and _grandparent not in _pyang_search_dirs:
            _pyang_search_dirs.append(_grandparent)

    _saved_argv = sys.argv
    sys.argv = [
        "group_by_path",
        "--compatible",        compatible_list,
        "--non-compatible",    non_compatible_list,
        "--out",               final_json,
        "--pyang-old-file",    old_yang_path,
        "--pyang-new-file",    new_yang_path,
        "--pyang-search-dirs", *_pyang_search_dirs,
    ]
    try:
        group_mod.main()
    finally:
        sys.argv = _saved_argv

    # 6) Group duplicate changes (same change repeated across augment/uses expansions)
    try:
        from .report import group_duplicate_changes as dedup_mod
        import json
        with open(final_json, 'r', encoding='utf-8') as f:
            report_data = json.load(f)
        grouped_report = dedup_mod.group_report(report_data)
        with open(final_json, 'w', encoding='utf-8') as f:
            json.dump(grouped_report, f, indent=2, ensure_ascii=False)
        print("[cli] group_duplicate_changes completed")
    except Exception as e:
        print(f"Warning: group_duplicate_changes failed: {e}")
        import traceback
        traceback.print_exc()

    # 7) Inject LLM verification results into the final grouped report
    # Build a clear summary regardless of whether verification succeeded,
    # so callers can distinguish: requested vs executed vs success vs stats.
    import json
    with open(final_json, 'r', encoding='utf-8') as f:
        report_data = json.load(f)

    # Was LLM verification requested by the caller?
    requested_llm = os.environ.get('ENABLE_LLM_VERIFICATION') == '1'

    # Default summary
    llm_summary = {
        'enabled': bool(requested_llm),
        'requested': bool(requested_llm),
        'executed': False,
        'success': False,
        'model': os.environ.get('LLM_MODEL', None),
        'stats': {'total': 0, 'verified': 0},
    }

    if llm_verification_results is not None:
        # We attempted verification; record execution + any returned stats/error
        llm_summary['executed'] = True
        llm_summary['success'] = bool(llm_verification_results.get('success'))
        llm_summary['stats'] = llm_verification_results.get('stats', llm_summary['stats'])
        if 'error' in llm_verification_results:
            llm_summary['error'] = llm_verification_results.get('error')

        # Build a rich lookup map from the LLM verification report JSON.
        # Key: "yang_path|keyword_or_constraint|action"  →  full verification dict
        # This is used to inject llm_assistance_decision, llm_classification and
        # explanation into matching constraints/attributes in the final report.
        llm_verify_file = os.path.join(out_dir, "llm_verification_report.json")
        rich_verification_map: dict = {}
        if os.path.exists(llm_verify_file):
            try:
                with open(llm_verify_file, 'r', encoding='utf-8') as _f:
                    _llm_data = json.load(_f)
                for _item in _llm_data.get('items', []):
                    _change = _item.get('change', {})
                    _veri = _item.get('verification', {})
                    _key = f"{_change.get('yang_path','')}|{_change.get('keyword_or_constraint','')}|{_change.get('action','')}"
                    rich_verification_map[_key] = _veri
            except Exception as _e:
                print(f"[cli] Warning: could not load LLM verification report for enrichment: {_e}")

        # If verification produced a verification_map, inject decisions into the report
        verification_map = llm_verification_results.get('verification_map', {}) if llm_verification_results.get('success') else {}

        def _get_paths(item):
            """Return a list of paths for an item (handles both scalar and list paths)."""
            path = item.get('path', '')
            if isinstance(path, list):
                return [p for p in path if p]
            return [path] if path else []

        def _lookup_rich_map(rich_map, paths, key_suffix):
            """Try each path in paths to find a match in rich_map.
            Returns (veri_dict, matched_path) or (None, None)."""
            for p in paths:
                key = f"{p}|{key_suffix}"
                if key in rich_map:
                    return rich_map[key], p
            return None, None

        # Vocabulary mapping for XPath/path attribute changes.
        # The LLM uses constraint vocabulary (narrowed/relaxed/equivalent/incomparable)
        # but for path changes the correct vocabulary is equivalent/not_equivalent.
        _XPATH_CLASSIFICATION_MAP = {
            'narrowed':     'not_equivalent',   # path points to a different schema node
            'relaxed':      'equivalent',        # path points to the same schema node (broader)
            'equivalent':   'equivalent',        # path points to the same schema node
            'incomparable': 'needs_review',      # cannot determine equivalence
        }

        def _normalize_xpath_classification(veri: dict, attr_name: str) -> dict:
            """Normalize llm_classification for XPath/path attributes.

            The LLM uses constraint vocabulary (narrowed/relaxed) but for path
            changes the correct vocabulary is equivalent/not_equivalent.
            """
            if attr_name not in ('path', 'name'):
                return veri
            cls = veri.get('llm_classification', '')
            if cls in _XPATH_CLASSIFICATION_MAP:
                veri = dict(veri)  # don't mutate original
                veri['llm_classification'] = _XPATH_CLASSIFICATION_MAP[cls]
            return veri

        def inject_llm_decision(items, verification_map, rich_map):
            injected_count = 0
            for item in items:
                paths = _get_paths(item)
                path = paths[0] if paths else ''  # primary path for legacy lookup

                # Inject into constraints
                for constraint in item.get('constraints', []):
                    constraint_type = constraint.get('constraint', '')
                    action = constraint.get('action', '')
                    level = constraint.get('level', '')

                    # Legacy map lookup (keyed by path|level|constraint)
                    legacy_key = f"{path}|{level}|{constraint_type}"
                    if legacy_key in verification_map:
                        constraint['llm_decision'] = verification_map[legacy_key]
                        injected_count += 1

                    # Rich map lookup — try all paths (handles grouped items with list paths)
                    veri, _ = _lookup_rich_map(rich_map, paths, f"{constraint_type}|{action}")
                    if veri is not None:
                        # Insert fields right after llm_assistance_decision if present
                        if 'llm_assistance_decision' in constraint:
                            new_constraint = {}
                            for k, v in constraint.items():
                                new_constraint[k] = v
                                if k == 'llm_assistance_decision':
                                    if 'static_tool_decision' in veri:
                                        new_constraint['static_tool_decision'] = veri['static_tool_decision']
                                    if 'static_tool_reason' in veri:
                                        new_constraint['static_tool_reason'] = veri['static_tool_reason']
                                    if 'llm_classification' in veri:
                                        new_constraint['llm_classification'] = veri['llm_classification']
                                    if 'explanation' in veri:
                                        new_constraint['explanation'] = veri['explanation']
                            constraint.clear()
                            constraint.update(new_constraint)
                        else:
                            if 'static_tool_decision' in veri:
                                constraint['static_tool_decision'] = veri['static_tool_decision']
                            if 'static_tool_reason' in veri:
                                constraint['static_tool_reason'] = veri['static_tool_reason']
                            if 'llm_classification' in veri:
                                constraint['llm_classification'] = veri['llm_classification']
                            if 'explanation' in veri:
                                constraint['explanation'] = veri['explanation']
                        injected_count += 1

                # Also inject into attributes (in case attributes have LLM decisions)
                for attribute in item.get('attributes', []):
                    attr_type = attribute.get('attribute', '')
                    action = attribute.get('action', '')
                    # Try all paths (handles grouped items with list paths)
                    veri, _ = _lookup_rich_map(rich_map, paths, f"{attr_type}|{action}")
                    if veri is not None:
                        if 'llm_assistance_decision' in attribute:
                            new_attribute = {}
                            for k, v in attribute.items():
                                new_attribute[k] = v
                                if k == 'llm_assistance_decision':
                                    if 'static_tool_decision' in veri:
                                        new_attribute['static_tool_decision'] = veri['static_tool_decision']
                                    if 'static_tool_reason' in veri:
                                        new_attribute['static_tool_reason'] = veri['static_tool_reason']
                                    if 'llm_classification' in veri:
                                        new_attribute['llm_classification'] = veri['llm_classification']
                                    if 'explanation' in veri:
                                        new_attribute['explanation'] = veri['explanation']
                            attribute.clear()
                            attribute.update(new_attribute)
                        else:
                            if 'static_tool_decision' in veri:
                                attribute['static_tool_decision'] = veri['static_tool_decision']
                            if 'static_tool_reason' in veri:
                                attribute['static_tool_reason'] = veri['static_tool_reason']
                            if 'llm_classification' in veri:
                                attribute['llm_classification'] = veri['llm_classification']
                            if 'explanation' in veri:
                                attribute['explanation'] = veri['explanation']
                        injected_count += 1

            return items, injected_count

        total_injected = 0
        if verification_map or rich_verification_map:
            for section in ('compatible', 'non_compatible', 'other-errors', 'unmarked'):
                if section in report_data:
                    report_data[section], n = inject_llm_decision(
                        report_data[section], verification_map, rich_verification_map)
                    total_injected += n

            if total_injected > 0:
                print(f"[cli] ✅ Injected {total_injected} LLM decisions into final report")

    # Always write the llm_verification_summary into the final report so callers can inspect
    report_data['llm_verification_summary'] = llm_summary

    with open(final_json, 'w', encoding='utf-8') as f:
        json.dump(report_data, f, indent=2, ensure_ascii=False)

    print(f"[cli] Final report: {final_json}")

    # 8) Generate concise_final_report.json alongside final_report.json.
    #    The concise report applies condensing rules:
    #      - Strip attributes/constraints from "added"/"deleted" entries
    #      - Detect and reclassify renames
    #      - Deduplicate child-path entries implied by parent entries
    #      - Merge grouping-expansion duplicates
    try:
        from pathlib import Path as _Path
        from .report import generate_concise_report as _concise_mod

        with open(final_json, 'r', encoding='utf-8') as _f:
            _report_for_concise = json.load(_f)
        _concise, _renamed, _name_removed, _deduped, _merged = _concise_mod.condense_report(_report_for_concise)
        _concise_json = str(_Path(final_json).parent / "concise_final_report.json")
        with open(_concise_json, 'w', encoding='utf-8') as _f:
            json.dump(_concise, _f, indent=2, ensure_ascii=False)
        print(f"[cli] Concise report: {_concise_json}")
        if _renamed or _deduped or _merged:
            print(f"[cli]   renamed={_renamed}, deduped={_deduped}, merged={_merged}")
    except Exception as _e:
        print(f"[cli] Warning: concise report generation failed: {_e}")

    return 0



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yang-comparator",
        description="Compare two YANG modules and produce a compatibility report",
    )
    parser.add_argument("yang_old", help="Old YANG file name (e.g. openconfig-platform.yang)")
    parser.add_argument("yang_new", help="New YANG file name")
    parser.add_argument("dir_old", help="Directory containing old module and dependencies")
    parser.add_argument("dir_new", help="Directory containing new module and dependencies")
    parser.add_argument("--out", dest="out", default=os.path.join("output", "final_report.json"),
                        help="Output JSON path (default: output/final_report.json)")
    parser.add_argument("--rfc7950", dest="rfc7950", action="store_true",
                        help="Use RFC 7950 strict compatibility mode")

    args = parser.parse_args(argv)
    try:
        flag = 'rfc7950' if args.rfc7950 else None
        return run_pipeline(args.yang_old, args.yang_new, args.dir_old, args.dir_new, args.out, compatibility_flag=flag)
    except KeyboardInterrupt:
        print("Interrupted")
        return 130
    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
