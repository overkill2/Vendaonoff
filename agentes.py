"""Todos os prompts do sistema, num lugar só: legendas, evolução de roteiros e atendimento."""
from __future__ import annotations

import json
import logging
import statistics
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Callable

from .config import Config
from .errors import ErroLLM
from .llm import ModeloLinguagem
from .models import Canal, LeadId, Mensagem, Papel, Produto, Segmento, Variante

if TYPE_CHECKING:
    from .db import Repositorio
    from .motor import Motor

log = logging.getLogger(__name__)

DIVULGACAO = "📢 Publi: link de afiliado. Posso receber comissão se você comprar."
FORMATOS: dict[Canal, str] = {
    Canal.INSTAGRAM: ("até 600 caracteres; gancho forte na primeira linha; 1 a 2 emojis; termine mandando "
                      "acessar o link da bio; até 5 hashtags relevantes"),
    Canal.FACEBOOK: "até 400 caracteres; tom conversacional; termine com uma chamada para ação clara",
}
PALAVRAS_PARAR = frozenset({"parar", "pare", "sair", "stop", "cancelar"})
MSG_OPTOUT = "Tudo bem, não enviarei mais mensagens. Obrigado!"
MSG_INSTABILIDADE = "Desculpe, estou com uma instabilidade rápida. Pode repetir sua mensagem em instantes?"


def _usuario(texto: str) -> list[Mensagem]:
    return [Mensagem(Papel.USUARIO, texto)]


def _pedir_json(llm: ModeloLinguagem, prompt: str, max_tokens: int = 800) -> Any:
    bruto = llm.gerar("Responda SOMENTE com JSON válido, sem markdown.", _usuario(prompt), max_tokens)
    try:
        return json.loads(bruto.replace("```json", "").replace("```", "").strip())
    except json.JSONDecodeError as e:
        raise ErroLLM("O modelo não devolveu JSON válido") from e


class GeradorLegendas:
    def __init__(self, llm: ModeloLinguagem, cfg: Config) -> None:
        self._llm = llm
        self._cfg = cfg

    def gerar(self, v: Variante, produto: Produto, canal: Canal,
              anteriores: Sequence[str], link: str) -> str:
        """Legenda final. O aviso de afiliado e o link são adicionados pelo código, nunca pelo modelo."""
        system = ("Você é um copywriter brasileiro especializado em afiliados. Escreva legendas persuasivas e "
                  "HONESTAS: use apenas os fatos fornecidos; não invente números, depoimentos, garantias de "
                  "resultado ou urgência falsa. Não inclua links nem avisos de publicidade. "
                  "Responda só com a legenda.")
        partes = [f"Produto: {produto.nome}"]
        if produto.descricao:
            partes.append(f"Descrição: {produto.descricao}")
        partes.append(f"Ângulo de venda a usar: {v.roteiro}")
        partes.append(f"Canal: {canal.value}\nFormato: {FORMATOS[canal]}")
        if anteriores:
            partes.append("Varie o estilo em relação a estas legendas recentes:\n- "
                          + "\n- ".join(a[:120] for a in anteriores))
        texto = self._llm.gerar(system, _usuario("\n".join(partes)), 500)
        final = [texto.strip()[:1500], DIVULGACAO]
        if canal is Canal.FACEBOOK:
            final.append(link)
        return "\n\n".join(final)


