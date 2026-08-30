"""
Pyang plugin for YANG backward compatibility checking.

This plugin integrates the YANG-RAG compatibility pipeline into pyang,
following the standard pyang check pattern.

Usage:
    pyang --check-compatibility \
          --old-version test_files/example-old.yang \
          --new-version test_files/example-new.yang \
          --old-dir test_files \
          --new-dir test_files

Features:
    - RAG-based similarity matching for unknown statements
    - Self-learning rule expansion
    - Detailed compatibility reports
    - Interactive or auto-approve mode

Environment:
    YANG_RAG_PROJECT_ROOT - Path to yang-rag-diff-tool project (auto-set during install)
"""
import sys
import os
from pathlib import Path
import optparse


def _check_dependencies():
    """
    Check if required dependencies are installed.
    
    Returns:
        tuple: (bool, list) - (all_installed, missing_packages)
    """
    required_packages = [
        ('sentence_transformers', 'sentence-transformers'),
        ('chromadb', 'chromadb'),
        ('torch', 'torch'),
        ('dspy', 'dspy-ai'),
        ('lxml', 'lxml'),
        ('rich', 'rich'),
    ]
    
    missing = []
    for module_name, package_name in required_packages:
        try:
            __import__(module_name)
        except ImportError:
            missing.append(package_name)
    
    return len(missing) == 0, missing


# Try to import yang_rag to ensure it's available
try:
    import yang_rag
except ImportError:
    # If not installed, try to add the parent directory (project root) to path
    # This supports running from source without installation
    current_file = Path(__file__)
    # Structure: root/yang_rag/pyang_plugin/check_compatibility.py
    # We want 'root' in path
    project_root = current_file.parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from pyang import plugin


class _StepError(Exception):
    """Raised when a pipeline step exits with a non-zero code."""
    def __init__(self, returncode: int):
        super().__init__(f"Pipeline step exited with code {returncode}")
        self.returncode = returncode


def _run_step(cmd, cwd=None):
    """Run a subprocess pipeline step, raising _StepError on failure."""
    import subprocess
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        raise _StepError(result.returncode)


def pyang_plugin_init():
    """Register the plugin with pyang."""
    plugin.register_plugin(CheckCompatibilityPlugin())


