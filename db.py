"""Persistência. É o ÚNICO módulo que contém SQL; o resto do sistema fala com esta classe."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace

from .errors import ErroBanco
from .models import (Canal, Gatilho, Lead, LeadId, Mensagem, Papel, Post, Produto, Segmento,
                     StatsSegmento, StatusPost, Variante, VarianteId)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS produtos (
    nome TEXT PRIMARY KEY, comissao REAL NOT NULL, imagem_url TEXT, descricao TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS variantes (
    id TEXT PRIMARY KEY, produto TEXT NOT NULL REFERENCES produtos(nome), roteiro TEXT NOT NULL,
    leads INTEGER NOT NULL DEFAULT 0, vendas INTEGER NOT NULL DEFAULT 0, custo REAL NOT NULL DEFAULT 0,
    ativa INTEGER NOT NULL DEFAULT 1, parent_id TEXT, geracao INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS leads (
    lead_id TEXT PRIMARY KEY, var_id TEXT NOT NULL, segmento TEXT NOT NULL, ts REAL NOT NULL,
    optout INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS vendas (
    lead_id TEXT PRIMARY KEY, var_id TEXT NOT NULL, valor REAL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS stats_segmento (
    segmento TEXT NOT NULL, var_id TEXT NOT NULL, leads INTEGER NOT NULL DEFAULT 0,
    vendas INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (segmento, var_id));
CREATE TABLE IF NOT EXISTS mensagens (
    id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT NOT NULL, papel TEXT NOT NULL,
    texto TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_msg ON mensagens(lead_id, id);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, var_id TEXT NOT NULL, canal TEXT NOT NULL, ts REAL NOT NULL,
    legenda TEXT NOT NULL, hash_roteiro TEXT NOT NULL, gatilho TEXT NOT NULL, custo REAL NOT NULL,
    status TEXT NOT NULL, externo_id TEXT, tentativas INTEGER NOT NULL DEFAULT 0,
    proxima_tentativa REAL NOT NULL DEFAULT 0, erro TEXT);
CREATE TABLE IF NOT EXISTS receita_social (
    id INTEGER PRIMARY KEY AUTOINCREMENT, canal TEXT NOT NULL, var_id TEXT NOT NULL,
    valor REAL NOT NULL, ts REAL NOT NULL);
"""

_SQL_VARIANTE = "SELECT v.*, p.comissao FROM variantes v JOIN produtos p ON p.nome = v.produto"


def _variante(r: sqlite3.Row) -> Variante:
    return Variante(
        id=VarianteId(r["id"]), produto=r["produto"], roteiro=r["roteiro"], comissao=r["comissao"],
        leads=r["leads"], vendas=r["vendas"], custo=r["custo"], ativa=bool(r["ativa"]),
        parent_id=VarianteId(r["parent_id"]) if r["parent_id"] else None, geracao=r["geracao"])


def _post(r: sqlite3.Row) -> Post:
    return Post(
        id=r["id"], var_id=VarianteId(r["var_id"]), canal=Canal(r["canal"]), ts=r["ts"],
        legenda=r["legenda"], hash_roteiro=r["hash_roteiro"], gatilho=Gatilho(r["gatilho"]),
        custo=r["custo"], status=StatusPost(r["status"]), externo_id=r["externo_id"],
        tentativas=r["tentativas"], proxima_tentativa=r["proxima_tentativa"], erro=r["erro"])


