"""Base LLM provider interface."""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any


class LLMProvider(ABC):
    """Abstract interface for LLM providers."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: str,
        schema: Optional[Dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """Generate a response from the LLM.

        Args:
            prompt: User prompt
            system_prompt: System instruction
            schema: Optional JSON schema for structured output
            max_tokens: Max tokens to generate
            temperature: Sampling temperature

        Returns:
            Generated text response
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources."""
        pass
