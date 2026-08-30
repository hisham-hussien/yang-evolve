#!/usr/bin/env python3
import sys
import os
from pathlib import Path
try:
    from pyang.scripts import pyang_tool as pyang_script
except ImportError:
    # Fallback or try different path if version differs
    try:
        from pyang.scripts import pyang as pyang_script
    except ImportError:
        # Last resort: just try importing pyang and assume we are running via wrapper that calls it?
        # No, we need to invoke it.
        # Check standard installation entry point
        # For now assume pyang_tool based on file listing.
        import pyang
        # Maybe pyang package itself exposes it?
        raise ImportError("Could not find pyang script entry point")

import yang_rag.pyang_plugin

def main():
    """
    Wrapper entry point for the YANG RAG Compatibility Tool.
    Invokes pyang with our plugin enabled.
    """
    try:
        # Find the directory containing our plugin
        # yang_rag.pyang_plugin is a package (directory containing __init__.py)
        # or it might be just the module if we handled it differently, but we made it a package.
        plugin_dir = str(Path(yang_rag.pyang_plugin.__file__).parent)
        
        # Inject our arguments
        # We perform a simple argument injection to ensure our plugin is loaded
        # and enabled by default.
        
        args = sys.argv[1:]
        
        # Arguments to inject
        extra_args = ["--plugindir", plugin_dir]
        
        # If the user hasn't specified an action that conflicts (like --version),
        # we default to enabling our compatibility check.
        # But pyang expects input files.
        if "--check-compatibility" not in args and not any(x in args for x in ["--help", "-h", "--version", "-v"]):
            extra_args.append("--check-compatibility")
            
        # Update sys.argv
        sys.argv = [sys.argv[0]] + extra_args + args
        
        # Set environment variable for the plugin to find the root if needed
        # Since we are installed as a package, we might rely on the plugin finding 'yang_rag' via import.
        # But the plugin code we saw tries to find PROJECT_ROOT. We should update that code later.
        # For now, let's proceed.

        # Run pyang
        pyang_script.run()
        
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception as e:
        sys.stderr.write(f"Error running yang-rag-check: {e}\n")
        sys.exit(1)

if __name__ == '__main__':
    main()