class Repositorio:
    def __init__(self, caminho: str = ":memory:") -> None:
        self._con = sqlite3.connect(caminho, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._tx() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Serializa o acesso e garante commit/rollback; converte erros do SQLite em ErroBanco."""
        with self._lock:
            try:
                with self._con:
                    yield self._con
            except sqlite3.Error as e:
                raise ErroBanco(str(e)) from e

    def fechar(self) -> None:
        self._con.close()

    # ---- produtos ----
    def salvar_produto(self, p: Produto) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO produtos (nome, comissao, imagem_url, descricao) VALUES (?,?,?,?) "
                "ON CONFLICT(nome) DO UPDATE SET comissao=excluded.comissao, "
                "imagem_url=excluded.imagem_url, descricao=excluded.descricao",
                (p.nome, p.comissao, p.imagem_url, p.descricao))

    def obter_produto(self, nome: str) -> Produto | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM produtos WHERE nome=?", (nome,)).fetchone()
        return Produto(r["nome"], r["comissao"], r["imagem_url"], r["descricao"]) if r else None

    # ---- variantes ----
    def salvar_variante(self, v: Variante) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO variantes (id, produto, roteiro, parent_id, geracao) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET roteiro=excluded.roteiro",
                (v.id, v.produto, v.roteiro, v.parent_id, v.geracao))

    def variantes(self, so_ativas: bool = False) -> list[Variante]:
        sql = _SQL_VARIANTE + (" WHERE v.ativa=1" if so_ativas else "")
        with self._tx() as c:
            return [_variante(r) for r in c.execute(sql)]

    def obter_variante(self, var_id: VarianteId) -> Variante | None:
        with self._tx() as c:
            r = c.execute(_SQL_VARIANTE + " WHERE v.id=?", (var_id,)).fetchone()
        return _variante(r) if r else None

    def pausar(self, var_id: VarianteId) -> None:
        with self._tx() as c:
            c.execute("UPDATE variantes SET ativa=0 WHERE id=?", (var_id,))

    def atualizar_roteiro(self, var_id: VarianteId, roteiro: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE variantes SET roteiro=? WHERE id=?", (roteiro, var_id))

    def adicionar_custo(self, var_id: VarianteId, valor: float) -> None:
        with self._tx() as c:
            c.execute("UPDATE variantes SET custo=custo+? WHERE id=?", (valor, var_id))

    # ---- leads e vendas (operações atômicas) ----
    def obter_lead(self, lead_id: LeadId) -> Lead | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
        return Lead(LeadId(r["lead_id"]), VarianteId(r["var_id"]), Segmento.de_texto(r["segmento"]),
                    bool(r["optout"])) if r else None

    def criar_lead(self, lead_id: LeadId, var_id: VarianteId, segmento: Segmento,
                   custo: float, agora: float) -> bool:
        """False se o lead já existia (concorrência): quem chamou deve reler o lead."""
        with self._tx() as c:
            if c.execute("SELECT 1 FROM leads WHERE lead_id=?", (lead_id,)).fetchone():
                return False
            c.execute("INSERT INTO leads (lead_id, var_id, segmento, ts) VALUES (?,?,?,?)",
                      (lead_id, var_id, segmento.value, agora))
            c.execute("UPDATE variantes SET leads=leads+1, custo=custo+? WHERE id=?", (custo, var_id))
            c.execute("INSERT INTO stats_segmento (segmento, var_id, leads) VALUES (?,?,1) "
                      "ON CONFLICT(segmento, var_id) DO UPDATE SET leads=leads+1",
                      (segmento.value, var_id))
            return True

    def marcar_optout(self, lead_id: LeadId) -> None:
        with self._tx() as c:
            c.execute("UPDATE leads SET optout=1 WHERE lead_id=?", (lead_id,))

    def registrar_venda(self, lead_id: LeadId, valor: float | None, agora: float) -> bool:
        """Idempotente: False se o lead é desconhecido ou a venda já foi registrada."""
        with self._tx() as c:
            lead = c.execute("SELECT var_id, segmento FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
            if lead is None or c.execute("SELECT 1 FROM vendas WHERE lead_id=?", (lead_id,)).fetchone():
                return False
            c.execute("INSERT INTO vendas (lead_id, var_id, valor, ts) VALUES (?,?,?,?)",
                      (lead_id, lead["var_id"], valor, agora))
            c.execute("UPDATE variantes SET vendas=vendas+1 WHERE id=?", (lead["var_id"],))
            c.execute("UPDATE stats_segmento SET vendas=vendas+1 WHERE segmento=? AND var_id=?",
                      (lead["segmento"], lead["var_id"]))
            return True

    def stats_segmentos(self) -> dict[Segmento, dict[VarianteId, StatsSegmento]]:
        out: dict[Segmento, dict[VarianteId, StatsSegmento]] = {}
        with self._tx() as c:
            for r in c.execute("SELECT * FROM stats_segmento"):
                out.setdefault(Segmento.de_texto(r["segmento"]), {})[VarianteId(r["var_id"])] = \
                    StatsSegmento(r["leads"], r["vendas"])
        return out

    # ---- conversas ----
    def salvar_mensagem(self, lead_id: LeadId, m: Mensagem, agora: float) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO mensagens (lead_id, papel, texto, ts) VALUES (?,?,?,?)",
                      (lead_id, m.papel.value, m.texto, agora))

    def ultimas_mensagens(self, lead_id: LeadId, n: int) -> list[Mensagem]:
        with self._tx() as c:
            linhas = c.execute("SELECT papel, texto FROM mensagens WHERE lead_id=? ORDER BY id DESC LIMIT ?",
                               (lead_id, n)).fetchall()
        return [Mensagem(Papel(r["papel"]), r["texto"]) for r in reversed(linhas)]

    # ---- posts sociais ----
    def inserir_post(self, p: Post) -> Post:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO posts (var_id, canal, ts, legenda, hash_roteiro, gatilho, custo, status, "
                "externo_id, tentativas, proxima_tentativa, erro) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.var_id, p.canal.value, p.ts, p.legenda, p.hash_roteiro, p.gatilho.value, p.custo,
                 p.status.value, p.externo_id, p.tentativas, p.proxima_tentativa, p.erro))
            novo_id = cur.lastrowid
        return replace(p, id=novo_id)

    def atualizar_post(self, p: Post) -> None:
        with self._tx() as c:
            c.execute("UPDATE posts SET status=?, externo_id=?, tentativas=?, proxima_tentativa=?, erro=? "
                      "WHERE id=?", (p.status.value, p.externo_id, p.tentativas, p.proxima_tentativa,
                                      p.erro, p.id))

    def posts_pendentes(self, agora: float) -> list[Post]:
        with self._tx() as c:
            return [_post(r) for r in c.execute(
                "SELECT * FROM posts WHERE status=? AND proxima_tentativa<=? ORDER BY id",
                (StatusPost.PENDENTE.value, agora))]

    def ultimo_post(self, var_id: VarianteId) -> Post | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM posts WHERE var_id=? ORDER BY id DESC LIMIT 1", (var_id,)).fetchone()
        return _post(r) if r else None

    def ultimo_ts_post(self, var_id: VarianteId, canal: Canal) -> float | None:
        """Considera qualquer status: falhas também ativam o cooldown (evita martelar um canal quebrado)."""
        with self._tx() as c:
            return c.execute("SELECT MAX(ts) FROM posts WHERE var_id=? AND canal=?",
                             (var_id, canal.value)).fetchone()[0]

    def contar_posts(self, canal: Canal, desde: float) -> int:
        with self._tx() as c:
            return c.execute("SELECT COUNT(*) FROM posts WHERE canal=? AND ts>? AND status!=?",
                             (canal.value, desde, StatusPost.FALHOU.value)).fetchone()[0]

    def legendas_recentes(self, produto: str, n: int) -> list[str]:
        with self._tx() as c:
            return [r["legenda"] for r in c.execute(
                "SELECT p.legenda FROM posts p JOIN variantes v ON v.id=p.var_id "
                "WHERE v.produto=? ORDER BY p.id DESC LIMIT ?", (produto, n))]

    def posts(self) -> list[Post]:
        with self._tx() as c:
            return [_post(r) for r in c.execute("SELECT * FROM posts ORDER BY id")]

    def resumo_posts_por_canal(self) -> dict[Canal, tuple[int, float]]:
        with self._tx() as c:
            return {Canal(r["canal"]): (r["n"], r["custo"]) for r in c.execute(
                "SELECT canal, COUNT(*) n, SUM(custo) custo FROM posts WHERE status=? GROUP BY canal",
                (StatusPost.PUBLICADO.value,))}

    # ---- receita vinda das redes ----
    def registrar_receita_social(self, canal: Canal, var_id: VarianteId, valor: float, agora: float) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO receita_social (canal, var_id, valor, ts) VALUES (?,?,?,?)",
                      (canal.value, var_id, valor, agora))

    def receita_social_por_variante(self) -> dict[VarianteId, float]:
        with self._tx() as c:
            return {VarianteId(r["var_id"]): r["t"] for r in c.execute(
                "SELECT var_id, SUM(valor) t FROM receita_social GROUP BY var_id")}

    def receita_social_por_canal(self) -> dict[Canal, float]:
        with self._tx() as c:
            return {Canal(r["canal"]): r["t"] for r in c.execute(
                "SELECT canal, SUM(valor) t FROM receita_social GROUP BY canal")}
