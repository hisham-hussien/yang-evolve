#!/usr/bin/env python3
"""
Simple Rule Updater CLI
-----------------------
Simplified DSPy-based rule updater that:
1. Uses RAG to find similar keywords
2. Clones similar keyword lines in XML
3. Inserts new keyword underneath

No complex rule generation - just smart cloning based on semantic similarity.
"""

import sys
import argparse
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Add project root to path
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

# Use new YANG-RAG adapter instead of old identify_yang_keyword
from yang_rag.rag.yang_rag_adapter import identify_keyword
from yang_rag.dspy.xml_rule_updater import update_rules_from_rag, XMLRuleUpdater

console = Console()

CONTEXT_WINDOW = 5  # lines above/below the target line to include in snippet


def _extract_yang_snippet(yang_file: str, line_number: int | None, keyword: str, window: int = CONTEXT_WINDOW) -> str:
    """Extract a YANG code snippet around the usage location of a keyword.

    Args:
        yang_file:   Path to the YANG file.
        line_number: 1-based line number of the keyword usage (None → search by keyword name).
        keyword:     The extension keyword name (used as fallback search term).
        window:      Number of context lines above/below the target line.

    Returns:
        A multi-line string with the surrounding YANG code, or just ``keyword;`` on failure.
    """
    try:
        path = Path(yang_file)
        if not path.exists():
            return f"{keyword};"
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

        # Determine target line (1-based → 0-based index)
        if line_number and 1 <= line_number <= len(lines):
            target_idx = line_number - 1
        else:
            # Search for first occurrence of the keyword in the file
            target_idx = next(
                (i for i, ln in enumerate(lines) if keyword in ln),
                None
            )
            if target_idx is None:
                return f"{keyword};"

        start = max(0, target_idx - window)
        end = min(len(lines), target_idx + window + 1)
        snippet_lines = lines[start:end]
        return "\n".join(snippet_lines)
    except Exception:
        return f"{keyword};"


