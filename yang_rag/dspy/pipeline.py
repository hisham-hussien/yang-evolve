"""
Rule Generation Pipeline
------------------------
Main pipeline that coordinates RAG → XML extraction → DSPy generation.
"""

import sys
from pathlib import Path
from typing import Optional
from dataclasses import dataclass
from rich.console import Console

# Add project root to path
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

# Use new YANG-RAG adapter instead of old identify_yang_keyword
from yang_rag.rag.yang_rag_adapter import identify_keyword
from .api_config import configure_dspy, FUELIX_API_BASE, FUELIX_DEFAULT_MODEL
from .xml_utils import extract_rules_for_keyword, format_rules_as_text
from .modules import RuleGeneratorModule

console = Console()


@dataclass
class RuleGenerationResult:
    """Result of rule generation process."""
    unknown_keyword: str
    similar_keyword: str
    similarity_score: float
    category: str
    generated_xml: str
    confidence: float
    rationale: str
    is_valid: bool
    validation_issues: str
    explanation: str
    existing_rules: list
    rules_count: int


def generate_rule_for_unknown_keyword(
    unknown_yang_structure: str,
    compatibility_xml_path: str = "yang_rag/comparator/compatibility_rules.xml",
    llm_model: str = FUELIX_DEFAULT_MODEL,
    n_rag_results: int = 5,
    api_base: str = FUELIX_API_BASE,
    generate_all: bool = True,
    min_similarity: float = 0.0,
    override_keyword: Optional[str] = None,
    yang_file: Optional[str] = None,
) -> Optional[RuleGenerationResult]:
    """Complete pipeline: RAG retrieval → Rule extraction → DSPy generation via FuelIX.
    
    Args:
        unknown_yang_structure: YANG syntax with unknown keyword (e.g., "oc-ext:posix-pattern '^...$';")
        compatibility_xml_path: Path to compatibility_rules.xml (default: yang_rag/comparator/compatibility_rules.xml)
        llm_model: LLM model name for FuelIX (e.g., 'gpt-4o-mini', 'gpt-4', etc.)
        n_rag_results: Number of similar examples to retrieve from RAG
        api_base: FuelIX API base URL (default: https://api.fuelix.ai/v1)
        generate_all: If True, generate rules for ALL existing patterns. If False, generate single rule.
        min_similarity: Minimum similarity threshold (0.0-1.0). Only generate if similarity >= threshold.
        override_keyword: If provided, skip RAG and use this keyword directly (for user-selected keywords)
    
    Returns:
        RuleGenerationResult with generated rule(s) and metadata, or None if failed
    """
    
    console.print("\n[bold cyan]🚀 Starting Rule Generation Pipeline[/bold cyan]\n")
    
    # ========================================================================
    # Step 1: RAG - Find similar keywords (or use override)
    # ========================================================================
    if override_keyword:
        console.print(f"[yellow]Step 1/4:[/yellow] Using user-selected keyword: [bold]{override_keyword}[/bold]")
        similar_keyword = override_keyword
        similarity_score = 1.0  # User-selected, so maximum confidence
        category = "user-selected"
        console.print(f"[green]✓[/green] Using keyword: [bold]{similar_keyword}[/bold]")
    else:
        console.print("[yellow]Step 1/4:[/yellow] RAG semantic search for similar keywords...")
        rag_result = identify_keyword(unknown_yang_structure, n_results=n_rag_results,
                                      min_similarity=0.0, yang_file=yang_file)
        
        if not rag_result or not rag_result.get('examples'):
            console.print("[red]❌ No similar keywords found in RAG[/red]")
            return None
        
        similar_keyword = rag_result['keyword']
        top_match = rag_result['examples'][0]
        similarity_score = top_match.get('similarity')
        if similarity_score is None:
            # Backward compatibility with legacy adapter payloads.
            similarity_score = 1 - top_match.get('distance', 0.0)
        category = top_match.get('type', top_match.get('category', 'unknown'))
        
        console.print(f"[green]✓[/green] Found similar keyword: [bold]{similar_keyword}[/bold]")
        console.print(f"  Similarity: {similarity_score:.1%}, Type: {category}")
        
        # Check similarity threshold (only if not user-override)
        if min_similarity > 0.0 and similarity_score < min_similarity:
            unknown_keyword = unknown_yang_structure.split()[0].split(':')[-1] if ':' in unknown_yang_structure else unknown_yang_structure.split()[0]
            
            console.print(f"\n[red]❌ Similarity {similarity_score:.1%} is below threshold {min_similarity:.1%}[/red]")
            console.print(f"[yellow]The system has no answer for unknown keyword '[bold]{unknown_keyword}[/bold]'[/yellow]")
            console.print(f"[dim]Best match: '{similar_keyword}' with {similarity_score:.1%} similarity[/dim]\n")
            console.print("[cyan]💡 Suggestions:[/cyan]")
            console.print(f"  • Lower the threshold: [bold]--similarity {max(0.3, min_similarity - 0.2):.1f}[/bold]")
            console.print("  • Provide more context in the YANG structure")
            console.print("  • Check for spelling variations of the keyword")
            console.print("  • Consult a YANG expert for manual analysis")
            
            return None
    
    # ========================================================================
    # Step 2: Extract existing XML rules
    # ========================================================================
    console.print("\n[yellow]Step 2/4:[/yellow] Extracting existing XML rules...")
    xml_path = Path(project_root) / compatibility_xml_path
    
    if not xml_path.exists():
        console.print(f"[red]❌ XML file not found: {xml_path}[/red]")
        return None
    
    existing_rules = extract_rules_for_keyword(xml_path, similar_keyword)
    console.print(f"[green]✓[/green] Found {len(existing_rules)} existing rules for '{similar_keyword}'")
    
    if existing_rules:
        for rule in existing_rules[:3]:  # Show first 3
            console.print(f"  • {rule['rule_id']}: {rule['compatible']}")
    
    # ========================================================================
    # Step 3: Configure DSPy with FuelIX API
    # ========================================================================
    console.print(f"\n[yellow]Step 3/4:[/yellow] Configuring DSPy with FuelIX ({llm_model})...")
    
    if not configure_dspy(model=llm_model, api_base=api_base):
        console.print("[red]❌ Failed to configure DSPy[/red]")
        console.print("[yellow]Set FUELIX_API_KEY environment variable:[/yellow]")
        console.print("[dim]  export FUELIX_API_KEY='your-api-key'[/dim]")
        return None
    
    # ========================================================================
    # Step 4: Generate rules with DSPy
    # ========================================================================
    console.print("\n[yellow]Step 4/4:[/yellow] Generating compatibility rule with DSPy...")
    
    # Extract unknown keyword name
    unknown_keyword = unknown_yang_structure.split()[0].split(':')[-1] if ':' in unknown_yang_structure else unknown_yang_structure.split()[0]
    
    # Format context
    rules_text = format_rules_as_text(existing_rules)
    
    # Create module and generate
    generator = RuleGeneratorModule()
    
    try:
        result = generator(
            unknown_keyword=unknown_keyword,
            similar_keyword=similar_keyword,
            similarity_score=similarity_score,
            category=category,
            existing_rules=rules_text,
            yang_context=unknown_yang_structure,
            generate_all=generate_all
        )
        
        if generate_all:
            console.print(f"[green]✓[/green] Generated {result.rules_count} rules successfully!")
        else:
            console.print("[green]✓[/green] Rule generated successfully!")
        
        return RuleGenerationResult(
            unknown_keyword=unknown_keyword,
            similar_keyword=similar_keyword,
            similarity_score=similarity_score,
            category=category,
            generated_xml=result.generated_xml,
            confidence=float(result.confidence),
            rationale=result.rationale,
            is_valid=result.is_valid,
            validation_issues=result.validation_issues,
            explanation=result.explanation,
            existing_rules=existing_rules,
            rules_count=result.rules_count
        )
    
    except Exception as e:
        console.print(f"[red]❌ Generation failed: {e}[/red]")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
        return None
