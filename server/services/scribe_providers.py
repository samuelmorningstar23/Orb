"""Orb Scribe — LLM Provider Abstraction

Lightweight provider layer so Scribe stages can swap LLM backends
without rewriting business logic. Only OllamaProvider is implemented
in this pass; the interface is designed so OpenAI-compatible, vLLM,
or local-transformers providers can be added later.
"""
import json
import time
from typing import Optional, AsyncGenerator
from abc import ABC, abstractmethod

import httpx
from config import OLLAMA_BASE_URL


class LLMProvider(ABC):
    """Minimal interface for Scribe LLM calls."""

    @abstractmethod
    def generate(
        self,
        prompt: str,
        system: str = "",
        model: str = "",
        temperature: float = 0.3,
        max_tokens: int = 2048,
        ctx_size: int = 4096,
        purpose: str = "scribe_draft",
    ) -> str:
        """Synchronous, non-streaming generation. Returns full text."""
        ...

    @abstractmethod
    async def agenerate(
        self,
        prompt: str,
        system: str = "",
        model: str = "",
        temperature: float = 0.3,
        max_tokens: int = 2048,
        ctx_size: int = 4096,
        purpose: str = "scribe_draft",
    ) -> str:
        """Async non-streaming generation. Returns full text."""
        ...

    @abstractmethod
    async def agenerate_stream(
        self,
        prompt: str,
        system: str = "",
        model: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
        ctx_size: int = 8192,
        purpose: str = "scribe_draft",
    ) -> AsyncGenerator[str, None]:
        """Async streaming generation. Yields token strings."""
        ...