class CheckCompatibilityPlugin(plugin.PyangPlugin):
    """Pyang plugin for YANG backward compatibility checking."""
    
    def __init__(self):
        plugin.PyangPlugin.__init__(self, name='check-compatibility')
    
    def add_opts(self, optparser):
        """Add plugin-specific command line options."""
        optlist = [
            optparse.make_option(
                "--check-compatibility",
                dest="check_compatibility",
                action="store_true",
                help="Check backward compatibility between YANG versions"
            ),
            optparse.make_option(
                "--old-version",
                dest="old_version",
                help="Old YANG module version for comparison"
            ),
            optparse.make_option(
                "--new-version",
                dest="new_version",
                help="New YANG module version for comparison"
            ),
            optparse.make_option(
                "--old-dir",
                dest="old_dir",
                help="Directory containing old module dependencies"
            ),
            optparse.make_option(
                "--new-dir",
                dest="new_dir",
                help="Directory containing new module dependencies"
            ),
            optparse.make_option(
                "--output-dir",
                dest="output_dir",
                default="output",
                help="Output directory for compatibility reports (default: output)"
            ),
            optparse.make_option(
                "--interactive",
                dest="interactive_mode",
                action="store_true",
                default=False,
                help="Enable interactive mode for reviewing changes"
            ),
            optparse.make_option(
                "--auto-approve",
                dest="auto_approve",
                action="store_true",
                default=False,
                help="Automatically approve all changes (non-interactive)"
            ),
            optparse.make_option(
                "--similarity-threshold",
                dest="similarity_threshold",
                type="float",
                default=0.7,
                help="Similarity threshold for matching (0.0-1.0, default: 0.7)"
            ),
            optparse.make_option(
                "--use-gpu",
                dest="use_gpu",
                action="store_true",
                default=False,
                help="Enable GPU acceleration (requires CUDA)"
            ),
            optparse.make_option(
                "--llm-verify",
                dest="llm_verify",
                action="store_true",
                default=False,
                help="Enable LLM verification for complex changes marked with <needs-deep-analysis>"
            ),
            optparse.make_option(
                "--llm-model",
                dest="llm_model",
                default="gpt-4o-mini",
                help="LLM model to use for verification (default: gpt-4o-mini)"
            ),
            optparse.make_option(
                "--auto-approve-llm",
                dest="auto_approve_llm",
                action="store_true",
                default=False,
                help="Automatically approve LLM verification results without prompting"
            ),
            optparse.make_option(
                "--xml-rules",
                dest="xml_rules",
                default=None,
                help=(
                    "Path to a custom compatibility_rules.xml file. "
                    "If not specified, the default rules bundled with the tool are used "
                    "(yang_rag/comparator/compatibility_rules.xml)."
                )
            ),
        ]
        
        g = optparser.add_option_group("Backward Compatibility Checking Options")
        g.add_options(optlist)
    
    def setup_ctx(self, ctx):
        """
        Setup hook called early in pyang processing.
        
        This allows us to run our compatibility check before pyang tries
        to parse any YANG files. We use this instead of post_validate_ctx
        because we don't actually need pyang's parsing - our pipeline does that.
        
        Args:
            ctx: Pyang context
        """
        # Only run if --check-compatibility is specified
        if not hasattr(ctx.opts, 'check_compatibility') or not ctx.opts.check_compatibility:
            return
        
        # Check dependencies first
        deps_ok, missing = _check_dependencies()
        if not deps_ok:
            sys.stderr.write("\nError: Missing required dependencies for YANG-RAG compatibility checker.\n\n")
            sys.stderr.write("Please install the following packages:\n")
            for pkg in missing:
                sys.stderr.write(f"  - {pkg}\n")
            sys.stderr.write("\nYou can install all requirements with:\n")
            _proj_root = Path(__file__).resolve().parent.parent.parent
            sys.stderr.write(f"  cd {_proj_root}\n")
            sys.stderr.write("  pip install -r requirements.txt\n")
            sys.stderr.write("\n")
            sys.exit(1)
        
        # Validate required options
        old_file = ctx.opts.old_version
        new_file = ctx.opts.new_version
        old_dir = ctx.opts.old_dir
        new_dir = ctx.opts.new_dir
        
        if not all([old_file, new_file, old_dir, new_dir]):
            sys.stderr.write("Error: --check-compatibility requires all of:\n")
            sys.stderr.write("  --old-version <old-yang-file>\n")
            sys.stderr.write("  --new-version <new-yang-file>\n")
            sys.stderr.write("  --old-dir <old-module-dir>\n")
            sys.stderr.write("  --new-dir <new-module-dir>\n\n")
            sys.stderr.write("Example:\n")
            sys.stderr.write("  pyang --check-compatibility \\\n")
            sys.stderr.write("        --old-version test_files/example-old.yang \\\n")
            sys.stderr.write("        --new-version test_files/example-new.yang \\\n")
            sys.stderr.write("        --old-dir test_files \\\n")
            sys.stderr.write("        --new-dir test_files\n")
            sys.exit(1)
        
        # Set GPU environment if requested
        if ctx.opts.use_gpu:
            os.environ['USE_GPU'] = '1'
            os.environ['RAG_USE_GPU'] = '1'
        
        # Run the pipeline
        try:
            print("=" * 80)
            print("YANG Backward Compatibility Check")
            print("=" * 80)
            print()
            print(f"Old version: {old_file}")
            print(f"New version: {new_file}")
            print(f"Old directory: {old_dir}")
            print(f"New directory: {new_dir}")
            print(f"Output directory: {ctx.opts.output_dir}")
            print(f"LLM verification: {ctx.opts.llm_verify}")
            if ctx.opts.llm_verify:
                print(f"LLM model: {ctx.opts.llm_model}")
            if ctx.opts.xml_rules:
                print(f"Custom XML rules: {ctx.opts.xml_rules}")
            print("=" * 80)
            print()

            # Locate the project root (two levels above this file's directory)
            proj_root = Path(__file__).resolve().parent.parent.parent

            # Step 1 — core comparator
            print("[1/6] Running YANG node comparator...")
            _run_step(
                [sys.executable, "-m", "yang_rag.comparator.yang_comparator",
                 old_file, new_file, old_dir, new_dir],
                cwd=str(proj_root)
            )

            # Step 2 — filter
            print("[2/6] Filtering report...")
            _run_step(
                [sys.executable, "yang_rag/comparator/filter_report.py"],
                cwd=str(proj_root)
            )

            # Step 3 — check_compatibility (enrichment with XPath resolution)
            old_yang_path = str(Path(old_dir) / old_file)
            new_yang_path = str(Path(new_dir) / new_file)
            print("[3/6] Enriching report with compatibility rules...")
            enrich_cmd = [
                sys.executable, "yang_rag/comparator/check_compatibility.py",
                old_yang_path, new_yang_path, old_dir, new_dir,
            ]
            if ctx.opts.xml_rules:
                enrich_cmd += ["--xml-rules", str(ctx.opts.xml_rules)]
            # Pass --llm-verify so the enricher marks when/must/pattern/leafref changes
            # with <needs-deep-analysis> for the LLM second-layer verification pass.
            if ctx.opts.llm_verify:
                enrich_cmd += ["--llm-verify"]
            _run_step(enrich_cmd, cwd=str(proj_root))

            # Step 4 — generate lists
            print("[4/6] Generating compatibility lists...")
            _run_step(
                [sys.executable, "yang_rag/comparator/generate_compatibility_list.py"],
                cwd=str(proj_root)
            )
            _run_step(
                [sys.executable, "yang_rag/comparator/generate_non_compatibility_list.py"],
                cwd=str(proj_root)
            )

            # Step 5 — group by path → final_report.json
            print("[5/6] Grouping changes by path...")
            _run_step(
                [sys.executable, "yang_rag/comparator/group_by_path.py",
                 "--out", "output/final_report.json"],
                cwd=str(proj_root)
            )

            # Step 6 — deduplicate
            print("[6/6] Removing duplicate changes...")
            _run_step(
                [sys.executable, "yang_rag/comparator/group_duplicate_changes.py",
                 "--report", "output/final_report.json"],
                cwd=str(proj_root)
            )

            print("\n✅ Compatibility check completed successfully!")

            # Optional LLM verification pass
            if ctx.opts.llm_verify:
                print()
                print("=" * 80)
                print("LLM Verification Pass")
                print(f"Model: {ctx.opts.llm_model}")
                print("=" * 80)
                enriched_path = str(proj_root / "output" / "enriched_report.txt")
                llm_output_dir = str(proj_root / "output")

                from yang_rag.dspy.llm_verification_hook import verify_enriched_report
                llm_result = verify_enriched_report(
                    enriched_report_path=enriched_path,
                    output_dir=llm_output_dir,
                    model_name=ctx.opts.llm_model,
                    xml_rules_path=ctx.opts.xml_rules,
                )

                if llm_result.get('success'):
                    stats = llm_result.get('stats', {})
                    print("\n✅ LLM verification done.")
                    print(f"   Verified : {stats.get('verified', 0)}")
                    print(f"   Confirmed: {stats.get('confirmed', 0)}")
                    print(f"   Conflicts: {stats.get('conflicts', 0)}")
                    vpath = llm_result.get('verified_report_path', '')
                    if vpath:
                        print(f"   Verified report: {vpath}")
                else:
                    err = llm_result.get('error', 'unknown error')
                    sys.stderr.write(f"\n⚠  LLM verification failed: {err}\n")
                    if 'traceback' in llm_result:
                        sys.stderr.write(llm_result['traceback'])

            print("\nReports in: output/")
            print("  - enriched_report.txt : Detailed analysis")
            print("  - compatible_list.txt : Backward-compatible changes")
            print("  - non_compatible_list.txt : Breaking changes")
            print("  - final_report.json   : Machine-readable JSON report")

        except _StepError as e:
            sys.stderr.write(f"\n❌ Pipeline step failed (exit {e.returncode})\n")
            sys.exit(e.returncode)
        except Exception as e:  # noqa: BLE001
            import traceback
            sys.stderr.write(f"Error during compatibility analysis: {e}\n")
            traceback.print_exc()
            sys.exit(1)
        
        # Exit successfully - we don't need pyang to parse anything else
        sys.exit(0)
