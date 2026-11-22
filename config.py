"""Configuração central e imutável, lida do ambiente (cada campo vira uma variável MAIÚSCULA)."""
from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field, fields

from .errors import ErroConfiguracao

TOKEN_PADRAO = "troque-este-token"

_CONVERSORES = {
    "int": int,
    "float": float,
    "str": str,
    "bool": lambda s: s.strip().lower() in {"1", "true", "sim", "yes"},
}


@dataclass(frozen=True, slots=True)
class Config:
    db_path: str = "afiliados.db"
    webhook_token: str = TOKEN_PADRAO
    link_base: str = "https://plataforma.exemplo/p/{produto}?src={src}"
    modelo: str = "claude-sonnet-5-5"

    # bandit e poda
    custo_por_lead: float = 0.10
    k_prior: float = 10.0
    min_ativas: int = 2
    max_ativas: int = 6
    max_geracao: int = 5
    min_leads_poda: int = 30
    vendas_esperadas: int = 5
    prob_min_melhor: float = 0.02
    share_segmento_min: float = 0.10
    nota_minima: float = 8.0

    # conversa
    historico_msgs: int = 12

    # gatilhos e limites de publicação
    min_vendas_post: int = 5
    lucro_min_post: float = 100.0
    cooldown_h: float = 24.0
    max_posts_dia: int = 3
    custo_legenda: float = 0.15
    social_dry_run: bool = True
    graph_version: str = "v26.0"

    # resiliência
    max_tentativas: int = 4          # por requisição (HTTP e SDK)
    backoff_base_s: float = 1.0
    max_tentativas_post: int = 5     # reenvios de um post pendente
    reenvio_apos_s: float = 900.0
    disjuntor_falhas: int = 3
    disjuntor_pausa_s: float = 1800.0

    @classmethod
    def do_ambiente(cls) -> Config:
        valores: dict[str, object] = {}
        for f in fields(cls):
            bruto = os.environ.get(f.name.upper())
            if bruto is None:
                continue
            try:
                valores[f.name] = _CONVERSORES[str(f.type)](bruto)
            except ValueError as e:
                raise ErroConfiguracao(f"{f.name.upper()} inválido: {bruto!r}") from e
        return cls(**valores)  # type: ignore[arg-type]

    def link(self, produto: str, src: str) -> str:
        return self.link_base.format(
            produto=produto.replace(" ", "-"), src=urllib.parse.quote(src, safe="_-.~")
        )


@dataclass(frozen=True, slots=True)
class Credenciais:
    ig_user_id: str = ""
    ig_token: str = field(default="", repr=False)
    fb_page_id: str = ""
    fb_page_token: str = field(default="", repr=False)

    @classmethod
    def do_ambiente(cls) -> Credenciais:
        return cls(**{f.name: os.environ.get(f.name.upper(), "") for f in fields(cls)})
