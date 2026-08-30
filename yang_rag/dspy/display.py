"""
Display Utilities for Rule Generation Results
----------------------------------------------
Pretty-print rule generation results with Rich formatting.
"""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from .pipeline import RuleGenerationResult

console = Console()


def display_generation_result(result: RuleGenerationResult):
    """Display the generated rule(s) in a nice format.
    
    Args:
        result: RuleGenerationResult from pipeline
    """
    
    console.print("\n" + "="*70)
    console.print("[bold green]✅ Rule Generation Complete[/bold green]")
    console.print("="*70 + "\n")
    
    # Summary table
    table = Table(title="Generation Summary", show_header=True, header_style="bold cyan")
    table.add_column("Property", style="yellow")
    table.add_column("Value", style="white")
    
    table.add_row("Unknown Keyword", result.unknown_keyword)
    table.add_row("Similar Keyword", result.similar_keyword)
    table.add_row("Similarity Score", f"{result.similarity_score:.1%}")
    table.add_row("Category", result.category)
    table.add_row("Rules Generated", str(result.rules_count))
    table.add_row("Based on Rules", f"{len(result.existing_rules)} existing patterns")
    table.add_row("Confidence", f"{result.confidence:.1%}")
    table.add_row("Valid XML", "✓ Yes" if result.is_valid else "✗ No")
    
    console.print(table)
    
    # Rationale
    console.print("\n[bold cyan]💡 Rationale:[/bold cyan]")
    console.print(Panel(result.rationale, border_style="cyan"))
    
    # Generated XML
    if result.rules_count > 1:
        console.print(f"\n[bold cyan]📄 Generated XML Rules ({result.rules_count} rules):[/bold cyan]")
    else:
        console.print("\n[bold cyan]📄 Generated XML Rule:[/bold cyan]")
    console.print(Panel(result.generated_xml, border_style="green", title="XML"))
    
    # Explanation
    console.print("\n[bold cyan]📖 Plain English Explanation:[/bold cyan]")
    console.print(Panel(result.explanation, border_style="blue"))
    
    # Validation
    if not result.is_valid:
        console.print("\n[bold yellow]⚠️  Validation Issues:[/bold yellow]")
        console.print(Panel(result.validation_issues, border_style="yellow"))
    else:
        console.print("\n[green]✓ XML validation passed[/green]")
    
    # Reference rules
    if result.existing_rules:
        console.print(f"\n[bold cyan]🔗 Based on {len(result.existing_rules)} existing rule(s):[/bold cyan]")
        for i, rule in enumerate(result.existing_rules[:3], 1):
            console.print(f"  {i}. {rule['rule_id']} ({rule['compatible']})")
