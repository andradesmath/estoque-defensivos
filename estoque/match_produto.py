"""
estoque/match_produto.py - Sugere o produto do sistema (código próprio) para uma linha de NF.

Só SUGERE: a tela pré-seleciona o produto e o usuário confere antes de confirmar. Se a
confiança for baixa ou houver empate entre candidatos, não sugere nada (melhor em branco
do que entrada de estoque no produto errado).

Comparação por chave compacta do nome: sem acento, sem embalagem de caixa ("12X1"),
sem unidade e sem espaços/pontuação. Assim "FROWNCIDE 750 HT" casa com "FROWNCIDE 750HT 1 L".
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

VERSAO = 2             # aumente ao mudar a regra: invalida leituras de NF já guardadas na sessão
LIMIAR = 0.80          # score mínimo para sugerir
MARGEM = 0.05          # vantagem mínima do 1º sobre o 2º (evita empate ambíguo)
PENALIDADE_TAMANHO = 0.15  # candidato com tamanho de embalagem diferente do da nota

_UNIDADES = {"L": "L", "LT": "L", "LTS": "L", "LIT": "L", "LITS": "L",
             "LITRO": "L", "LITROS": "L",
             "KG": "KG", "KGS": "KG", "G": "G", "GR": "G", "GRS": "G", "ML": "ML"}
_RE_CAIXA = re.compile(r"\b(\d+)\s*[Xx]\s*(\d+(?:[.,]\d+)?)\b")          # 12X1, 6 x 5
# "LIT" entra porque o cadastro usa as duas grafias no mesmo produto ("PADRON 5 LIT",
# "REGLONE 01 LT"); sem ela o tamanho não era reconhecido de um dos lados e o par não
# casava.
_RE_TAM = re.compile(r"\b0*(\d+(?:[.,]\d+)?)\s*(LTS?|LITS?|LITROS?|L|KGS?|GRS?|G|ML)\b")
# Unidade SOLTA, sem número: "ARTYS LT", "APPROVE KG", "KASUMIN LITRO".
_RE_UNIDADE_SOLTA = re.compile(r"\b(LTS?|LITS?|LITROS?|L|KGS?|GRS?|G|ML)\b")


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _tamanho(texto: str) -> tuple[float, str] | None:
    """Tamanho da embalagem: '12X1' -> (1.0, None-unidade); '5 LT' -> (5.0, 'L').

    UNIDADE SEM NÚMERO VALE 1: "ARTYS LT" é a apresentação de 1 litro. Não é chute - é
    a convenção do próprio cadastro, que lista "ARTYS 20 LT", "ARTYS 5 LT" e
    "ARTYS 1 LT" lado a lado, e dos relatórios do SGI, que escrevem só "ARTYS LT" para
    a de 1. Sem isso, nome sem número empata com todas as apresentações do produto e o
    matcher desiste - foi o que deixou 21 itens da contagem de Piatã de fora."""
    t = _sem_acento(texto).upper()
    m = _RE_TAM.search(t)
    if m:
        return float(m.group(1).replace(",", ".")), _UNIDADES[m.group(2)]
    m = _RE_CAIXA.search(t)
    if m:
        return float(m.group(2).replace(",", ".")), ""
    m = _RE_UNIDADE_SOLTA.search(t)
    if m:
        return 1.0, _UNIDADES[m.group(1)]
    return None


def _chave(texto: str) -> str:
    t = _sem_acento(texto).upper()
    t = _RE_CAIXA.sub(" ", t)
    t = _RE_TAM.sub(" ", t)
    t = re.sub(r"\b(?:LTS?|LITS?|LITROS?|KGS?|GRS?|G|ML|L)\b", " ", t)  # unidade solta ("APPROVE KG")
    return re.sub(r"[^A-Z0-9]", "", t)


def _score(chave_nf: str, chave_cand: str) -> float:
    if not chave_nf or not chave_cand:
        return 0.0
    if chave_nf == chave_cand:
        return 1.0
    base = SequenceMatcher(None, chave_nf, chave_cand).ratio()
    if chave_cand.startswith(chave_nf) or chave_nf.startswith(chave_cand):
        base = max(base, 0.92)
    return base


def chave(descricao: str) -> str:
    """Nome compacto, sem tamanho nem pontuação - público para quem precisa comparar
    nomes por fora (ver scripts/importar_contagem_unidade.py)."""
    return _chave(descricao or "")


def tamanho(descricao: str) -> tuple[float, str] | None:
    """Tamanho da embalagem de uma descrição, público para quem precisa desempatar
    apresentações por fora (ver scripts/importar_contagem_unidade.py)."""
    return _tamanho(descricao or "")


def melhores(descricao: str, produtos: dict[str, str], n: int = 5) -> list[tuple[str, float]]:
    """Os `n` candidatos mais parecidos, do melhor pro pior, SEM aplicar limiar nem
    margem. Serve pra mostrar opções a quem vai decidir quando sugerir_produto se
    recusa a escolher - que é o caso comum de nome sem apresentação ("VERDADERO", que
    existe em 1 KG e 5 KG)."""
    chave = _chave(descricao or "")
    if len(chave) < 4:
        return []
    tam = _tamanho(descricao)
    ranking = []
    for cod, desc in produtos.items():
        s = _score(chave, _chave(desc or ""))
        tam_c = _tamanho(desc or "")
        if tam and tam_c and tam[0] != tam_c[0]:
            s -= PENALIDADE_TAMANHO
        ranking.append((round(s, 3), cod))
    ranking.sort(reverse=True)
    return [(cod, s) for s, cod in ranking[:n]]


def sugerir_produto(descricao_nf: str, produtos: dict[str, str]) -> tuple[str, float] | None:
    """(cod_produto, score) do produto mais provável, ou None se não houver confiança.
    `produtos` = {cod_produto: descricao}."""
    ranking = melhores(descricao_nf, produtos, n=2)
    if not ranking or ranking[0][1] < LIMIAR:
        return None
    if len(ranking) > 1 and ranking[0][1] - ranking[1][1] < MARGEM:
        return None
    return ranking[0][0], ranking[0][1]
