"""
Lightweight Pyang plugin for YANG backward compatibility checking.

This plugin provides direct comparator-only functionality without RAG/pipeline dependencies.

Usage:
    pyang --yang-check-compatibility \
          --old-version test_files/example-old.yang \
          --new-version test_files/example-new.yang \
          --old-dir test_files \
          --new-dir test_files
    
    # With RFC 7950 strict mode
    pyang --yang-check-compatibility \
          --old-version test_files/example-old.yang \
          --new-version test_files/example-new.yang \
          --old-dir test_files \
          --new-dir test_files \
          --rfc7950

Note: We use --yang-check-compatibility (not --check-compatibility) to avoid 
      conflicts with pyang's built-in option.

Features:
    - Direct comparator-based analysis (no RAG/LLM dependencies)
    - RFC 7950 strict compatibility mode support
    - Optional LLM verification for complex constraints
    - Detailed compatibility reports
    - Lightweight and fast
"""
import sys
import os
from pathlib import Path
import optparse


def _check_dependencies():
    """
    Check if required dependencies are installed (lightweight version).
    
    Returns:
        tuple: (bool, list) - (all_installed, missing_packages)
    """
    required_packages = [
        ('lxml', 'lxml'),
    ]
    
    missing = []
    for module_name, package_name in required_packages:
        try:
            __import__(module_name)
        except ImportError:
            missing.append(package_name)
    
    return len(missing) == 0, missing


def _find_project_root():
    """Find the project root directory."""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / 'yang_rag').exists():
            return str(current)
        current = current.parent
    return None


# Try to import yang_rag to ensure it's available
_yang_rag_imported = False
try:
    import yang_rag
    _yang_rag_imported = True
except ImportError:
    # If not installed, try to add the parent directory (project root) to path
    current_file = Path(__file__)
    # Structure: root/yang_rag/pyang_plugin/check_compatibility_lite.py OR
    # /site-packages/pyang/plugins/check_compatibility.py (when installed)
    # Try to find project root
    project_root = _find_project_root()
    if project_root and str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
        try:
            import yang_rag
            _yang_rag_imported = True
        except ImportError:
            pass  # Will fail later when trying to use it

from pyang import plugin

# Global flag to prevent double registration
_plugin_registered = False

def pyang_plugin_init():
    """Register the plugin with pyang."""
    global _plugin_registered
    if not _plugin_registered:
        plugin.register_plugin(CheckCompatibilityLitePlugin())
        _plugin_registered = True


