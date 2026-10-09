from io import BytesIO

import pandas as pd

from estoque.parser_compras_sgi import extrair_compras, para_linhas_movimentacao


def _xls(rows: dict) -> bytes:
    buf = BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


def test_extrai_itens_com_quantidade_e_ignora_sem_quantidade():
    dados = _xls({
        "COD_PROD": ["08477", "05519", "99999"],
        "DESCRICAO": ["JOINER 250 ML", "ABAMEX MAXX 1 LT", "SEM QTD"],
        "COMPRA_TOTAL": [6228.0, 100.0, 0.0],
        "BONIFICACAO_TOTAL": [6228.0, 0.0, 0.0],
        "QTD_TOTAL": [12.0, 3.0, 0.0],
        "IMPOSTOS_FEDERAIS": [0, 0, 0],
        "VL_ICMS": [0, 0, 0],
        "cl_preco_medio": [0, 33.3, 0],
    })
    rel = extrair_compras(dados)
    assert rel.aviso is None
    assert [i.cod_produto for i in rel.itens] == ["08477", "05519"]
    assert rel.itens[0].qtd_total == 12.0

    linhas = para_linhas_movimentacao(rel)
    assert linhas[0] == {
        "cod_produto": "08477", "descricao": "JOINER 250 ML",
        "quantidade_entrada": 12.0, "valor_entrada": 6228.0,
    }


def test_colunas_faltando_gera_aviso():
    buf = BytesIO()
    pd.DataFrame({"A": [1], "B": [2]}).to_excel(buf, index=False, engine="openpyxl")
    rel = extrair_compras(buf.getvalue())
    assert rel.itens == []
    assert "Colunas esperadas" in rel.aviso


def test_sem_itens_com_quantidade_gera_aviso():
    dados = _xls({
        "COD_PROD": ["99999"], "DESCRICAO": ["X"], "COMPRA_TOTAL": [0.0],
        "BONIFICACAO_TOTAL": [0.0], "QTD_TOTAL": [0.0],
        "IMPOSTOS_FEDERAIS": [0], "VL_ICMS": [0], "cl_preco_medio": [0],
    })
    rel = extrair_compras(dados)
    assert rel.itens == []
    assert rel.aviso == "Nenhum item com quantidade encontrado no período/filtro."