class OllamaProvider(LLMProvider):
    """Ollama-backed LLM provider (current default for all Scribe stages)."""

    def __init__(self, base_url: str = OLLAMA_BASE_URL):
        self.base_url = base_url

    def generate(
        self,
        prompt: str,
        system: str = "",
        model: str = "",
        temperature: float = 0.3,
        max_tokens: int = 2048,
        ctx_size: int = 4096,
        purpose: str = "scribe_draft",
    ) -> str:
        """Synchronous Ollama call (used by correction stage in subprocess context)."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            with httpx.Client(timeout=120.0, trust_env=False) as client:
                response = client.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                        "options": {
                            "temperature": temperature,
                            "num_predict": max_tokens,
                            "num_ctx": ctx_size,
                        },
                    },
                )
                data = response.json()
                return data.get("message", {}).get("content", "").strip()
        except Exception as e:
            print(f"[ScribeProvider/Ollama] generate error: {e}")
            return ""

    async def agenerate(
        self,
        prompt: str,
        system: str = "",
        model: str = "",
        temperature: float = 0.3,
        max_tokens: int = 2048,
        ctx_size: int = 4096,
        purpose: str = "scribe_draft",
    ) -> str:
        """Async non-streaming Ollama call."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
                response = await client.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                        "options": {
                            "temperature": temperature,
                            "num_predict": max_tokens,
                            "num_ctx": ctx_size,
                        },
                    },
                )
                data = response.json()
                return data.get("message", {}).get("content", "").strip()
        except Exception as e:
            print(f"[ScribeProvider/Ollama] agenerate error: {e}")
            return ""

    async def agenerate_stream(
        self,
        prompt: str,
        system: str = "",
        model: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
        ctx_size: int = 8192,
        purpose: str = "scribe_draft",
    ) -> AsyncGenerator[str, None]:
        """Async streaming Ollama call. Yields content tokens."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                        "num_ctx": ctx_size,
                    },
                },
            ) as response:
                try:
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                            content = data.get("message", {}).get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
                except GeneratorExit:
                    print("  [ScribeProvider/Ollama] Stream cancelled")
                finally:
                    await response.aclose()


# ── Provider Registry ──

class MLXProvider(LLMProvider):
    """MLX-LM backed local LLM provider. Runs in-process on Apple Silicon GPU."""

    def __init__(self):
        pass

    def generate(
        self,
        prompt: str,
        system: str = "",
        model: str = "",
        temperature: float = 0.3,
        max_tokens: int = 2048,
        ctx_size: int = 4096,
        purpose: str = "scribe_draft",
    ) -> str:
        """Synchronous, non-streaming in-process MLX generation."""
        try:
            from services.voice_agent_service import get_voice_agent
            agent = get_voice_agent()
            agent.load()
            if not agent._loaded:
                return ""
            
            from mlx_lm import generate as mlx_generate
            from mlx_lm.sample_utils import make_sampler
            from services.gpu_coordinator import vram_guard
            
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            
            formatted_prompt = agent._tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            
            with vram_guard("Scribe MLX (sync)"):
                result = mlx_generate(
                    agent._model,
                    agent._tokenizer,
                    prompt=formatted_prompt,
                    max_tokens=max_tokens,
                    sampler=make_sampler(temp=temperature),
                )
            return result.strip()
        except Exception as e:
            print(f"[ScribeProvider/MLX] generate error: {e}")
            return ""

    async def agenerate(
        self,
        prompt: str,
        system: str = "",
        model: str = "",
        temperature: float = 0.3,
        max_tokens: int = 2048,
        ctx_size: int = 4096,
        purpose: str = "scribe_draft",
    ) -> str:
        """Async non-streaming generation running on thread executor."""
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self.generate,
            prompt,
            system,
            model,
            temperature,
            max_tokens,
            ctx_size
        )

    async def agenerate_stream(
        self,
        prompt: str,
        system: str = "",
        model: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
        ctx_size: int = 8192,
        purpose: str = "scribe_draft",
    ) -> AsyncGenerator[str, None]:
        """Async streaming generation using background thread queue.
        
        Acquires UMSQ GPU lock at PRIORITY_SCRIBE (2) — user-initiated but
        yields to Sage voice if both are queued simultaneously.
        """
        try:
            from services.voice_agent_service import get_voice_agent
            agent = get_voice_agent()
            agent.load()
            if not agent._loaded:
                yield "MLX model not loaded."
                return
            
            import asyncio
            import queue
            import threading
            from mlx_lm import stream_generate
            from mlx_lm.sample_utils import make_sampler
            from services.gpu_coordinator import umsq, vram_guard, PRIORITY_SCRIBE
            
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            
            formatted_prompt = agent._tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            
            sampler = make_sampler(temp=temperature)
            q = queue.Queue()
            
            def _producer():
                with vram_guard("Scribe MLX (stream)"):
                    try:
                        for resp in stream_generate(
                            agent._model,
                            agent._tokenizer,
                            prompt=formatted_prompt,
                            max_tokens=max_tokens,
                            sampler=sampler,
                        ):
                            q.put(resp.text)
                    except Exception as e:
                        q.put(e)
                    finally:
                        q.put(None)
            
            async with umsq.acquire("Scribe MLX", priority=PRIORITY_SCRIBE):
                thread = threading.Thread(target=_producer, daemon=True)
                thread.start()
                
                loop = asyncio.get_running_loop()
                while True:
                    item = await loop.run_in_executor(None, q.get)
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        print(f"[ScribeProvider/MLX] Stream error: {item}")
                        break
                    yield item
        except Exception as e:
            print(f"[ScribeProvider/MLX] agenerate_stream error: {e}")
            yield ""


_PROVIDERS: dict[str, type[LLMProvider]] = {
    "ollama": OllamaProvider,
    "mlx": MLXProvider,
}


def get_provider(provider_name: str = "ollama") -> LLMProvider:
    """Resolve a provider instance by name. Every provider here runs on the
    local box — Scribe payloads are clinical narrative (the transcript IS the
    note), so inference never leaves the building."""
    cls = _PROVIDERS.get(provider_name)
    if not cls:
        raise ValueError(f"Unknown Scribe LLM provider: {provider_name!r}. Available: {list(_PROVIDERS.keys())}")
    return cls()


class StageTimer:
    """Simple context manager for timing pipeline stages."""

    def __init__(self):
        self.timings: dict[str, float] = {}
        self._start: Optional[float] = None
        self._stage: Optional[str] = None

    def start(self, stage: str):
        self._stage = stage
        self._start = time.time()

    def stop(self):
        if self._start and self._stage:
            self.timings[self._stage] = round(time.time() - self._start, 3)
        self._start = None
        self._stage = None

    def to_dict(self) -> dict:
        return dict(self.timings)