def main():
    parser = argparse.ArgumentParser(
        description="Update compatibility_rules.xml by cloning similar keyword rules",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview what would change
  python simple_rule_updater_cli.py "oc-ext:posix-pattern '^test$';" --preview
  
  # Update the XML file
  python simple_rule_updater_cli.py "oc-ext:posix-pattern '^test$';"
  
  # Use a specific similar keyword (skip RAG)
  python simple_rule_updater_cli.py "new-constraint" --similar pattern --preview
  
  # Specify custom XML path
  python simple_rule_updater_cli.py "new-keyword" --xml path/to/rules.xml
        """
    )
    
    parser.add_argument(
        'yang_structure',
        help='YANG structure with unknown keyword (e.g., "oc-ext:posix-pattern \'^\';")' 
    )
    
    parser.add_argument(
        '--xml',
        default='yang_rag/comparator/compatibility_rules.xml',
        help='Path to compatibility_rules.xml (default: yang_rag/comparator/compatibility_rules.xml)'
    )
    
    parser.add_argument(
        '--similar',
        help='Override RAG and use this specific similar keyword'
    )
    
    parser.add_argument(
        '--preview',
        action='store_true',
        help='Preview changes without saving'
    )
    
    parser.add_argument(
        '--no-backup',
        action='store_true',
        help='Do not create backup file before saving'
    )
    
    parser.add_argument(
        '--n-results',
        type=int,
        default=5,
        help='Number of RAG results to retrieve (default: 5)'
    )
    
    parser.add_argument(
        '--min-similarity',
        type=float,
        default=0.0,
        help='Minimum similarity threshold 0.0-1.0 (default: 0.0)'
    )
    
    parser.add_argument(
        '--summary',
        action='store_true',
        help='Show summary of existing rules and exit'
    )

    parser.add_argument(
        '--yang-file',
        type=str,
        default=None,
        help='Path to the YANG file containing the unknown keyword (used for accurate skeleton path)'
    )

    parser.add_argument(
        '--yang-context',
        type=str,
        default=None,
        help='Pre-built YANG code snippet to use as RAG query instead of yang_structure'
    )

    parser.add_argument(
        '--line-number',
        type=int,
        default=None,
        help='Line number in the YANG file where the keyword appears (used to extract context snippet)'
    )

    args = parser.parse_args()

    # Resolve XML path — support both absolute paths and paths relative to project root
    _xml_arg = Path(args.xml)
    if _xml_arg.is_absolute():
        xml_path = _xml_arg
    else:
        xml_path = Path(project_root) / args.xml
    if not xml_path.exists():
        console.print(f"[red]❌ XML file not found: {xml_path}[/red]")
        sys.exit(1)

    # Show summary if requested
    if args.summary:
        show_summary(xml_path)
        sys.exit(0)

    # Extract unknown keyword from YANG structure
    unknown_keyword = args.yang_structure.split()[0].split(':')[-1] if ':' in args.yang_structure else args.yang_structure.split()[0]
    unknown_keyword = unknown_keyword.strip(';').strip()

    # Build the RAG query context:
    # Priority: --yang-context > extract from --yang-file + --line-number > yang_structure
    yang_query = args.yang_structure  # fallback
    context_source = "argument"

    if args.yang_context:
        yang_query = args.yang_context
        context_source = "provided context"
    elif args.yang_file and args.line_number:
        yang_query = _extract_yang_snippet(args.yang_file, args.line_number, unknown_keyword)
        context_source = f"{Path(args.yang_file).name}:{args.line_number}"
    elif args.yang_file:
        yang_query = _extract_yang_snippet(args.yang_file, None, unknown_keyword)
        context_source = f"{Path(args.yang_file).name} (keyword search)"

    console.print(Panel.fit(
        f"[bold cyan]XML Rule Updater[/bold cyan]\n\n"
        f"Unknown keyword: [bold]{unknown_keyword}[/bold]\n"
        f"Context source:  [dim]{context_source}[/dim]\n"
        f"XML file: {xml_path}",
        border_style="cyan"
    ))

    # Step 1: Find similar keyword (or use override)
    if args.similar:
        console.print(f"\n[yellow]Using specified similar keyword:[/yellow] [bold]{args.similar}[/bold]")
        similar_keyword = args.similar
        similarity_score = 1.0
    else:
        console.print(f"\n[yellow]Step 1/2:[/yellow] Running RAG semantic search...")
        if len(yang_query) > 50:
            console.print(Panel(
                yang_query[:300] + ('...' if len(yang_query) > 300 else ''),
                title="[cyan]RAG Search Query[/cyan]",
                border_style="dim cyan",
                padding=(0, 1)
            ))

        rag_result = identify_keyword(
            yang_query,
            n_results=args.n_results,
            yang_file=args.yang_file,
        )
        
        if not rag_result or not rag_result.get('examples'):
            console.print("[red]❌ No similar keywords found in RAG[/red]")
            sys.exit(1)
        
        similar_keyword = rag_result['keyword']
        top_match = rag_result['examples'][0]
        # Support both 'similarity' (new adapter) and legacy 'distance' field
        if 'similarity' in top_match:
            similarity_score = top_match['similarity']
        else:
            similarity_score = 1 - top_match.get('distance', 0.0)
        
        # Show top results
        candidates = rag_result['examples'][:args.n_results]
        table = Table(
            title=f"Top Similar Keywords for [bold magenta]{unknown_keyword}[/bold magenta]",
            show_header=True, header_style="bold cyan", show_lines=False
        )
        table.add_column("#", style="dim", width=3, no_wrap=True)
        table.add_column("Keyword", style="magenta", width=20, no_wrap=True)
        table.add_column("Occurrences", justify="center", width=11, no_wrap=True)
        table.add_column("Sim%", justify="right", width=6, no_wrap=True)
        table.add_column("Type", style="yellow", width=12, no_wrap=True)
        table.add_column("Scores", style="dim", no_wrap=False)

        for i, example in enumerate(candidates, 1):
            if 'similarity' in example:
                sim = example['similarity']
            else:
                sim = 1 - example.get('distance', 0.0)

            sim_color = "green" if sim >= 0.8 else "yellow" if sim >= 0.6 else "red"
            occ = example.get('occurrence_count', '?')
            occ_color = "green" if isinstance(occ, int) and occ >= 10 else "yellow" if isinstance(occ, int) and occ >= 5 else "white"

            # Category abbreviation
            category = example.get('type', 'N/A')
            if 'ATTRIBUTE, CONSTRAINT' in category:
                cat_abbr = "Attr+Cons"
            elif 'ATTRIBUTE' in category:
                cat_abbr = "Attr"
            elif 'CONSTRAINT' in category:
                cat_abbr = "Cons"
            elif 'STRUCTURAL' in category:
                cat_abbr = "Struct"
            elif 'EXTENSION' in category:
                cat_abbr = "Ext"
            else:
                cat_abbr = category[:12]

            # Score breakdown
            sc = example.get('score_components')
            if sc and isinstance(sc, dict):
                scores_dict = {
                    'st': sc.get('structure_sim', 0),
                    'ds': sc.get('description_sim', 0),
                    'ph': sc.get('path_shape', 0),
                    'kw': sc.get('keyword_overlap', 0),
                    'ct': sc.get('category_match', 0),
                    'nb': sc.get('neighbor_boost', 0),
                    'lx': sc.get('lexical_overlap', 0),
                    'tk': sc.get('token_score', sc.get('token_match', 0)),
                }
                all_scores = sorted(
                    [(k, v) for k, v in scores_dict.items() if v > 0.001],
                    key=lambda x: x[1], reverse=True
                )
                score_str = ' '.join(f"{k}={v:.2f}" for k, v in all_scores) if all_scores else "—"
            else:
                score_str = "—"

            table.add_row(
                str(i),
                example.get('keyword', 'N/A')[:20],
                f"[{occ_color}]{occ}[/{occ_color}]",
                f"[{sim_color}]{sim*100:.0f}[/{sim_color}]",
                cat_abbr,
                score_str,
            )

        console.print(table)
        console.print("[dim]Weights: st=35% ds=5% ph=10% kw=8% ct=10% nb=7% lx=5% bm25=5% tk=15%[/dim]")
        console.print("[dim]Scores: structure(st) description(ds) path(ph) keyword(kw) category(ct) neighbor(nb) lexical(lx) token(tk)[/dim]")

        # Show the YANG context snippet used for the query
        console.print(f"\n[cyan]Statement context (RAG query):[/cyan]")
        if len(yang_query) > 50:
            console.print(Panel(
                yang_query[:400] + ('...' if len(yang_query) > 400 else ''),
                border_style="dim cyan",
                padding=(0, 1)
            ))
        else:
            console.print(f"  [bold]{yang_query}[/bold]")

        # Check similarity threshold
        if args.min_similarity > 0.0 and similarity_score < args.min_similarity:
            console.print(f"\n[red]❌ Similarity {similarity_score:.1%} is below threshold {args.min_similarity:.1%}[/red]")
            console.print(f"[yellow]Suggestion:[/yellow] Lower threshold with --min-similarity {max(0.3, args.min_similarity - 0.2):.1f}")
            sys.exit(1)

        # ── Interactive selection ──────────────────────────────────────────
        console.print(
            f"\n[bold]Select the similar keyword to clone rules from:[/bold]\n"
            f"  • Enter a number [cyan]1–{len(candidates)}[/cyan] to pick a candidate\n"
            f"  • Press [cyan]Enter[/cyan] to accept the top suggestion "
            f"([bold]{similar_keyword}[/bold], {similarity_score:.1%})\n"
            f"  • Type a [cyan]custom keyword[/cyan] to use it directly\n"
            f"  • Type [cyan]s[/cyan] or [cyan]skip[/cyan] to skip this keyword"
        )

        try:
            raw = input("  Your choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Skipped (interrupted).[/yellow]")
            sys.exit(2)  # exit 2 = user skipped (not an error)

        if raw.lower() in ("s", "skip", "q", "quit"):
            console.print("[yellow]Skipped.[/yellow]")
            sys.exit(2)  # exit 2 = user skipped (not an error)
        elif raw == "":
            # Accept top suggestion — already set above
            pass
        elif raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(candidates):
                chosen = candidates[idx]
                similar_keyword = chosen.get('keyword', similar_keyword)
                if 'similarity' in chosen:
                    similarity_score = chosen['similarity']
                else:
                    similarity_score = 1 - chosen.get('distance', 0.0)
            else:
                console.print(f"[red]Invalid number '{raw}'. Accepting top suggestion.[/red]")
        else:
            # Treat as a custom keyword typed by the user
            similar_keyword = raw
            similarity_score = 1.0
            console.print(f"[yellow]Using custom keyword:[/yellow] [bold]{similar_keyword}[/bold]")

        console.print(f"\n[green]✓[/green] Using similar keyword: [bold]{similar_keyword}[/bold] (similarity: {similarity_score:.1%})")
    
    # Step 2: Update XML
    console.print(f"\n[yellow]Step 2/2:[/yellow] Updating XML rules...")
    
    success, message = update_rules_from_rag(
        xml_path=str(xml_path),
        new_keyword=unknown_keyword,
        similar_keyword=similar_keyword,
        preview_only=args.preview,
        backup=not args.no_backup
    )
    
    # Show result
    console.print()
    if success:
        console.print(Panel.fit(
            f"[bold green]✓ Success[/bold green]\n\n{message}",
            border_style="green"
        ))
        
        if not args.preview:
            console.print(f"\n[dim]Updated file: {xml_path}[/dim]")
            if not args.no_backup:
                console.print(f"[dim]Backup file: {xml_path.with_suffix('.xml.bak')}[/dim]")
    else:
        console.print(Panel.fit(
            f"[bold red]✗ Failed[/bold red]\n\n{message}",
            border_style="red"
        ))
        sys.exit(1)


def show_summary(xml_path: Path):
    """Show summary of existing rules in the XML file."""
    try:
        updater = XMLRuleUpdater(str(xml_path))
        summary = updater.get_rules_summary()
        
        console.print(Panel.fit(
            f"[bold cyan]Rules Summary[/bold cyan]\n\n"
            f"File: {xml_path}\n"
            f"Total Rules: {summary['total_rules']}",
            border_style="cyan"
        ))
        
        # Keywords
        if summary['keywords']:
            console.print("\n[bold yellow]Keywords:[/bold yellow]")
            for kw in summary['keywords']:
                console.print(f"  • {kw}")
        
        # Constraints
        if summary['constraints']:
            console.print("\n[bold yellow]Constraints:[/bold yellow]")
            for c in summary['constraints']:
                console.print(f"  • {c}")
        
        # Attributes
        if summary['attributes']:
            console.print("\n[bold yellow]Attributes:[/bold yellow]")
            for a in summary['attributes']:
                console.print(f"  • {a}")
        
        console.print(f"\n[dim]Total unique items: {len(summary['keywords']) + len(summary['constraints']) + len(summary['attributes'])}[/dim]")
        
    except Exception as e:
        console.print(f"[red]❌ Error: {e}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
