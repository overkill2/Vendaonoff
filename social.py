"""Integração com as APIs sociais (Instagram e Facebook via Graph API). Só fala com a rede, sem regra de negócio."""
from __future__ import annotations

import json
import logging
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol

from .config import Config, Credenciais
from .errors import (ErroConfiguracao, ErroSocialIncerto, ErroSocialPermanente, ErroSocialTransitorio)
from .models import Canal

log = logging.getLogger(__name__)

Enviador = Callable[[str, str, Mapping[str, str]], dict[str, Any]]
CODIGOS_LIMITE = frozenset({4, 17, 32, 613})   # limites de taxa da Graph API (requisição não processada)
CODIGOS_SERVICO = frozenset({1, 2})            # falha temporária do serviço (resultado ambíguo)


class ClienteSocial(Protocol):
    canal: Canal

    def publicar(self, legenda: str, imagem_url: str | None) -> str:
        """Retorna o ID externo do post. Levanta ErroSocial* em caso de falha."""
        ...


class GraphHTTP:
    """Camada HTTP com retentativas e backoff exponencial com jitter.

    Regra de ouro: requisições NÃO idempotentes (publicar) só são repetidas quando temos certeza de que
    não foram processadas. Em caso ambíguo (timeout, 5xx) levantamos ErroSocialIncerto, em vez de arriscar
    um post duplicado.
    """

    def __init__(self, cfg: Config, *, enviar: Enviador | None = None,
                 dormir: Callable[[float], None] = time.sleep, rng: random.Random | None = None) -> None:
        self._cfg = cfg
        self._enviar = enviar or self._enviar_urllib
        self.dormir = dormir
        self._rng = rng or random.Random()

    def requisitar(self, metodo: str, caminho: str, dados: Mapping[str, str], *,
                   idempotente: bool) -> dict[str, Any]:
        url = f"https://graph.facebook.com/{self._cfg.graph_version}/{caminho}"
        for tentativa in range(1, self._cfg.max_tentativas + 1):
            try:
                return self._enviar(metodo, url, dados)
            except ErroSocialTransitorio as e:
                if not idempotente and not e.seguro_repetir:
                    raise ErroSocialIncerto(f"resultado incerto; não repeti automaticamente: {e}") from e
                if tentativa == self._cfg.max_tentativas:
                    raise
                espera = self._cfg.backoff_base_s * 2 ** (tentativa - 1) * (0.5 + self._rng.random())
                log.warning("Falha transitória (%s); tentativa %d/%d, nova em %.1fs",
                            e, tentativa, self._cfg.max_tentativas, espera)
                self.dormir(espera)
        raise ErroSocialTransitorio("sem tentativas configuradas")

    @staticmethod
    def _enviar_urllib(metodo: str, url: str, dados: Mapping[str, str]) -> dict[str, Any]:
        corpo = urllib.parse.urlencode(dados).encode() if metodo == "POST" else None
        if metodo == "GET":
            url += "?" + urllib.parse.urlencode(dados)
        req = urllib.request.Request(url, data=corpo, method=metodo)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())  # type: ignore[no-any-return]
        except urllib.error.HTTPError as e:
            texto = e.read().decode(errors="replace")[:300]
            try:
                codigo = json.loads(texto)["error"]["code"]
            except (ValueError, KeyError, TypeError):
                codigo = None
            if e.code == 429 or codigo in CODIGOS_LIMITE:
                raise ErroSocialTransitorio(f"limite de taxa ({e.code})", seguro_repetir=True) from None
            if e.code >= 500 or codigo in CODIGOS_SERVICO:
                raise ErroSocialTransitorio(f"erro do serviço ({e.code})", seguro_repetir=False) from None
            raise ErroSocialPermanente(f"Graph API {e.code}: {texto}") from None
        except urllib.error.URLError as e:
            seguro = isinstance(e.reason, (ConnectionRefusedError, socket.gaierror))
            raise ErroSocialTransitorio(f"rede: {e.reason}", seguro_repetir=seguro) from None
        except (TimeoutError, ConnectionError, OSError) as e:
            raise ErroSocialTransitorio(f"rede: {e}", seguro_repetir=False) from None
        except ValueError as e:  # corpo 200 que não é JSON: o post provavelmente saiu
            raise ErroSocialTransitorio(f"resposta ilegível: {e}", seguro_repetir=False) from None


