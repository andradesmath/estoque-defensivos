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
