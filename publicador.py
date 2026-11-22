"""Disparo de posts: ÚNICO ponto que decide, gera, publica e registra. Resiliente a falhas de rede."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from .agentes import GeradorLegendas
from .config import Config
from .db import Repositorio
from .errors import (ErroDados, ErroLLM, ErroSocial, ErroSocialIncerto, ErroSocialTransitorio)
from .metricas import avaliar_gatilho, lucro_produto, pode_postar
from .models import Canal, Gatilho, Post, Produto, StatusPost, Variante, VarianteId
from .social import ClienteSocial

log = logging.getLogger(__name__)


@dataclass(slots=True)
class DisjuntorCanal:
    """Circuit breaker: depois de N falhas seguidas, deixa o canal em paz por um tempo."""
    falhas_seguidas: int = 0
    aberto_ate: float = 0.0

    def permite(self, agora: float) -> bool:
        return agora >= self.aberto_ate

    def registrar_sucesso(self) -> None:
        self.falhas_seguidas, self.aberto_ate = 0, 0.0

    def registrar_falha(self, agora: float, cfg: Config) -> None:
        self.falhas_seguidas += 1
        if self.falhas_seguidas >= cfg.disjuntor_falhas:
            self.aberto_ate = agora + cfg.disjuntor_pausa_s
            self.falhas_seguidas = 0
            log.error("Disjuntor aberto por %.0fs", cfg.disjuntor_pausa_s)


@dataclass(frozen=True, slots=True)
class ResultadoPost:
    var_id: VarianteId
    canal: Canal
    gatilho: Gatilho
    status: StatusPost
    erro: str | None = None


class Publicador:
    def __init__(self, repo: Repositorio, clientes: Mapping[Canal, ClienteSocial], legendas: GeradorLegendas,
                 cfg: Config, relogio: Callable[[], float] = time.time) -> None:
        self._repo, self._clientes, self._legendas, self._cfg, self._relogio = repo, clientes, legendas, cfg, relogio
        self._disjuntores = {c: DisjuntorCanal() for c in clientes}
        self._lock = threading.Lock()

    def executar(self, agora: float | None = None) -> list[ResultadoPost]:
        """1) reenvia o que ficou pendente por falha; 2) cria posts novos pelos gatilhos."""
        if not self._lock.acquire(blocking=False):
            log.info("Publicador já em execução; ignorando")
            return []
        try:
            agora = self._relogio() if agora is None else agora
            return self._reenviar_pendentes(agora) + self._novos_posts(agora)
        finally:
            self._lock.release()

    # ---- fluxo ----
    def _reenviar_pendentes(self, agora: float) -> list[ResultadoPost]:
        resultados = []
        for post in self._repo.posts_pendentes(agora):
            disj = self._disjuntores.get(post.canal)
            if disj is not None and not disj.permite(agora):
                continue
            novo = self._publicar(post, agora)  # reaproveita a legenda: não paga o modelo de novo
            resultados.append(ResultadoPost(post.var_id, post.canal, post.gatilho, novo.status, novo.erro))
        return resultados

    def _novos_posts(self, agora: float) -> list[ResultadoPost]:
        ativas = self._repo.variantes(so_ativas=True)
        if not ativas:
            return []
        todas = self._repo.variantes()
        receita = self._repo.receita_social_por_variante()
        resultados: list[ResultadoPost] = []
        for v in sorted(ativas, key=lambda x: x.lucro, reverse=True):  # mais lucrativas primeiro
            ultimo = self._repo.ultimo_post(v.id)
            gatilho = avaliar_gatilho(v, ativas, ultimo.hash_roteiro if ultimo else None,
                                      lucro_produto(todas, v.produto, receita), self._cfg)
            if gatilho is None:
                continue
            produto = self._repo.obter_produto(v.produto)
            if produto is None:
                raise ErroDados(f"Produto {v.produto!r} não cadastrado")
            for canal in self._clientes:
                if not self._disjuntores[canal].permite(agora):
                    continue
                if not pode_postar(self._repo.ultimo_ts_post(v.id, canal),
                                   self._repo.contar_posts(canal, agora - 86400), agora, self._cfg):
                    continue
                if canal is Canal.INSTAGRAM and not produto.imagem_url:
                    log.info("%s: sem imagem cadastrada; pulando Instagram", v.id)
                    continue
                r = self._criar_e_publicar(v, produto, canal, gatilho, agora)
                if r is not None:
                    resultados.append(r)
        return resultados

    def _criar_e_publicar(self, v: Variante, produto: Produto, canal: Canal, gatilho: Gatilho,
                          agora: float) -> ResultadoPost | None:
        link = self._cfg.link(v.produto, f"social-{canal.value}-{v.id}")
        try:
            legenda = self._legendas.gerar(v, produto, canal, self._repo.legendas_recentes(v.produto, 3), link)
        except ErroLLM as e:
            log.warning("Sem legenda para %s/%s (tentaremos no próximo ciclo): %s", v.id, canal.value, e)
            return None  # nada foi gravado, então não há cooldown: o próximo ciclo tenta de novo
        post = self._repo.inserir_post(Post(
            var_id=v.id, canal=canal, ts=agora, legenda=legenda, hash_roteiro=v.hash_roteiro,
            gatilho=gatilho, custo=self._cfg.custo_legenda))
        self._repo.adicionar_custo(v.id, self._cfg.custo_legenda)  # o custo da legenda entra no lucro
        novo = self._publicar(post, agora)
        return ResultadoPost(v.id, canal, gatilho, novo.status, novo.erro)

    def _publicar(self, post: Post, agora: float) -> Post:
        """Único lugar que conversa com um ClienteSocial e traduz o resultado em estado persistido."""
        cliente = self._clientes.get(post.canal)
        disj = self._disjuntores.get(post.canal)
        if cliente is None or disj is None:
            novo = replace(post, status=StatusPost.FALHOU, erro="canal não configurado")
            self._repo.atualizar_post(novo)
            return novo
        try:
            externo = cliente.publicar(post.legenda, self._imagem(post.var_id))
        except ErroSocialTransitorio as e:
            disj.registrar_falha(agora, self._cfg)
            tentativas = post.tentativas + 1
            if tentativas >= self._cfg.max_tentativas_post:
                novo = replace(post, status=StatusPost.FALHOU, tentativas=tentativas, erro=f"desistiu: {e}")
            else:
                novo = replace(post, tentativas=tentativas, erro=str(e),
                               proxima_tentativa=agora + self._cfg.reenvio_apos_s * tentativas)
            log.warning("Post %s/%s pendente (tentativa %d): %s", post.var_id, post.canal.value, tentativas, e)
        except ErroSocialIncerto as e:
            novo = replace(post, status=StatusPost.FALHOU, tentativas=post.tentativas + 1,
                           erro=f"incerto, verificar manualmente: {e}")
            log.error("Post %s/%s com resultado incerto: %s", post.var_id, post.canal.value, e)
        except ErroSocial as e:  # permanente e demais
            disj.registrar_falha(agora, self._cfg)
            novo = replace(post, status=StatusPost.FALHOU, tentativas=post.tentativas + 1, erro=str(e))
            log.error("Post %s/%s falhou: %s", post.var_id, post.canal.value, e)
        else:
            disj.registrar_sucesso()
            novo = replace(post, status=StatusPost.PUBLICADO, externo_id=externo,
                           tentativas=post.tentativas + 1, erro=None)
            log.info("Publicado %s/%s (%s, gatilho=%s)", post.var_id, post.canal.value, externo, post.gatilho.value)
        self._repo.atualizar_post(novo)
        return novo

    def _imagem(self, var_id: VarianteId) -> str | None:
        v = self._repo.obter_variante(var_id)
        p = self._repo.obter_produto(v.produto) if v else None
        return p.imagem_url if p else None