class Evolucao:
    """Cria variações de roteiros e as valida num simulador com juiz antes de irem ao ar."""

    def __init__(self, llm: ModeloLinguagem, cfg: Config) -> None:
        self._llm = llm
        self._cfg = cfg

    def mutar(self, base: Variante, pausada: Variante) -> str:
        return self._llm.gerar(
            "Você escreve roteiros de venda honestos para WhatsApp (3-4 frases, sem promessas falsas).",
            _usuario(f"Produto: {base.produto}\nRoteiro CAMPEÃO:\n{base.roteiro}\n\n"
                     f"Roteiro PAUSADO (não funcionou):\n{pausada.roteiro}\n\n"
                     "Crie uma NOVA variação, mantendo o que funciona no campeão e testando um ângulo "
                     "diferente dos dois. Responda só com o roteiro."), 400)

    def aprovar(self, produto: str, roteiro: str) -> bool:
        """Qualquer falha na avaliação significa NÃO aprovar (padrão seguro)."""
        try:
            personas = [str(p) for p in _pedir_json(
                self._llm, f"Crie 5 perfis curtos de clientes difíceis para '{produto}' "
                           '(desconfiado, sem tempo, acha caro, curioso, pronto). Formato: ["perfil 1", ...]')][:5]
            notas = [float(_pedir_json(
                self._llm, f"Produto: {produto}\nRoteiro: {roteiro}\nPersona: {p}\n\n"
                           "Simule uma conversa curta em que o vendedor segue o roteiro, sem mentir. Como juiz, "
                           'dê nota 0-10 (persuasão, clareza, objeções). Formato: {"nota": n}')["nota"])
                for p in personas]
        except (ErroLLM, KeyError, TypeError, ValueError) as e:
            log.warning("Avaliação do roteiro falhou; não aprovado: %s", e)
            return False
        return bool(notas) and statistics.mean(notas) >= self._cfg.nota_minima


class Atendente:
    """Conversa com o lead (WhatsApp): segmenta, escolhe a variante, mantém histórico e respeita opt-out."""

    def __init__(self, motor: Motor, repo: Repositorio, llm: ModeloLinguagem, cfg: Config,
                 relogio: Callable[[], float]) -> None:
        self._motor, self._repo, self._llm, self._cfg, self._relogio = motor, repo, llm, cfg, relogio

    def classificar(self, texto: str) -> Segmento:
        try:
            r = self._llm.gerar("Classifique a mensagem do lead em UMA categoria: "
                                + ", ".join(s.value for s in Segmento if s is not Segmento.GERAL)
                                + ". Responda só com a categoria.", _usuario(texto), 10)
        except ErroLLM:
            return Segmento.GERAL
        return Segmento.de_texto(r)

    def responder(self, lead_id: LeadId, texto: str) -> str | None:
        if texto.strip().lower() in PALAVRAS_PARAR:
            self._repo.marcar_optout(lead_id)
            return MSG_OPTOUT
        lead = self._repo.obter_lead(lead_id)
        if lead and lead.optout:
            return None  # respeita o opt-out: não responde

        variante = self._motor.atribuir_lead(lead_id, lead.segmento if lead else self.classificar(texto))
        self._repo.salvar_mensagem(lead_id, Mensagem(Papel.USUARIO, texto), self._relogio())
        hist = self._repo.ultimas_mensagens(lead_id, self._cfg.historico_msgs)
        while hist and hist[0].papel is not Papel.USUARIO:
            hist.pop(0)  # a API exige começar por "user"

        link = self._cfg.link(variante.produto, f"{variante.id}__{lead_id}")
        system = ("Você é um assistente virtual de vendas no WhatsApp.\n"
                  "- Na primeira mensagem, deixe claro que é um assistente automatizado.\n"
                  "- Seja honesto: sem promessas de resultado e sem urgência falsa.\n"
                  "- Responda curto (no máximo 4 frases) e tire dúvidas com base no produto.\n"
                  f"- Quando fizer sentido, envie este link: {link}\n"
                  f"Produto: {variante.produto}\nRoteiro de abordagem: {variante.roteiro}")
        try:
            resposta = self._llm.gerar(system, hist, 500)
        except ErroLLM as e:
            log.warning("LLM indisponível ao atender %s: %s", lead_id, e)
            return MSG_INSTABILIDADE  # não entra no histórico
        self._repo.salvar_mensagem(lead_id, Mensagem(Papel.ASSISTENTE, resposta), self._relogio())
        return resposta
