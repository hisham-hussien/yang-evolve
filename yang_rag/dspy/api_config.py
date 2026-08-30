"""
FuelIX API Configuration
------------------------
Configure DSPy to use FuelIX OpenAI-compatible API endpoint.
"""

import os
import dspy
from rich.console import Console

console = Console()

FUELIX_API_BASE = "https://api.fuelix.ai/v1"
FUELIX_DEFAULT_MODEL = "gpt-4o-mini"


def get_api_key() -> str:
    """Get FuelIX API key from environment.
    
    Returns:
        API key string or None if not found
    """
    api_key = os.getenv('FUELIX_API_KEY') or os.getenv('OPENAI_API_KEY')
    
    if not api_key:
        console.print("[yellow]⚠️  Warning: No API key found[/yellow]")
        console.print("[dim]Set FUELIX_API_KEY or OPENAI_API_KEY environment variable[/dim]")
        return None
    
    return api_key


def configure_dspy(
    model: str = FUELIX_DEFAULT_MODEL,
    api_base: str = FUELIX_API_BASE,
    api_key: str = None
) -> bool:
    """Configure DSPy to use FuelIX API endpoint.
    
    Args:
        model: LLM model name (e.g., 'gpt-4o-mini', 'gpt-4')
        api_base: FuelIX API base URL
        api_key: API key (if None, will try to get from environment)
        
    Returns:
        True if configuration successful, False otherwise
    """
    
    if api_key is None:
        api_key = get_api_key()
        if not api_key:
            return False
    
    try:
        # Configure DSPy to use FuelIX endpoint.
        # FuelIX is an OpenAI-compatible gateway at https://api.fuelix.ai/v1.
        # Prefix the model with "openai/" so litellm uses the OpenAI code path
        # and respects the custom api_base (without it litellm routes claude-*
        # models to Anthropic, ignoring api_base entirely).
        if "/" not in model:
            lm_model = f"openai/{model}"
        else:
            lm_model = model
        lm = dspy.LM(
            model=lm_model,
            api_base=api_base,
            api_key=api_key,
            temperature=0,
            max_tokens=16000,
        )
        dspy.configure(lm=lm)
        
        console.print(f"[green]✓[/green] DSPy configured with FuelIX API")
        console.print(f"[dim]  Endpoint: {api_base}[/dim]")
        console.print(f"[dim]  Model: {model}[/dim]")
        console.print(f"[dim]  Temperature: 0, Max tokens: 16000[/dim]")
        return True
        
    except Exception as e:
        console.print(f"[red]❌ Failed to configure DSPy: {e}[/red]")
        console.print("[yellow]Make sure you have set FUELIX_API_KEY environment variable[/yellow]")
        return False