def _extrair_id(resp: Mapping[str, Any], *chaves: str) -> str:
    for k in (*chaves, "id"):
        if resp.get(k):
            return str(resp[k])
    raise ErroSocialIncerto(f"resposta sem ID: {dict(resp)!r}")


class FacebookPage:
    canal = Canal.FACEBOOK

    def __init__(self, http: GraphHTTP, page_id: str, token: str) -> None:
        self._http, self._page_id, self._token = http, page_id, token

    def publicar(self, legenda: str, imagem_url: str | None) -> str:
        if imagem_url:
            r = self._http.requisitar("POST", f"{self._page_id}/photos", {
                "url": imagem_url, "caption": legenda, "access_token": self._token}, idempotente=False)
            return _extrair_id(r, "post_id")
        r = self._http.requisitar("POST", f"{self._page_id}/feed", {
            "message": legenda, "access_token": self._token}, idempotente=False)
        return _extrair_id(r)


class InstagramBusiness:
    canal = Canal.INSTAGRAM

    def __init__(self, http: GraphHTTP, ig_user_id: str, token: str) -> None:
        self._http, self._ig_id, self._token = http, ig_user_id, token

    def publicar(self, legenda: str, imagem_url: str | None) -> str:
        if not imagem_url:
            raise ErroSocialPermanente("Instagram exige uma imagem em URL pública")
        # 1) container: criar é idempotente (um container não publicado é inofensivo)
        container = _extrair_id(self._http.requisitar("POST", f"{self._ig_id}/media", {
            "image_url": imagem_url, "caption": legenda, "access_token": self._token}, idempotente=True))
        # 2) espera ficar pronto
        for _ in range(10):
            status = self._http.requisitar("GET", container, {
                "fields": "status_code", "access_token": self._token}, idempotente=True).get("status_code")
            if status == "FINISHED":
                break
            if status in {"ERROR", "EXPIRED"}:
                raise ErroSocialPermanente(f"container do Instagram em estado {status}")
            self._http.dormir(2.0)
        else:
            raise ErroSocialTransitorio("container do Instagram não ficou pronto a tempo")
        # 3) publicar: NÃO idempotente
        return _extrair_id(self._http.requisitar("POST", f"{self._ig_id}/media_publish", {
            "creation_id": container, "access_token": self._token}, idempotente=False))


class ClienteDryRun:
    """Não publica nada: só registra no log o que seria publicado."""

    def __init__(self, canal: Canal) -> None:
        self.canal = canal
        self._n = 0

    def publicar(self, legenda: str, imagem_url: str | None) -> str:
        self._n += 1
        log.info("[DRY-RUN %s] %s", self.canal.value, legenda.splitlines()[0][:70])
        return f"dry-{self.canal.value}-{self._n}"


def criar_clientes(cfg: Config, cred: Credenciais, canais: Iterable[Canal] | None = None,
                   http: GraphHTTP | None = None) -> dict[Canal, ClienteSocial]:
    canais = list(canais) if canais is not None else list(Canal)
    if cfg.social_dry_run:
        return {c: ClienteDryRun(c) for c in canais}
    http = http or GraphHTTP(cfg)
    clientes: dict[Canal, ClienteSocial] = {}
    for c in canais:
        if c is Canal.INSTAGRAM:
            if not (cred.ig_user_id and cred.ig_token):
                raise ErroConfiguracao("Credenciais do Instagram ausentes (IG_USER_ID, IG_TOKEN)")
            clientes[c] = InstagramBusiness(http, cred.ig_user_id, cred.ig_token)
        else:
            if not (cred.fb_page_id and cred.fb_page_token):
                raise ErroConfiguracao("Credenciais do Facebook ausentes (FB_PAGE_ID, FB_PAGE_TOKEN)")
            clientes[c] = FacebookPage(http, cred.fb_page_id, cred.fb_page_token)
    return clientes