class CheckCompatibilityLitePlugin(plugin.PyangPlugin):
    """Lightweight pyang plugin for YANG backward compatibility checking."""
    
    def __init__(self):
        plugin.PyangPlugin.__init__(self, name='yang-compatibility')
    
    def add_opts(self, optparser):
        """Add plugin-specific command line options."""
        optlist = [
            optparse.make_option(
                "--yang-check-compatibility",
                dest="check_compatibility",
                action="store_true",
                help="Check backward compatibility between YANG versions (lightweight comparator)"
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
                "--rfc7950",
                dest="rfc7950_mode",
                action="store_true",
                default=False,
                help="Use RFC 7950 strict compatibility mode"
            ),
            optparse.make_option(
                "--llm-verify",
                dest="llm_verify",
                action="store_true",
                default=False,
                help="Enable LLM verification for complex changes (requires dspy-ai, openai)"
            ),
            optparse.make_option(
                "--llm-model",
                dest="llm_model",
                default="gpt-4o-mini",
                help="LLM model to use for verification (default: gpt-4o-mini)"
            ),
        ]
        
        g = optparser.add_option_group("Backward Compatibility Checking Options")
        g.add_options(optlist)
    
    def setup_ctx(self, ctx):
        """
        Setup hook called early in pyang processing.
        
        Runs the comparator directly without RAG/pipeline dependencies.
        
        Args:
            ctx: Pyang context
        """
        # Only run if --check-compatibility is specified
        if not hasattr(ctx.opts, 'check_compatibility') or not ctx.opts.check_compatibility:
            return
        
        # Check dependencies first
        deps_ok, missing = _check_dependencies()
        if not deps_ok:
            sys.stderr.write("\nError: Missing required dependencies for YANG compatibility checker.\n\n")
            sys.stderr.write("Please install the following packages:\n")
            for pkg in missing:
                sys.stderr.write(f"  - {pkg}\n")
            sys.stderr.write("\nYou can install all requirements with:\n")
            sys.stderr.write(f"  pip install {' '.join(missing)}\n")
            sys.stderr.write("\n")
            sys.exit(1)
        
        # Validate required options
        old_file = ctx.opts.old_version
        new_file = ctx.opts.new_version
        old_dir = ctx.opts.old_dir
        new_dir = ctx.opts.new_dir
        
        if not all([old_file, new_file, old_dir, new_dir]):
            sys.stderr.write("Error: --yang-check-compatibility requires all of:\n")
            sys.stderr.write("  --old-version <old-yang-file>\n")
            sys.stderr.write("  --new-version <new-yang-file>\n")
            sys.stderr.write("  --old-dir <old-module-dir>\n")
            sys.stderr.write("  --new-dir <new-module-dir>\n\n")
            sys.stderr.write("Example:\n")
            sys.stderr.write("  pyang --yang-check-compatibility \\\n")
            sys.stderr.write("        --old-version test_files/example-old.yang \\\n")
            sys.stderr.write("        --new-version test_files/example-new.yang \\\n")
            sys.stderr.write("        --old-dir test_files \\\n")
            sys.stderr.write("        --new-dir test_files\n\n")
            sys.stderr.write("For RFC 7950 strict mode:\n")
            sys.stderr.write("  pyang --yang-check-compatibility ... --rfc7950\n")
            sys.exit(1)
        
        # Import and run the comparator directly
        try:
            from yang_rag.comparator import cli as comparator_cli
            
            # Check LLM dependencies if requested
            if ctx.opts.llm_verify:
                try:
                    import dspy
                    import openai
                except ImportError as e:
                    sys.stderr.write("\nError: LLM verification requires additional dependencies.\n")
                    sys.stderr.write("Please install: pip install dspy-ai openai\n\n")
                    sys.exit(1)
            
            print("=" * 80)
            print("YANG Backward Compatibility Check (Lightweight Comparator)")
            print("=" * 80)
            print()
            print(f"Old version: {old_file}")
            print(f"New version: {new_file}")
            print(f"Old directory: {old_dir}")
            print(f"New directory: {new_dir}")
            print(f"Output directory: {ctx.opts.output_dir}")
            print(f"RFC 7950 mode: {ctx.opts.rfc7950_mode}")
            if ctx.opts.llm_verify:
                print(f"LLM verification: Enabled ({ctx.opts.llm_model})")
            print("=" * 80)
            print()
            
            # Change to output directory context
            os.makedirs(ctx.opts.output_dir, exist_ok=True)
            original_dir = os.getcwd()
            
            try:
                # Run the comparator pipeline with RFC 7950 flag if specified
                # We need to pass the flag through the environment since cli doesn't expose it directly
                if ctx.opts.rfc7950_mode:
                    os.environ['YANG_COMPAT_FLAG'] = 'rfc7950'
                
                # Set LLM options if requested
                if ctx.opts.llm_verify:
                    os.environ['ENABLE_LLM_VERIFICATION'] = '1'
                    os.environ['LLM_MODEL'] = ctx.opts.llm_model
                
                result = comparator_cli.run_pipeline(
                    yang_old=old_file,
                    yang_new=new_file,
                    dir_old=old_dir,
                    dir_new=new_dir,
                    out_json=os.path.join(ctx.opts.output_dir, "final_report.json")
                )
                
                if result == 0:
                    print("\n✅ Compatibility check completed successfully!")
                    print(f"\nReports generated in: {ctx.opts.output_dir}/")
                    print("  - report.txt: Basic change summary")
                    print("  - enriched_report_llm.txt: Detailed analysis with compatibility tags")
                    if ctx.opts.llm_verify:
                        print("    (includes LLM verification for complex constraints)")
                        print("  - llm_verification_report.json: LLM analysis details")
                    print("  - compatible_list.txt: Backward-compatible changes")
                    print("  - non_compatible_list.txt: Breaking changes")
                    print("  - final_report.json: Machine-readable JSON report")
                    
                    if ctx.opts.rfc7950_mode:
                        print("\n  (RFC 7950 strict mode applied)")
                else:
                    sys.stderr.write("\n❌ Compatibility check failed\n")
                    sys.exit(1)
                    
            finally:
                os.chdir(original_dir)
                if 'YANG_COMPAT_FLAG' in os.environ:
                    del os.environ['YANG_COMPAT_FLAG']
                if 'ENABLE_LLM_VERIFICATION' in os.environ:
                    del os.environ['ENABLE_LLM_VERIFICATION']
                if 'LLM_MODEL' in os.environ:
                    del os.environ['LLM_MODEL']
            
        except ImportError as e:
            sys.stderr.write(f"Error: Could not import comparator module: {e}\n")
            sys.stderr.write("Make sure you're running pyang from the project root directory\n")
            sys.stderr.write("or that the project is properly installed.\n")
            sys.exit(1)
        except Exception as e:
            import traceback
            sys.stderr.write(f"Error during compatibility analysis: {e}\n")
            traceback.print_exc()
            sys.exit(1)
        
        # Exit successfully - we don't need pyang to parse anything else
        sys.exit(0)


def _find_project_root():
    """Find the project root directory."""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / 'yang_rag').exists() or (current / 'requirements.txt').exists():
            return str(current)
        current = current.parent
    return None
