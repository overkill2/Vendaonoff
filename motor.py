"""Motor: regras de negócio do funil (leads, vendas, poda, evolução). Sem SQL e sem HTTP."""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Protocol

from .config import Config
from .db import Repositorio
from .errors import AfiliadosError, ErroDados
from .metricas import escolher_variante, selecionar_para_poda
from .models import Canal, LeadId, Produto, Segmento, Variante, VarianteId

log = logging.getLogger(__name__)


class Evoluidor(Protocol):
    def mutar(self, base: Variante, pausada: Variante) -> str: ...
    def aprovar(self, produto: str, roteiro: str) -> bool: ...


class Motor:
    def __init__(self, repo: Repositorio, evolucao: Evoluidor | None, cfg: Config, *,
                 rng: random.Random | None = None, relogio: Callable[[], float] = time.time) -> None:
        self.repo, self._evolucao, self._cfg = repo, evolucao, cfg
        self._rng = rng or random.Random()
        self._relogio = relogio

    # ---- cadastro ----
    def registrar_produto(self, p: Produto) -> None:
        self.repo.salvar_produto(p)

    def criar_variante(self, var_id: VarianteId, produto: str, roteiro: str) -> Variante:
        p = self.repo.obter_produto(produto)
        if p is None:
            raise ErroDados(f"Produto {produto!r} não cadastrado")
        v = Variante(id=var_id, produto=produto, roteiro=roteiro, comissao=p.comissao)
        self.repo.salvar_variante(v)
        return v

    def atualizar_roteiro(self, var_id: VarianteId, roteiro: str) -> None:
        """Atualizar o roteiro muda o hash e pode disparar o gatilho de 'atualização' no publicador."""
        self.repo.atualizar_roteiro(var_id, roteiro)

    # ---- funil ----
    def atribuir_lead(self, lead_id: LeadId, segmento: Segmento) -> Variante:
        """Lead novo recebe uma variante via bandit contextual; lead conhecido mantém a mesma."""
        lead = self.repo.obter_lead(lead_id)
        if lead is None:
            ativas = self.repo.variantes(so_ativas=True)
            if not ativas:
                raise ErroDados("Nenhuma variante ativa")
            stats = self.repo.stats_segmentos().get(segmento, {})
            escolhida = escolher_variante(ativas, stats, self._cfg, self._rng)
            if self.repo.criar_lead(lead_id, escolhida.id, segmento, self._cfg.custo_por_lead, self._relogio()):
                return escolhida
            lead = self.repo.obter_lead(lead_id)  # outro processo criou antes: usa a variante dele
        if lead is None:
            raise ErroDados(f"Lead {lead_id!r} inconsistente")
        v = self.repo.obter_variante(lead.var_id)
        if v is None:
            raise ErroDados(f"Variante {lead.var_id!r} não existe")
        return v

    def registrar_venda(self, lead_id: LeadId, valor: float | None = None) -> bool:
        return self.repo.registrar_venda(lead_id, valor, self._relogio())

    def registrar_venda_social(self, canal: Canal, var_id: VarianteId, valor: float | None = None) -> bool:
        """Venda vinda de um post: fica fora das estatísticas do bandit (não tem lead)."""
        v = self.repo.obter_variante(var_id)
        if v is None:
            return False
        self.repo.registrar_receita_social(canal, var_id, valor if valor is not None else v.comissao,
                                           self._relogio())
        return True

    # ---- evolução ----
    def podar(self) -> list[Variante]:
        podadas = selecionar_para_poda(self.repo.variantes(so_ativas=True), self.repo.stats_segmentos(),
                                       self._cfg, self._rng)
        for v in podadas:
            self.repo.pausar(v.id)
            log.info("PODA %s (leads=%d, vendas=%d)", v.id, v.leads, v.vendas)
        return podadas

    def gerar_substituta(self, pausada: Variante) -> Variante | None:
        if self._evolucao is None:
            return None
        ativas = self.repo.variantes(so_ativas=True)
        if len(ativas) >= self._cfg.max_ativas or pausada.geracao >= self._cfg.max_geracao:
            return None
        candidatas = [v for v in ativas if v.produto == pausada.produto] or ativas
        if not candidatas:
            return None
        base = max(candidatas, key=lambda v: (v.vendas + 1) / (v.leads + 2))
        roteiro = self._evolucao.mutar(base, pausada)
        if not self._evolucao.aprovar(base.produto, roteiro):
            log.info("Mutação de %s reprovada no simulador", base.id)
            return None
        geracao = base.geracao + 1
        todas = len(self.repo.variantes())
        novo_id = VarianteId(f"{base.produto[:12]}|g{geracao}_{todas + 1}")
        nova = Variante(id=novo_id, produto=base.produto, roteiro=roteiro, comissao=base.comissao,
                        parent_id=base.id, geracao=geracao)
        self.repo.salvar_variante(nova)
        log.info("MUTAÇÃO: %s (filha de %s)", novo_id, base.id)
        return nova

    def manutencao(self) -> None:
        for pausada in self.podar():
            try:
                self.gerar_substituta(pausada)
            except AfiliadosError as e:  # uma falha do modelo não pode derrubar a manutenção
                log.warning("Sem substituta para %s: %s", pausada.id, e)

    # ---- relatório ----
    def relatorio(self) -> str:
        social_var = self.repo.receita_social_por_variante()
        linhas = ["--- LUCRO POR VARIANTE (funil + social) ---"]
        for v in sorted(self.repo.variantes(), key=lambda x: x.lucro + social_var.get(x.id, 0), reverse=True):
            s = social_var.get(v.id, 0.0)
            linhas.append(f"{v.id:26s} [{'ATIVA' if v.ativa else 'PAUSADA':7s}] leads={v.leads:4d} "
                          f"vendas={v.vendas:3d} funil=R${v.lucro:8.2f} social=R${s:7.2f} total=R${v.lucro + s:8.2f}")
        linhas.append("\n--- LUCRO POR CANAL SOCIAL ---")
        receita = self.repo.receita_social_por_canal()
        for canal, (n, custo) in self.repo.resumo_posts_por_canal().items():
            r = receita.get(canal, 0.0)
            linhas.append(f"{canal.value:10s} posts={n:2d} custo=R${custo:.2f} receita=R${r:.2f} lucro=R${r - custo:.2f}")
        return "\n".join(linhas)
