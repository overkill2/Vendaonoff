"""Modelos de domínio imutáveis e tipados. Nenhuma lógica de I/O aqui."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

VarianteId = NewType("VarianteId", str)
LeadId = NewType("LeadId", str)


class Segmento(StrEnum):
    DESCONFIADO = "desconfiado"
    SEM_TEMPO = "sem_tempo"
    ACHA_CARO = "acha_caro"
    CURIOSO = "curioso"
    PRONTO = "pronto"
    GERAL = "geral"

    @classmethod
    def de_texto(cls, texto: str) -> Segmento:
        try:
            return cls(texto.strip().lower())
        except ValueError:
            return cls.GERAL


class Canal(StrEnum):
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"


class Gatilho(StrEnum):
    DESEMPENHO = "desempenho"
    ATUALIZACAO = "atualizacao"


class StatusPost(StrEnum):
    PENDENTE = "pendente"
    PUBLICADO = "publicado"
    FALHOU = "falhou"


class Papel(StrEnum):
    USUARIO = "user"
    ASSISTENTE = "assistant"


@dataclass(frozen=True, slots=True)
class Produto:
    nome: str
    comissao: float
    imagem_url: str | None = None
    descricao: str = ""


@dataclass(frozen=True, slots=True)
class Variante:
    id: VarianteId
    produto: str
    roteiro: str
    comissao: float
    leads: int = 0
    vendas: int = 0
    custo: float = 0.0
    ativa: bool = True
    parent_id: VarianteId | None = None
    geracao: int = 0

    @property
    def lucro(self) -> float:
        return self.vendas * self.comissao - self.custo

    @property
    def taxa_conversao(self) -> float:
        return self.vendas / self.leads if self.leads else 0.0

    @property
    def valor_por_lead(self) -> float:
        return self.taxa_conversao * self.comissao

    @property
    def hash_roteiro(self) -> str:
        return hashlib.sha1(self.roteiro.encode()).hexdigest()[:10]


@dataclass(frozen=True, slots=True)
class Lead:
    id: LeadId
    var_id: VarianteId
    segmento: Segmento
    optout: bool = False


@dataclass(frozen=True, slots=True)
class Mensagem:
    papel: Papel
    texto: str


@dataclass(frozen=True, slots=True)
class StatsSegmento:
    leads: int = 0
    vendas: int = 0


@dataclass(frozen=True, slots=True)
class Post:
    var_id: VarianteId
    canal: Canal
    ts: float
    legenda: str
    hash_roteiro: str
    gatilho: Gatilho
    custo: float
    status: StatusPost = StatusPost.PENDENTE
    externo_id: str | None = None
    tentativas: int = 0
    proxima_tentativa: float = 0.0
    erro: str | None = None
    id: int | None = None
