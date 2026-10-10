"""Sugestão de produto do sistema para linhas de NF (sem banco)."""
import io

import pandas as pd
import pytest

from estoque.match_produto import sugerir_produto


@pytest.fixture(scope="module")
def produtos(base_xlsx_bytes):
    d = pd.read_excel(io.BytesIO(base_xlsx_bytes), sheet_name="Defensivos", dtype={"Código": str})
    return dict(zip(d["Código"], d["Produto"]))


@pytest.mark.parametrize("nf,esperado", [
    ("APPROVE - 12X1", "06935"),
    ("YAMATO SC - 12X1", "08438"),
    ("FROWNCIDE 750 HT - 12X1", "00312"),   # 1 L, não a de 5 LT (03721)
    ("CERCOBIN 875 WG - 12X1", "04859"),
    ("ACTARA 250 WG - 12X1", "00051"),      # 1 KG, não a de 100 GRS (00050)
])
def test_sugere_produto_certo(produtos, nf, esperado):
    r = sugerir_produto(nf, produtos)
    assert r is not None and r[0] == esperado


@pytest.mark.parametrize("nf", ["PRODUTO INEXISTENTE XYZ - 12X1", "", "AB", None])
def test_sem_confianca_nao_sugere(produtos, nf):
    assert sugerir_produto(nf, produtos) is None


def test_empate_ambiguo_nao_sugere():
    assert sugerir_produto("FOO BAR 1 L", {"00001": "FOO BAR A", "00002": "FOO BAR B"}) is None


def test_unidade_sem_numero_vale_1():
    """"ARTYS LT" é a apresentação de 1 litro: o cadastro lista 1 LT, 5 LT e 20 LT lado
    a lado e o relatório do SGI escreve só "LT" para a de 1. Sem essa regra o nome sem
    número empata com todas as apresentações e o matcher desiste - foi o que deixou 21
    itens da contagem de Piatã de fora."""
    cad = {"00289": "ARTYS 1 LT", "00288": "ARTYS 20 LT", "00287": "ARTYS 5 LT"}
    assert sugerir_produto("ARTYS LT", cad)[0] == "00289"
    assert sugerir_produto("ARTYS 5 LT", cad)[0] == "00287"


def test_lit_e_lt_sao_a_mesma_unidade():
    """O cadastro usa as duas grafias ("PADRON 5 LIT", "REGLONE 01 LT")."""
    cad = {"09441": "PADRON 5 LIT", "03133": "PADRON 1LT"}
    assert sugerir_produto("PADRON 5LT", cad)[0] == "09441"
    assert sugerir_produto("PADRON LT", cad)[0] == "03133"


def test_sem_apresentacao_nenhuma_continua_recusando():
    """"VERDADERO" existe em 1 KG e 5 KG e o nome não diz qual: recusar é o certo -
    saldo inicial chutado é pior que saldo faltando."""
    cad = {"06908": "VERDADERO 600 WG 1 KG", "09416": "VERDADERO 600 WG 5KG"}
    assert sugerir_produto("VERDADERO", cad) is None


def test_melhores_mostra_candidatos_mesmo_sem_confianca():
    """O que sugerir_produto recusa, `melhores` lista - é com isso que a pessoa decide."""
    from estoque.match_produto import melhores
    cad = {"06908": "VERDADERO 600 WG 1 KG", "09416": "VERDADERO 600 WG 5KG"}
    cods = [c for c, _s in melhores("VERDADERO", cad, n=3)]
    assert set(cods) == {"06908", "09416"}
