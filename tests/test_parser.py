from datetime import date
from decimal import Decimal

from estoque import parser_sgi as ps


def test_pdf_real_reconcilia_com_total_impresso(pdf_real_bytes):
    res = ps.parse_relatorio_defensivos(pdf_real_bytes)
    assert res.estrategia == "celula_por_linha"
    assert res.periodo == (date(2026, 9, 22), date(2026, 9, 24))
    assert res.grupos == ["DEFENSIVOS"]
    assert len(res.itens) == 40
    assert res.registros_descartados == 0
    assert sum(i["qtd"] for i in res.itens) == Decimal(126)
    # total impresso: R$ 12.502,84; soma das linhas: 12.502,85 (1 centavo de arredondamento do SGI)
    assert res.totais[0]["valor_total"] == Decimal("12502.84")
    assert abs(sum(i["valor_total"] for i in res.itens) - Decimal("12502.84")) <= Decimal("0.05")
    assert ps.validar(res) == []


def test_pdf_real_campos_da_primeira_linha(pdf_real_bytes):
    it = ps.parse_relatorio_defensivos(pdf_real_bytes).itens[0]
    assert it["cod_produto"] == "09490"
    assert it["descricao"] == "DEXTER PLUS 1LT"
    assert it["qtd"] == Decimal(10) and it["valor_total"] == Decimal("1397.24")


def test_descricao_com_hifen_e_codigo_com_zeros(pdf_real_bytes):
    itens = {i["cod_produto"]: i for i in ps.parse_relatorio_defensivos(pdf_real_bytes).itens}
    assert itens["04859"]["descricao"] == "CERCOBIN 875WG - 12X1 KG"
    assert "00004" in itens and itens["00004"]["qtd"] == Decimal(9)


def test_validar_detecta_periodo_diferente_do_pedido(pdf_real_bytes):
    res = ps.parse_relatorio_defensivos(pdf_real_bytes)
    probs = ps.validar(res, dia_esperado=date(2026, 9, 24))
    assert any("difere do dia pedido" in p for p in probs)


def test_validar_detecta_grupo_errado(pdf_real_bytes):
    res = ps.parse_relatorio_defensivos(pdf_real_bytes)
    assert any("filtro de grupo" in p for p in ps.validar(res, grupo_esperado="ADUBO"))
    res.grupos = ["DEFENSIVOS", "ADUBO"]
    assert any("filtro de grupo" in p for p in ps.validar(res))


def test_validar_detecta_total_que_nao_bate(pdf_real_bytes):
    linhas = ps.extrair_linhas_pdf(pdf_real_bytes)
    i = linhas.index("1.397,24")
    linhas[i] = "1.497,24"  # adultera um valor de produto
    probs = ps.validar(ps.parse_linhas(linhas))
    assert any("não bate com o 'Total:'" in p for p in probs)


def test_linha_perdida_e_detectada(pdf_real_bytes):
    linhas = ps.extrair_linhas_pdf(pdf_real_bytes)
    i = linhas.index("09490")  # remove o código do 1º produto -> registro some
    del linhas[i]
    res = ps.parse_linhas(linhas)
    assert len(res.itens) == 39
    assert ps.validar(res)  # soma não bate


def test_estrategia_inline_quando_extrator_devolve_linha_inteira():
    linhas = [
        "22/09/2026 a 22/09/2026", "Relatorio Totais de Vendas por Produtos", "Grupos:", " DEFENSIVOS,",
        "0 09490 DEXTER PLUS 1LT - 4 10 10 10 1.397,24 87,50",
        "1 00004 EPINGLE LT - 2 5 9 9 200,00 12,50",
        "Total: 6 15 19 19 1.597,24 100%",
    ]
    res = ps.parse_linhas(linhas)
    assert res.estrategia == "inline" and len(res.itens) == 2
    assert ps.validar(res, dia_esperado=date(2026, 9, 22)) == []


def test_formato_marca_fornecedor_estilo_vendedor():
    linhas = [
        "22/09/2026 a 22/09/2026", "Relatorio Totais de Vendas por Produtos", "Grupos:", " DEFENSIVOS,", "N°",
        "0", "09490", "DEXTER", "PLUS 1LT", "00122 - NORTENE MATRIZ", "00122 - NORTENE MATRIZ",
        "4", "10", "10", "10", "1.397,24", "100,00", "Total:", "4", "10", "10", "10", "1.397,24", "100%",
    ]
    res = ps.parse_linhas(linhas)
    assert [i["descricao"] for i in res.itens] == ["DEXTER PLUS 1LT"]
    assert ps.validar(res) == []


def test_agregar_por_codigo_soma_repetidos():
    itens = [
        {"cod_produto": "00004", "descricao": "X", "qtd": Decimal(2), "valor_total": Decimal("10.00")},
        {"cod_produto": "00004", "descricao": "X", "qtd": Decimal(3), "valor_total": Decimal("15.50")},
    ]
    out = ps.agregar_por_codigo(itens)
    assert len(out) == 1 and out[0]["quantidade_saida"] == 5 and out[0]["valor_saida"] == Decimal("25.50")


def test_parse_decimal_brl():
    assert ps.parse_decimal_brl("1.397,24") == Decimal("1397.24")
    assert ps.parse_decimal_brl("100%") == Decimal(100)
    assert ps.parse_decimal_brl("-") is None
    assert ps.parse_decimal_brl("-5,5") == Decimal("-5.5")
