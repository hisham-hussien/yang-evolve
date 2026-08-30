"""
Lightweight pyang plugin for YANG compatibility checking.
Integrates directly with the comparator module without RAG dependencies.
"""

import optparse
import sys
import os
import json
from pathlib import Path

from pyang import plugin


def pyang_plugin_init():
    """Initialize the pyang plugin."""
    plugin.register_plugin(YangCompatibilityPlugin())


class YangCompatibilityPlugin(plugin.PyangPlugin):
    """Pyang plugin for YANG backward compatibility checking."""
    
    def add_opts(self, optparser):
        """Add plugin-specific options to pyang."""
        # Collect every option string already registered (main parser + all groups)
        existing_opts = {str(o) for o in optparser.option_list}
        for grp in getattr(optparser, 'option_groups', []):
            existing_opts.update(str(o) for o in grp.option_list)

        def _opt(flag, **kwargs):
            """Return make_option(...) only if the flag is not yet registered."""
            if flag in existing_opts:
                return None
            return optparse.make_option(flag, **kwargs)

        candidates = [
            _opt("--check-compatibility",
                 dest="check_compatibility", action="store_true",
                 help="Check YANG module compatibility between old and new versions"),
            _opt("--old-version",
                 dest="old_version",
                 help="Old YANG module file name"),
            _opt("--new-version",
                 dest="new_version",
                 help="New YANG module file name"),
            _opt("--old-dir",
                 dest="old_dir",
                 help="Directory containing old module and dependencies"),
            _opt("--new-dir",
                 dest="new_dir",
                 help="Directory containing new module and dependencies"),
            _opt("--output-dir",
                 dest="output_dir", default="output",
                 help="Output directory for compatibility reports (default: output)"),
            _opt("--rfc7950",
                 dest="rfc7950_mode", action="store_true",
                 help="Use RFC 7950 strict compatibility mode"),
            _opt("--llm-verify",
                 dest="llm_verify", action="store_true",
                 help="Enable LLM verification for complex cases (requires LLM dependencies)"),
            _opt("--llm-model",
                 dest="llm_model", default="gpt-4o-mini",
                 help="LLM model to use for verification (default: gpt-4o-mini)"),
            _opt("--xml-rules",
                 dest="xml_rules", default=None,
                 help=("Path to a custom compatibility_rules.xml. "
                       "Defaults to the bundled rules in yang_rag/comparator/compatibility_rules.xml.")),
            _opt("--rag-top-k",
                 dest="rag_top_k", default=5, type="int",
                 help=("Number of top-K RAG candidates to show per unmarked keyword in Phase 2 "
                       "(default: 5). Phase 2 runs automatically whenever [UNMARKED] statements "
                       "are found in the report. Requires the RAG index (data/index/) and "
                       "API_KEY to be set.")),
        ]
        optlist = [o for o in candidates if o is not None]

        if optlist:
            g = optparser.add_option_group("YANG Compatibility Checker")
            g.add_options(optlist)
    
    def setup_ctx(self, ctx):
        """Setup plugin context - run compatibility check here to avoid waiting for validation."""
        if not ctx.opts.check_compatibility:
            return
        
        # Validate required options
        if not all([
            ctx.opts.old_version,
            ctx.opts.new_version,
            ctx.opts.old_dir,
            ctx.opts.new_dir,
        ]):
            sys.stderr.write(
                "Error: --check-compatibility requires --old-version, "
                "--new-version, --old-dir, and --new-dir\n"
            )
            sys.exit(1)
        
        # Run the comparison immediately and exit
        self._run_compatibility_check(ctx)
        sys.exit(0)
    
    def pre_validate_ctx(self, ctx, modules):
        """Run before validation."""
        pass
    
    def post_validate_ctx(self, ctx, modules):
        """Run after validation - not used since we exit in setup_ctx."""
        pass
    
    def _run_compatibility_check(self, ctx):
        """Execute the compatibility check."""
        
        # Import comparator CLI
        try:
            from yang_rag.comparator import cli as comparator_cli
        except ImportError as e:
            import traceback
            sys.stderr.write(f"Error: Failed to import comparator: {e}\n")
            traceback.print_exc(file=sys.stderr)
            sys.exit(1)
        
        # Expand and resolve paths (handle ~ and relative paths)
        old_version = os.path.expanduser(ctx.opts.old_version)
        new_version = os.path.expanduser(ctx.opts.new_version)
        old_dir = os.path.expanduser(ctx.opts.old_dir)
        new_dir = os.path.expanduser(ctx.opts.new_dir)
        
        # Convert to absolute paths if relative
        if not os.path.isabs(old_dir):
            old_dir = os.path.abspath(old_dir)
        if not os.path.isabs(new_dir):
            new_dir = os.path.abspath(new_dir)
        
        # Extract just the filename from old_version and new_version if they contain paths
        old_filename = os.path.basename(old_version)
        new_filename = os.path.basename(new_version)
        
        # Set environment variables for compatibility mode
        if ctx.opts.rfc7950_mode:
            os.environ["YANG_COMPAT_FLAG"] = "rfc7950"
            print("🔒 Strict compatibility mode ENABLED: rfc7950")
        
        # Set LLM verification mode
        if ctx.opts.llm_verify:
            os.environ["ENABLE_LLM_VERIFICATION"] = "1"
            os.environ["LLM_MODEL"] = ctx.opts.llm_model
            print(f"🤖 LLM verification ENABLED: {ctx.opts.llm_model}")
        
        # Forward custom rules path if provided
        if getattr(ctx.opts, 'xml_rules', None):
            os.environ["YANG_XML_RULES"] = ctx.opts.xml_rules
            print(f"📋 Custom XML rules: {ctx.opts.xml_rules}")
        
        # Prepare output path — resolve to absolute so it's correct regardless
        # of the working directory from which pyang is invoked.
        output_dir = Path(ctx.opts.output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_json = output_dir / "final_report.json"
        
        print(f"\n🔍 Comparing YANG modules:")
        print(f"   Old: {old_filename} (in {old_dir})")
        print(f"   New: {new_filename} (in {new_dir})")
        print(f"   Output: {output_json}")
        print()
        
        # Run the comparison pipeline
        try:
            compat_flag = "rfc7950" if ctx.opts.rfc7950_mode else None

            comparator_cli.run_pipeline(
                yang_old=old_filename,
                yang_new=new_filename,
                dir_old=old_dir,
                dir_new=new_dir,
                out_json=str(output_json),
                compatibility_flag=compat_flag,
            )

            print("\n✅ Compatibility check completed successfully!")
            print(f"📊 Reports generated in: {output_dir}")

            # ---------------------------------------------------------------
            # Phase 2: RAG loop for unmarked statements (runs automatically)
            # ---------------------------------------------------------------
            xml_rules_path = getattr(ctx.opts, 'xml_rules', None)
            top_k = getattr(ctx.opts, 'rag_top_k', 5)
            rerun = self._run_rag_phase2(
                output_json=output_json,
                old_filename=old_filename,
                new_filename=new_filename,
                old_dir=old_dir,
                new_dir=new_dir,
                output_dir=output_dir,
                xml_rules_path=xml_rules_path,
                top_k=top_k,
                compat_flag=compat_flag,
                comparator_cli=comparator_cli,
            )
            if rerun:
                print("\n✅ Phase 2 complete — final report updated with new rules applied.")

        except BaseException as e:
            if isinstance(e, SystemExit):
                error_msg = f"Pipeline exited with code {e.code}"
            else:
                error_msg = str(e)

            sys.stderr.write(f"\n❌ Error during compatibility check: {error_msg}\n")
            import traceback
            # Persist a minimal failure artifact so batch workflows don't leave
            # missing final_report.json folders on parse/runtime failures.
            try:
                error_report = {
                    "status": "error",
                    "error_type": type(e).__name__,
                    "error_message": error_msg,
                    "old_version": old_filename,
                    "new_version": new_filename,
                    "old_dir": old_dir,
                    "new_dir": new_dir,
                    "llm_verification_summary": {
                        "enabled": bool(ctx.opts.llm_verify),
                        "requested": bool(ctx.opts.llm_verify),
                        "executed": False,
                        "success": False,
                        "model": ctx.opts.llm_model if ctx.opts.llm_verify else None,
                        "stats": {"total": 0, "verified": 0},
                    },
                }
                with open(output_json, "w", encoding="utf-8") as fh:
                    json.dump(error_report, fh, indent=2, ensure_ascii=False)
                sys.stderr.write(f"📄 Wrote failure report: {output_json}\n")
            except Exception as write_err:
                sys.stderr.write(f"⚠️ Could not write failure report: {write_err}\n")
            traceback.print_exc()
            if isinstance(e, SystemExit):
                code = e.code if isinstance(e.code, int) else 1
                sys.exit(code)
            sys.exit(1)
        finally:
            for _env_key in ("YANG_COMPAT_FLAG", "ENABLE_LLM_VERIFICATION", "LLM_MODEL", "YANG_XML_RULES",
                             "YANG_ENRICHED_FILE", "YANG_COMPAT_LIST_FILE", "YANG_NON_COMPAT_LIST_FILE",
                             "YANG_LLM_VERIFY_FILE"):
                os.environ.pop(_env_key, None)

    # ------------------------------------------------------------------
    # Phase 2: RAG loop for unmarked statements
    # ------------------------------------------------------------------

    @staticmethod
    def _load_covered_keywords(xml_rules: "Path") -> set:
        """
        Parse compatibility_rules.xml and return the set of all keyword,
        attribute, and constraint names that already have rules.

        This set grows automatically as new rules are added to the XML —
        no hardcoded list needed.
        """
        import xml.etree.ElementTree as ET
        covered: set = set()
        try:
            tree = ET.parse(str(xml_rules))
            root = tree.getroot()
            for elem in root.iter():
                # Cover all element types used in compatibility_rules.xml:
                # <structural> (new schema), <keyword> (legacy), <attribute>, <constraint>
                # A keyword added by Phase 2 may land in any of these categories
                # depending on what the generated rule uses — we must recognise all of
                # them so the re-run does not re-process already-covered keywords.
                if elem.tag in ("structural", "keyword", "attribute", "constraint") and elem.text:
                    covered.add(elem.text.strip())
                # Also pick up any 'name' attribute on elements
                name = elem.get("name", "").strip()
                if name:
                    covered.add(name)
        except Exception as exc:
            print(f"  [WARN] Could not parse XML rules for covered-keyword check: {exc}")
        return covered

    def _run_rag_phase2(
        self,
        output_json: "Path",
        old_filename: str,
        new_filename: str,
        old_dir: str,
        new_dir: str,
        output_dir: "Path",
        xml_rules_path,
        top_k: int,
        compat_flag,
        comparator_cli,
    ) -> bool:
        """
        Phase 2: for every [UNMARKED] entry in final_report.json, run the
        RAG pipeline (experiment_rag_candidates.py) interactively so the user
        can pick the best similar keyword, optionally edit the cloned rules,
        and confirm the update to compatibility_rules.xml.

        After all keywords are processed, reruns the comparator so the newly
        added rules are applied and the report is refreshed.

        Returns True if at least one keyword was processed and the comparator
        was rerun, False otherwise.
        """
        import subprocess

        sep = "=" * 72

        # ---- 1. Resolve XML rules path ----
        _plugin_dir = Path(__file__).resolve().parent
        if xml_rules_path:
            xml_rules = Path(xml_rules_path).resolve()
        else:
            xml_rules = _plugin_dir / "compatibility_rules.xml"

        if not xml_rules.exists():
            print(f"\n⚠️  Phase 2: XML rules not found: {xml_rules}")
            return False

        # ---- 1b. Check if a rerun is needed even without Phase 2 processing ----
        # When keywords were added to the XML in a PREVIOUS Phase 2 session (e.g.
        # max-access added as <constraint>), _load_covered_keywords now marks them
        # as covered so collect_uncovered_statements() won't return them.  But the
        # enriched report may still have untagged lines for those keywords because
        # the report was generated before the rules were added.
        # Detect this by checking if the current final_report.json has any items
        # in the 'unmarked' section whose keywords are now covered by the XML.
        _covered_now = self._load_covered_keywords(xml_rules)
        _needs_rerun_for_covered = False
        try:
            import json as _json
            _report_data = _json.loads(output_json.read_text(encoding="utf-8"))
            for _item in _report_data.get("unmarked", []):
                _kw = _item.get("keyword", "").strip()
                if _kw and _kw in _covered_now:
                    _needs_rerun_for_covered = True
                    break
                for _attr in _item.get("attributes", []):
                    _ak = _attr.get("attribute", "").strip()
                    if _ak and _ak in _covered_now:
                        _needs_rerun_for_covered = True
                        break
                if _needs_rerun_for_covered:
                    break
        except Exception:
            pass

        if _needs_rerun_for_covered:
            print(f"\n📋 Detected unmarked items that are now covered by XML rules — forcing rerun...")
            try:
                comparator_cli.run_pipeline(
                    yang_old=old_filename,
                    yang_new=new_filename,
                    dir_old=old_dir,
                    dir_new=new_dir,
                    out_json=str(output_json),
                    compatibility_flag=compat_flag,
                    xml_rules_path=str(xml_rules) if xml_rules else None,
                )
                print("\n✅ Rerun complete — report updated with newly covered keywords.")
                # Do NOT return here — continue to Phase 2 to process any remaining
                # uncovered keywords (e.g. 'privileges') that still have no rules.
            except Exception as exc:
                print(f"\n⚠️  Rerun failed: {exc}")
                return False

        # ---- 2. Collect uncovered statements using statement_extractor ----
        # statement_extractor handles:
        #   - Reading final_report.json
        #   - Filtering YANG builtins and already-covered keywords (from XML)
        #   - Building accurate bare queries by reading the actual YANG file
        try:
            from yang_rag.rag.statement_extractor import collect_uncovered_statements
            # Pass old_dir and new_dir as search roots so the extractor can
            # resolve bare YANG filenames (e.g. "openconfig-interfaces.yang")
            # to absolute paths for accurate bare-query construction.
            # Also include parent directories (models root) for cross-module deps.
            search_dirs = []
            # new_dir must come BEFORE old_dir so that bare filenames (e.g.
            # "demo-defval.yang") resolve to the *new* version of the file.
            # The report's new_value is what we want to extract for the bare query.
            for d in [new_dir, old_dir]:
                if d:
                    search_dirs.append(d)
                    parent = os.path.dirname(d)
                    if parent and parent not in search_dirs:
                        search_dirs.append(parent)
                    grandparent = os.path.dirname(parent) if parent else None
                    if grandparent and grandparent not in search_dirs:
                        search_dirs.append(grandparent)
            entries = collect_uncovered_statements(
                report_path=output_json,
                xml_rules_path=xml_rules,
                search_dirs=search_dirs,
            )
        except Exception as exc:
            import traceback
            print(f"\n⚠️  Phase 2: statement_extractor failed: {exc}")
            traceback.print_exc()
            return False

        if not entries:
            print("\n✅ Phase 2: no [UNMARKED] statements — nothing to do.")
            return False

        print(f"\n{sep}")
        print(f"  PHASE 2: RAG Search for {len(entries)} Uncovered Extension Keyword(s)")
        print(sep)
        print()
        for e in entries:
            print(f"  • {e.keyword}  →  {e.bare_query}")
        print()

        # ---- 3. For each keyword, call simple_rule_updater_cli ----
        # simple_rule_updater_cli accepts:
        #   yang_structure  — the bare query (e.g. "oc-ext:telemetry-on-change;")
        #   --yang-file     — path to the YANG file (for accurate context extraction)
        #   --line-number   — line number in the YANG file
        #   --xml           — path to compatibility_rules.xml
        #   --n-results     — number of RAG candidates to show
        updated = 0
        skipped = 0

        for entry in entries:
            print()
            print("-" * 60)
            print(f"  Unmarked keyword : '{entry.keyword}'")
            print(f"  Bare RAG query   : {entry.bare_query}")
            if entry.yang_file:
                print(f"  YANG file        : {entry.yang_file}")
            if entry.line_no:
                print(f"  Line             : {entry.line_no}")
            print("-" * 60)

            cli_args = [
                sys.executable, "-m", "yang_rag.dspy.simple_rule_updater_cli",
                entry.bare_query,          # yang_structure positional arg (accurate single line)
                "--xml", str(xml_rules),
                "--n-results", str(top_k),
                # Pass the bare query as the RAG context directly — it's already the
                # accurate single-line YANG statement (e.g. "oc-ext:telemetry-on-change;")
                # read from the YANG file by statement_extractor.
                # Using --yang-context avoids the 5-line snippet window which adds noise.
                "--yang-context", entry.bare_query,
            ]
            if entry.yang_file:
                cli_args += ["--yang-file", entry.yang_file]
            if entry.line_no:
                cli_args += ["--line-number", str(entry.line_no)]

            try:
                result = subprocess.run(cli_args)
                exit_code = result.returncode
            except Exception as exc:
                print(f"  [ERROR] Failed to launch RAG pipeline: {exc}")
                exit_code = 1

            if exit_code == 0:
                updated += 1
                print(f"  [OK] Rules updated for '{entry.keyword}'")
            elif exit_code == 2:
                skipped += 1
                print(f"  [SKIP] User skipped '{entry.keyword}'")
            else:
                skipped += 1
                print(f"  [FAIL] Pipeline failed for '{entry.keyword}' (exit {exit_code})")

        print(f"\nPhase 2 complete: {updated} updated, {skipped} skipped.")

        # Always rerun when there were uncovered keywords, even if no new rules
        # were added this session.  Reasons:
        # 1. Rules may have been added in a PREVIOUS Phase 2 session (e.g. max-access
        #    added as <constraint> last time) but the enriched report was never
        #    regenerated with those rules applied.
        # 2. The cross-match fix in enrich_report can now tag keywords that the
        #    comparator classified as "attribute" but whose rules are in <constraint>
        #    elements — but only if enrich_report is re-run with the updated XML.
        # Only skip if there were truly no uncovered keywords at all.
        if updated == 0 and not entries:
            print("No rules were updated and no uncovered keywords — skipping rerun.")
            return False
        if updated == 0:
            print("No new rules added this run — rerunning to apply existing XML rules to report...")

        # ---- 6. Rerun the comparator with the updated rules ----
        print(f"\n{sep}")
        print("  PHASE 2 RERUN: Applying updated rules to generate final report...")
        print(sep)
        print()

        # Build the set of keywords that were unmarked before the rerun
        keywords_before = {e.keyword for e in entries}
        n_unmarked_before = len(keywords_before)

        try:
            comparator_cli.run_pipeline(
                yang_old=old_filename,
                yang_new=new_filename,
                dir_old=old_dir,
                dir_new=new_dir,
                out_json=str(output_json),
                compatibility_flag=compat_flag,
                # Pass xml_rules_path explicitly so run_pipeline() can re-initialize
                # CONSTRAINT_KEYWORDS even after YANG_XML_RULES was cleared from the
                # environment by the pyang plugin's finally block after the first run.
                xml_rules_path=str(xml_rules) if xml_rules else None,
            )
            # Report how many were reclassified.
            # Compare keyword SETS (not raw counts) so the numbers are accurate:
            #   - "Now classified" = keywords that were unmarked before but are
            #     no longer uncovered after the rerun (set difference).
            #   - "Still unmarked" = keywords still uncovered after the rerun.
            #   - "New uncovered"  = keywords newly surfaced by the rerun that
            #     were not in the original unmarked set.
            try:
                from yang_rag.rag.statement_extractor import collect_uncovered_statements
                rerun_entries = collect_uncovered_statements(
                    report_path=output_json,
                    xml_rules_path=xml_rules,
                    search_dirs=search_dirs,
                )
                keywords_after = {e.keyword for e in rerun_entries}
                newly_classified = len(keywords_before - keywords_after)
                still_unmarked = len(keywords_before & keywords_after)
                net_new = len(keywords_after - keywords_before)
                print(f"\n📊 Phase 2 rerun results:")
                print(f"   Previously unmarked : {n_unmarked_before}")
                print(f"   Now classified      : {newly_classified}")
                print(f"   Still unmarked      : {still_unmarked}")
                if net_new > 0:
                    print(f"   ⚠ New uncovered     : +{net_new} (newly surfaced by rerun)")
            except Exception:
                pass
        except Exception as exc:
            print(f"\n⚠️  Rerun failed: {exc}")
            return False

        return True
