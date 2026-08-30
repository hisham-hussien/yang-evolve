#!/usr/bin/env python3
"""
DSPy-based Rule Generator for Unknown YANG Keywords - CLI
----------------------------------------------------------
Command-line interface for generating XML compatibility rules.

This is the main entry point that coordinates all DSPy components:
- signatures.py: DSPy input/output signatures
- modules.py: DSPy reasoning chains
- api_config.py: FuelIX API configuration
- xml_utils.py: XML rule extraction
- pipeline.py: Main generation pipeline
- display.py: Result formatting

Usage:
    python dspy_rule_generator.py --text 'oc-ext:posix-pattern "regex";'
    python dspy_rule_generator.py --text 'pr:privileges "read";' --similarity 0.7
"""

import sys
import argparse
from rich.console import Console

from .pipeline import generate_rule_for_unknown_keyword
from .display import display_generation_result
from .api_config import FUELIX_API_BASE, FUELIX_DEFAULT_MODEL

console = Console()


def main():
    """Main entry point for CLI."""
    
    parser = argparse.ArgumentParser(
        description="Generate XML compatibility rules for unknown YANG keywords using DSPy + RAG via FuelIX API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate rules for unknown keyword
  python -m code.dspy.cli --text 'oc-ext:posix-pattern "^[0-9]+$";'
  
  # With similarity threshold
  python -m code.dspy.cli --text 'pr:privileges "read write";' --similarity 0.7
  
  # With specific model
  python -m code.dspy.cli --text 'ep:endpoint "/api";' --model gpt-4
  
Environment:
  Set FUELIX_API_KEY environment variable before running
        """
    )
    
    parser.add_argument(
        '--text',
        type=str,
        help='YANG structure with unknown keyword (e.g., "oc-ext:posix-pattern \'regex\';")'
    )
    parser.add_argument(
        '--xml',
        type=str,
        default='yang_rag/comparator/compatibility_rules.xml',
        help='Path to compatibility_rules.xml (default: yang_rag/comparator/compatibility_rules.xml)'
    )
    parser.add_argument(
        '--model',
        type=str,
        default=FUELIX_DEFAULT_MODEL,
        help=f'LLM model name for FuelIX (default: {FUELIX_DEFAULT_MODEL}). FuelIX supports OpenAI-compatible models.'
    )
    parser.add_argument(
        '--top',
        type=int,
        default=5,
        help='Number of similar examples to retrieve from RAG (default: 5)'
    )
    parser.add_argument(
        '--similarity',
        type=float,
        default=0.0,
        help='Minimum similarity threshold (0.0-1.0). Only generate rules if similarity >= threshold (default: 0.0, no filtering)'
    )
    parser.add_argument(
        '--api-base',
        type=str,
        default=FUELIX_API_BASE,
        help=f'FuelIX API base URL (default: {FUELIX_API_BASE})'
    )
    parser.add_argument(
        '--single',
        action='store_true',
        help='Generate only single rule instead of all rules (default: generate all)'
    )
    
    args = parser.parse_args()
    
    if not args.text:
        # Demo mode with examples
        console.print("[bold cyan]Running demo with example unknown keywords...[/bold cyan]\n")
        console.print("[dim]Set --text to provide your own YANG structure[/dim]\n")
        
        examples = [
            "oc-ext:posix-pattern '^[0-9A-Fa-f]{2}(\\.[0-9A-Fa-f]{4}){0,3}$';",
            'ep:endpoint "/ip_net_to_media_table2";',
            'pr:privileges "create delete";'
        ]
        
        for example in examples:
            console.print(f"\n[yellow]{'='*70}[/yellow]")
            console.print(f"[yellow]Processing: {example}[/yellow]")
            console.print(f"[yellow]{'='*70}[/yellow]")
            
            result = generate_rule_for_unknown_keyword(
                example,
                compatibility_xml_path=args.xml,
                llm_model=args.model,
                n_rag_results=args.top,
                api_base=args.api_base,
                generate_all=not args.single,
                min_similarity=args.similarity
            )
            
            if result:
                display_generation_result(result)
            
            console.print("\n[dim]Press Enter to continue...[/dim]")
            input()
    else:
        # Single query mode
        result = generate_rule_for_unknown_keyword(
            args.text,
            compatibility_xml_path=args.xml,
            llm_model=args.model,
            n_rag_results=args.top,
            api_base=args.api_base,
            generate_all=not args.single,
            min_similarity=args.similarity
        )
        
        if result:
            display_generation_result(result)
        else:
            console.print("[red]Failed to generate rule[/red]")
            sys.exit(1)


if __name__ == "__main__":
    main()
