from io import BytesIO

import xlwt

from estoque.parser_compras_sgi import extrair_compras, para_linhas_movimentacao


def _xls(colunas: list[str], linhas: list[list]) -> bytes:
    """Gera um .xls real (BIFF), igual ao que o SGI exporta — não um .xlsx disfarçado,
    porque extrair_compras agora lê com xlrd (só entende o formato antigo)."""
    livro = xlwt.Workbook()
    aba = livro.add_sheet("dados")
    for c, nome in enumerate(colunas):
        aba.write(0, c, nome)
    for r, linha in enumerate(linhas, start=1):
        for c, valor in enumerate(linha):
            aba.write(r, c, valor)
    buf = BytesIO()
    livro.save(buf)
    return buf.getvalue()


_COLUNAS = ["COD_PROD", "DESCRICAO", "COMPRA_TOTAL", "BONIFICACAO_TOTAL", "QTD_TOTAL",
            "IMPOSTOS_FEDERAIS", "VL_ICMS", "cl_preco_medio"]


def test_extrai_itens_com_quantidade_e_ignora_sem_quantidade():
    dados = _xls(_COLUNAS, [
        ["08477", "JOINER 250 ML", 6228.0, 6228.0, 12.0, 0, 0, 0],
        ["05519", "ABAMEX MAXX 1 LT", 100.0, 0.0, 3.0, 0, 0, 33.3],
        ["99999", "SEM QTD", 0.0, 0.0, 0.0, 0, 0, 0],
    ])
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
    dados = _xls(["A", "B"], [[1, 2]])
    rel = extrair_compras(dados)
    assert rel.itens == []
    assert "Colunas esperadas" in rel.aviso


def test_sem_itens_com_quantidade_gera_aviso():
    dados = _xls(_COLUNAS, [["99999", "X", 0.0, 0.0, 0.0, 0, 0, 0]])
    rel = extrair_compras(dados)
    assert rel.itens == []
    assert rel.aviso == "Nenhum item com quantidade encontrado no período/filtro."
