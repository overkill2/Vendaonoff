"""Servidor HTTP (webhooks). Traduz requisições em chamadas ao domínio e erros em códigos HTTP."""
from __future__ import annotations

import hmac
import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .aplicacao import Aplicacao
from .config import TOKEN_PADRAO
from .errors import (AfiliadosError, ErroAutenticacao, ErroConfiguracao, ErroDados, ErroLLM, ErroRequisicao,
                     ErroRota, ErroSocial)
from .models import Canal, LeadId, VarianteId

log = logging.getLogger(__name__)
MAX_CORPO = 64 * 1024
_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (ErroAutenticacao, 401), (ErroRota, 404), (ErroRequisicao, 400), (ErroDados, 422),
    (ErroLLM, 503), (ErroSocial, 502), (AfiliadosError, 500),
)


def _campo(dados: dict[str, Any], nome: str) -> str:
    valor = dados.get(nome)
    if not isinstance(valor, str) or not valor:
        raise ErroRequisicao(f"Campo obrigatório ausente ou inválido: {nome}")
    return valor


def _valor(dados: dict[str, Any]) -> float | None:
    bruto = dados.get("valor")
    if bruto is None:
        return None
    try:
        return float(bruto)
    except (TypeError, ValueError):
        raise ErroRequisicao("Campo 'valor' inválido") from None


def rotear(app: Aplicacao, caminho: str, dados: dict[str, Any]) -> dict[str, Any]:
    if caminho == "/mensagem":
        return {"resposta": app.atendente.responder(LeadId(_campo(dados, "lead_id")), _campo(dados, "texto"))}
    if caminho == "/venda":
        src = str(dados.get("src", ""))
        if src.startswith("social-"):  # venda vinda de um post
            partes = src.split("-", 2)
            try:
                canal = Canal(partes[1])
                var_id = VarianteId(urllib.parse.unquote(partes[2]))
            except (ValueError, IndexError):
                raise ErroRequisicao("src de venda social malformado") from None
            ok = app.motor.registrar_venda_social(canal, var_id, _valor(dados))
        else:
            lead_id = dados.get("lead_id") or urllib.parse.unquote(src).split("__")[-1]
            if not isinstance(lead_id, str) or not lead_id:
                raise ErroRequisicao("Informe lead_id ou src")
            ok = app.motor.registrar_venda(LeadId(lead_id), _valor(dados))
        threading.Thread(target=app.ciclo, daemon=True).start()
        return {"registrada": ok}
    raise ErroRota(f"Rota inexistente: {caminho}")


def criar_servidor(app: Aplicacao, porta: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    if app.cfg.webhook_token == TOKEN_PADRAO:
        raise ErroConfiguracao("Defina WEBHOOK_TOKEN com um valor secreto antes de subir o servidor")

    class Handler(BaseHTTPRequestHandler):
        def _enviar(self, codigo: int, corpo: dict[str, Any]) -> None:
            bruto = json.dumps(corpo, ensure_ascii=False).encode()
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(bruto)))
            self.end_headers()
            self.wfile.write(bruto)

        def do_GET(self) -> None:
            self._enviar(200, {"ok": True}) if self.path == "/saude" else self._enviar(404, {"erro": "rota inexistente"})

        def do_POST(self) -> None:
            try:
                if not hmac.compare_digest(self.headers.get("X-Token", ""), app.cfg.webhook_token):
                    raise ErroAutenticacao("token inválido")
                tamanho = int(self.headers.get("Content-Length") or 0)
                if tamanho > MAX_CORPO:
                    raise ErroRequisicao("corpo grande demais")
                try:
                    dados = json.loads(self.rfile.read(tamanho) or b"{}")
                except json.JSONDecodeError:
                    raise ErroRequisicao("JSON inválido") from None
                if not isinstance(dados, dict):
                    raise ErroRequisicao("O corpo deve ser um objeto JSON")
                self._enviar(200, rotear(app, self.path, dados))
            except Exception as e:  # ponto único de tradução de erros
                codigo = next((c for t, c in _STATUS if isinstance(e, t)), 500)
                if codigo == 500 and not isinstance(e, AfiliadosError):
                    log.exception("Erro inesperado")
                    self._enviar(500, {"erro": "erro interno"})
                else:
                    self._enviar(codigo, {"erro": str(e)})

        def log_message(self, *args: Any) -> None:
            pass

    return ThreadingHTTPServer((host, porta), Handler)
