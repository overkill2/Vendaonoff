"""Métricas, bandit e gatilhos. Funções PURAS: sem I/O, sem relógio, com aleatoriedade injetada."""
from __future__ import annotations

import math
import statistics
import sys
from collections.abc import Mapping, Sequence
from random import Random

from .config import Config
from .models import Gatilho, Segmento, StatsSegmento, Variante, VarianteId

SEM_DADOS = StatsSegmento()


def lucro_produto(variantes: Sequence[Variante], produto: str,
                  receita_social: Mapping[VarianteId, float]) -> float:
    return sum(v.lucro + receita_social.get(v.id, 0.0) for v in variantes if v.produto == produto)


# ---------- bandit contextual ----------
def amostra(v: Variante, s: StatsSegmento, cfg: Config, rng: Random) -> float:
    """Thompson Sampling: Beta do segmento, com prior puxado para a taxa global da variante."""
    pg = (v.vendas + 1) / (v.leads + 2)
    a = 1 + s.vendas + cfg.k_prior * pg
    b = 1 + (s.leads - s.vendas) + cfg.k_prior * (1 - pg)
    return rng.betavariate(a, b) * v.comissao


def escolher_variante(ativas: Sequence[Variante], stats: Mapping[VarianteId, StatsSegmento],
                      cfg: Config, rng: Random) -> Variante:
    return max(ativas, key=lambda v: amostra(v, stats.get(v.id, SEM_DADOS), cfg, rng))


# ---------- poda adaptativa ----------
def leads_minimos(ativas: Sequence[Variante], cfg: Config) -> int:
    """Escala com o tráfego: só julga quando a variante teria ~N vendas na taxa média."""
    leads = sum(v.leads for v in ativas)
    vendas = sum(v.vendas for v in ativas)
    if vendas == 0:
        return sys.maxsize  # ninguém vendeu: não há base para podar
    return max(cfg.min_leads_poda, math.ceil(cfg.vendas_esperadas / (vendas / leads)))


def prob_melhor(ativas: Sequence[Variante], stats: Mapping[VarianteId, StatsSegmento],
                cfg: Config, rng: Random, n: int = 1000) -> dict[VarianteId, float]:
    vitorias = dict.fromkeys((v.id for v in ativas), 0)
    for _ in range(n):
        vitorias[escolher_variante(ativas, stats, cfg, rng).id] += 1
    return {vid: w / n for vid, w in vitorias.items()}


def selecionar_para_poda(ativas: Sequence[Variante],
                         stats: Mapping[Segmento, Mapping[VarianteId, StatsSegmento]],
                         cfg: Config, rng: Random, n: int = 1000) -> list[Variante]:
    """Pausa só quem é improvável de ser a melhor em TODOS os segmentos relevantes."""
    if len(ativas) <= cfg.min_ativas:
        return []
    total = sum(v.leads for v in ativas)
    piso = leads_minimos(ativas, cfg)
    candidatas = [v for v in ativas if v.leads >= piso]
    relevantes = [seg for seg, d in stats.items()
                  if sum(d[v.id].leads for v in ativas if v.id in d) >= cfg.share_segmento_min * total]
    if not candidatas or not relevantes:
        return []

    melhor = dict.fromkeys((v.id for v in ativas), 0.0)
    for seg in relevantes:
        for vid, p in prob_melhor(ativas, stats[seg], cfg, rng, n).items():
            melhor[vid] = max(melhor[vid], p)

    podar: list[Variante] = []
    restantes = len(ativas)
    for v in sorted(candidatas, key=lambda x: melhor[x.id]):
        if restantes <= cfg.min_ativas:
            break
        if melhor[v.id] < cfg.prob_min_melhor:
            podar.append(v)
            restantes -= 1
    return podar


# ---------- gatilhos de publicação ----------
def avaliar_gatilho(v: Variante, ativas: Sequence[Variante], ultimo_hash: str | None,
                    lucro_do_produto: float, cfg: Config) -> Gatilho | None:
    mediana = statistics.median(x.valor_por_lead for x in ativas)
    if v.vendas >= cfg.min_vendas_post and v.lucro >= cfg.lucro_min_post and v.valor_por_lead >= mediana:
        return Gatilho.DESEMPENHO
    atualizada = (ultimo_hash is not None and ultimo_hash != v.hash_roteiro) or \
                 (ultimo_hash is None and v.geracao > 0)
    if atualizada and lucro_do_produto >= cfg.lucro_min_post:
        return Gatilho.ATUALIZACAO
    return None


def pode_postar(ultimo_ts: float | None, posts_24h: int, agora: float, cfg: Config) -> bool:
    if ultimo_ts is not None and agora - ultimo_ts < cfg.cooldown_h * 3600:
        return False
    return posts_24h < cfg.max_posts_dia
