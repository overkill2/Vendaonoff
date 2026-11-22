"""Composição: único lugar que sabe montar todas as peças (injeção de dependências)."""
from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from .agentes import Atendente, Evolucao, GeradorLegendas
from .config import Config, Credenciais
from .db import Repositorio
from .errors import AfiliadosError
from .llm import ClaudeLLM, ModeloLinguagem
from .models import Canal
from .motor import Evoluidor, Motor
from .publicador import Publicador, ResultadoPost
from .social import ClienteSocial, criar_clientes

log = logging.getLogger(__name__)


@dataclass
class Aplicacao:
    cfg: Config
    repo: Repositorio
    motor: Motor
    atendente: Atendente
    publicador: Publicador
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def criar(cls, cfg: Config | None = None, cred: Credenciais | None = None, *,
              llm: ModeloLinguagem | None = None, clientes: Mapping[Canal, ClienteSocial] | None = None,
              evolucao: Evoluidor | None = None, relogio: Callable[[], float] = time.time,
              rng: random.Random | None = None) -> Aplicacao:
        cfg = cfg or Config.do_ambiente()
        repo = Repositorio(cfg.db_path)
        llm = llm or ClaudeLLM(cfg)
        motor = Motor(repo, evolucao or Evolucao(llm, cfg), cfg, rng=rng, relogio=relogio)
        clientes = clientes if clientes is not None else criar_clientes(cfg, cred or Credenciais.do_ambiente())
        return cls(cfg, repo, motor, Atendente(motor, repo, llm, cfg, relogio),
                   Publicador(repo, clientes, GeradorLegendas(llm, cfg), cfg, relogio))

    def ciclo(self, agora: float | None = None) -> list[ResultadoPost]:
        """Manutenção (poda + substitutas) e depois publicação. Nunca roda dois ciclos ao mesmo tempo."""
        if not self._lock.acquire(blocking=False):
            log.info("Ciclo já em andamento; ignorando")
            return []
        try:
            self.motor.manutencao()
            return self.publicador.executar(agora)
        except AfiliadosError:
            log.exception("Falha no ciclo")
            return []
        finally:
            self._lock.release()
