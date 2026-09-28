"""Fluxo do sync sem navegador: PDF real (adaptado para 1 dia) -> parser -> validação -> banco."""
from datetime import date
from decimal import Decimal

import pytest

from estoque import parser_sgi, sync_core
from estoque.sync_core import RelatorioInvalido, dias_a_sincronizar

D0 = date(2026, 9, 21)
D22 = date(2026, 9, 22)


def test_dias_a_sincronizar_primeira_execucao():
    assert dias_a_sincronizar(D22, date(2026, 9, 24), set(), janela=3) == [D22, date(2026, 9, 23), date(2026, 9, 24)]


def test_dias_a_sincronizar_janela_movel_mais_lacunas():
    # feitos: 22,23,25,26,27,28,29  (falta o dia 24); hoje = 30; janela = 3 (28,29,30)
    ja = {date(2026, 9, d) for d in (22, 23, 25, 26, 27, 28, 29)}
    out = dias_a_sincronizar(D22, date(2026, 9, 30), ja, janela=3)
    assert out == [date(2026, 9, 24), date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)]


def test_dias_a_sincronizar_janela_zero_so_faltantes():
    ja = {date(2026, 9, d) for d in range(22, 28)}  # 22..27 feitos
    assert dias_a_sincronizar(D22, date(2026, 9, 30), ja, janela=0) == [
        date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)]


def test_dias_a_sincronizar_futuro_e_vazio():
    assert dias_a_sincronizar(date(2026, 10, 1), date(2026, 9, 24), set()) == []


@pytest.fixture
def pdf_do_dia_22(monkeypatch, pdf_real_bytes):
    """O PDF real cobre 22-24/09; reescrevemos só a linha de período para simular o
    relatório de um único dia (o restante — 40 produtos e o Total — é o real)."""
    linhas = parser_sgi.extrair_linhas_pdf(pdf_real_bytes)
    linhas[0] = "22/09/2026 a 22/09/2026"
    monkeypatch.setattr(parser_sgi, "extrair_linhas_pdf", lambda _b: list(linhas))
    return b"%PDF-fake"


def test_processar_pdf_dia_grava_via_callback(pdf_do_dia_22):
    chamadas = []

    def gravar(loja, dia, linhas, permitir_zerar=False):
        chamadas.append((loja, dia, linhas))
        return {"gravados": len(linhas), "removidos": 0}

    r = sync_core.processar_pdf_dia(pdf_do_dia_22, "Porteira", D22, gravar)
    assert r["n_produtos"] == 40 and r["qtd_total"] == Decimal(126)
    assert chamadas[0][0] == "Porteira" and chamadas[0][1] == D22


def test_dia_errado_nao_grava(pdf_do_dia_22):
    chamadas = []
    with pytest.raises(RelatorioInvalido) as e:
        sync_core.processar_pdf_dia(pdf_do_dia_22, "Porteira", date(2026, 9, 23), lambda *a, **k: chamadas.append(1))
    assert "difere do dia pedido" in str(e.value) and not chamadas


def test_pdf_longo_sem_produtos_reconhecidos_nao_vira_dia_vazio(monkeypatch):
    lixo = ["22/09/2026 a 22/09/2026", "Relatorio Totais de Vendas por Produtos", "Grupos:", " DEFENSIVOS,"] + ["x"] * 60
    monkeypatch.setattr(parser_sgi, "extrair_linhas_pdf", lambda _b: lixo)
    with pytest.raises(RelatorioInvalido):
        sync_core.processar_pdf_dia(b"", "Porteira", D22, lambda *a, **k: {})


def test_pdf_curto_sem_vendas_e_dia_vazio_valido(monkeypatch):
    vazio = ["22/09/2026 a 22/09/2026", "24/09/2026 16:46", "S.G.I. - Sistema", "Relatorio Totais de Vendas por Produtos",
             "Grupos:", " DEFENSIVOS,", "N°", "Cod", "Descrição Produto", "Marca", "Fornecedor", "Posit", "Vendas",
             "Qtd", "Qtd Cx", "Valor Total", "%", "© 2026", "SGI SOLUTION"]
    monkeypatch.setattr(parser_sgi, "extrair_linhas_pdf", lambda _b: vazio)
    gravado = {}
    sync_core.processar_pdf_dia(b"", "Porteira", D22, lambda l, d, linhas, permitir_zerar=False:
                                gravado.update(n=len(linhas)) or {"removidos": 0})
    assert gravado["n"] == 0


def test_sincronizar_dias_isola_falha_de_um_dia(pdf_do_dia_22):
    ok_dias = []

    def baixar(dia):
        if dia == date(2026, 9, 23):
            raise TimeoutError("SGI fora do ar")
        return pdf_do_dia_22

    def gravar(loja, dia, linhas, permitir_zerar=False):
        ok_dias.append(dia)
        return {"gravados": len(linhas), "removidos": 0}

    ok, erros = sync_core.sincronizar_dias("Porteira", [D22, date(2026, 9, 23)], baixar, gravar, log=lambda *_: None)
    # 22 grava; 23 falha no download; nada além disso
    assert ok_dias == [D22] and len(ok) == 1 and len(erros) == 1 and "SGI fora do ar" in erros[0]


def test_fluxo_completo_com_banco(banco, pdf_do_dia_22, base_xlsx_bytes):
    from estoque.importacao import validar_planilha
    imp = validar_planilha(base_xlsx_bytes, "b.xlsx")
    banco.importar_produtos(imp.df, "novos", D0)
    for _, r in imp.ignorados.iterrows():
        banco.ignorar_codigo(r["cod_produto"], r["descricao"], r["motivo"])

    for _ in range(3):  # 3 rodadas do sync para o mesmo dia
        sync_core.processar_pdf_dia(pdf_do_dia_22, "Porteira", D22, banco.substituir_movimentacao_dia)

    epingle = banco.obter_produto("00004")
    assert float(epingle["saldo_atual"]) == 25 - 9         # base 25, vendeu 9 (uma vez só)
    abamex = banco.obter_produto("05519")
    assert float(abamex["saldo_atual"]) == float(abamex["saldo_inicial"])  # não vendeu

    # as 3 vendas de itens da aba "Removidos" estão ignoradas: nada pendente
    assert banco.listar_nao_encontrados().empty
    assert banco.listar_ignorados().shape[0] == 37

    # segunda loja soma
    sync_core.processar_pdf_dia(pdf_do_dia_22, "Casa de Adubo", D22, banco.substituir_movimentacao_dia)
    assert float(banco.obter_produto("00004")["saldo_atual"]) == 25 - 18
