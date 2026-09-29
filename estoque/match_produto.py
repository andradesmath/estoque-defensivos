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

LIMIAR = 0.80          # score mínimo para sugerir
MARGEM = 0.05          # vantagem mínima do 1º sobre o 2º (evita empate ambíguo)
PENALIDADE_TAMANHO = 0.15  # candidato com tamanho de embalagem diferente do da nota

_UNIDADES = {"L": "L", "LT": "L", "LTS": "L", "LITRO": "L", "LITROS": "L",
             "KG": "KG", "KGS": "KG", "G": "G", "GR": "G", "GRS": "G", "ML": "ML"}
_RE_CAIXA = re.compile(r"\b(\d+)\s*[Xx]\s*(\d+(?:[.,]\d+)?)\b")          # 12X1, 6 x 5
_RE_TAM = re.compile(r"\b0*(\d+(?:[.,]\d+)?)\s*(LTS?|LITROS?|L|KGS?|GRS?|G|ML)\b")  # 1 L, 01L, 5LT, 100 GRS


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _tamanho(texto: str) -> tuple[float, str] | None:
    """Tamanho da embalagem: '12X1' -> (1.0, None-unidade); '5 LT' -> (5.0, 'L')."""
    t = _sem_acento(texto).upper()
    m = _RE_TAM.search(t)
    if m:
        return float(m.group(1).replace(",", ".")), _UNIDADES[m.group(2)]
    m = _RE_CAIXA.search(t)
    if m:
        return float(m.group(2).replace(",", ".")), ""
    return None


def _chave(texto: str) -> str:
    t = _sem_acento(texto).upper()
    t = _RE_CAIXA.sub(" ", t)
    t = _RE_TAM.sub(" ", t)
    t = re.sub(r"\b(?:LT|LTS|KG|KGS|L)\b", " ", t)  # unidade solta ("APPROVE KG", "EPINGLE LT")
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


def sugerir_produto(descricao_nf: str, produtos: dict[str, str]) -> tuple[str, float] | None:
    """(cod_produto, score) do produto mais provável, ou None se não houver confiança.
    `produtos` = {cod_produto: descricao}."""
    chave_nf = _chave(descricao_nf or "")
    if len(chave_nf) < 4:
        return None
    tam_nf = _tamanho(descricao_nf)
    ranking = []
    for cod, desc in produtos.items():
        s = _score(chave_nf, _chave(desc or ""))
        tam_c = _tamanho(desc or "")
        if tam_nf and tam_c and tam_nf[0] != tam_c[0]:
            s -= PENALIDADE_TAMANHO
        ranking.append((s, cod))
    ranking.sort(reverse=True)
    if not ranking or ranking[0][0] < LIMIAR:
        return None
    if len(ranking) > 1 and ranking[0][0] - ranking[1][0] < MARGEM:
        return None
    return ranking[0][1], round(ranking[0][0], 3)
