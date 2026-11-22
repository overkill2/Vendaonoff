"""Fronteira com o modelo de linguagem. O resto do sistema depende só do Protocol."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from .config import Config
from .errors import ErroConfiguracao, ErroLLM
from .models import Mensagem


class ModeloLinguagem(Protocol):
    def gerar(self, system: str, mensagens: Sequence[Mensagem], max_tokens: int = 500) -> str: ...


class ClaudeLLM:
    """Retentativas com backoff ficam por conta do SDK (max_retries); aqui só traduzimos os erros."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._cliente: Any = None

    def _obter_cliente(self) -> Any:
        if self._cliente is None:
            try:
                import anthropic
            except ImportError as e:
                raise ErroConfiguracao("Instale o SDK: pip install anthropic") from e
            self._cliente = anthropic.Anthropic(max_retries=self._cfg.max_tentativas, timeout=30.0)
        return self._cliente

    def gerar(self, system: str, mensagens: Sequence[Mensagem], max_tokens: int = 500) -> str:
        import anthropic

        cliente = self._obter_cliente()
        try:
            r = cliente.messages.create(
                model=self._cfg.modelo, max_tokens=max_tokens, system=system,
                messages=[{"role": m.papel.value, "content": m.texto} for m in mensagens])
        except anthropic.APIError as e:
            raise ErroLLM(f"Falha na API do modelo: {e}") from e
        texto = "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
        if not texto:
            raise ErroLLM("Resposta vazia do modelo")
        return texto
