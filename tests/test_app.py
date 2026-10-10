"""Smoke tests funcionais do painel com streamlit.testing (sem navegador), contra Postgres real."""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from estoque import parser_sgi, sync_core
from estoque.importacao import validar_planilha

st_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = st_testing.AppTest

D0, D22 = date(2026, 9, 21), date(2026, 9, 22)
APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def app_semeado(banco, monkeypatch, pdf_real_bytes, base_xlsx_bytes):
    import streamlit as st
    from estoque import paginas
    monkeypatch.delenv("APP_SENHA", raising=False)
    monkeypatch.setattr(paginas, "hoje_brasil", lambda: date(2026, 9, 24))  # determinismo: 3 dias de histórico
    imp = validar_planilha(base_xlsx_bytes, "b.xlsx")
    banco.importar_produtos(imp.df, "novos", D0)
    for _, r in imp.ignorados.iterrows():
        banco.ignorar_codigo(r["cod_produto"], r["descricao"], r["motivo"])
    # vendas do dia 22 (PDF real com a linha de período ajustada) + um código desconhecido
    linhas = parser_sgi.extrair_linhas_pdf(pdf_real_bytes)
    linhas[0] = "22/09/2026 a 22/09/2026"
    monkeypatch.setattr(parser_sgi, "extrair_linhas_pdf", lambda _b: list(linhas))
    sync_core.processar_pdf_dia(b"x", "Porteira", D22, banco.substituir_movimentacao_dia)
    banco.substituir_movimentacao_dia("Casa de Adubo", D22, [
        {"cod_produto": "77777", "descricao": "PRODUTO NOVO XYZ", "quantidade_saida": Decimal(3), "valor_saida": Decimal("90.00")}])
    banco.atualizar_produto("00004", preco_custo=40, preco_venda=60, lead_time_dias=5, estoque_minimo=3)
    st.cache_data.clear(); st.cache_resource.clear()
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    return at


def _ir(at, tela):
    """Navega clicando no item do menu, como a pessoa faz. A navegação virou botões
    agrupados por seção (estoque/navegacao.py); cada um tem key 'nav_<nome da tela>'."""
    at.sidebar.button(key=f"nav_{tela}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _trocar_unidade(at, unidade):
    """O seletor de unidade é um segmented_control; mexer no session_state é o jeito
    estável de acioná-lo no AppTest, que não expõe esse widget."""
    at.session_state["unidade_estoque"] = unidade
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _por_rotulo(elementos, rotulo):
    achados = [e for e in elementos if e.label == rotulo]
    assert achados, f"widget '{rotulo}' não encontrado; existentes: {[e.label for e in elementos]}"
    return achados[0]


def test_todas_as_telas_renderizam_sem_excecao(app_semeado):
    from estoque.paginas import PAGINAS
    assert not app_semeado.exception
    for tela in PAGINAS:
        _ir(app_semeado, tela)


def test_visao_geral_mostra_cartoes(app_semeado):
    _ir(app_semeado, "Visão geral")
    rotulos = [m.label for m in app_semeado.metric]
    assert "Valor do estoque (custo)" in rotulos and "Saldo negativo" in rotulos
    # 3 dias de histórico (< 15): mostra giro/ROI do PERÍODO, sem anualizar
    assert any(r.startswith("Giro no período (3 dia") for r in rotulos)
    assert any(r.startswith("ROI do estoque no período") for r in rotulos)
    assert "Giro anualizado" not in rotulos
    # 00004: base 25, vendeu 9 -> 16 un x R$ 40 (único com custo) = R$ 640,00
    valor = [m.value for m in app_semeado.metric if m.label == "Valor do estoque (custo)"][0]
    assert valor == "R$ 640,00"


def test_nao_encontrados_lista_o_codigo_desconhecido_e_nao_os_ignorados(app_semeado):
    _ir(app_semeado, "Não encontrados")
    pendentes = str(app_semeado.dataframe[0].value.to_dict())  # 1ª tabela = pendentes; a 2ª (expander) = ignorados
    assert "77777" in pendentes and "08452" not in pendentes  # 08452 está na aba 'Removidos'


def test_adicionar_produto_pela_tela_e_desconto_automatico_das_vendas(app_semeado, banco):
    _ir(app_semeado, "Produtos")
    _por_rotulo(app_semeado.text_input, "Código (SGI)").set_value("77777")
    _por_rotulo(app_semeado.text_input, "Descrição").set_value("PRODUTO NOVO XYZ")
    _por_rotulo(app_semeado.number_input, "Saldo na contagem").set_value(20.0)
    app_semeado.run()
    _por_rotulo(app_semeado.button, "Adicionar produto").click().run()
    assert not app_semeado.exception
    # a venda de 3 un em 22/09 (guardada antes do cadastro) já conta: hoje > 22/09? a contagem é 'hoje', então NÃO conta
    p = banco.obter_produto("77777")
    assert p is not None and float(p["saldo_inicial"]) == 20


def test_adicionar_duplicado_mostra_erro(app_semeado):
    _ir(app_semeado, "Produtos")
    _por_rotulo(app_semeado.text_input, "Código (SGI)").set_value("00004")
    _por_rotulo(app_semeado.text_input, "Descrição").set_value("QUALQUER")
    app_semeado.run()
    _por_rotulo(app_semeado.button, "Adicionar produto").click().run()
    assert any("Já existe" in e.value for e in app_semeado.error)


def test_contagem_pela_tela_movimentar(app_semeado, banco):
    _ir(app_semeado, "Movimentar estoque")
    sb = _por_rotulo(app_semeado.selectbox, "Produto")
    sb.set_value("00004")
    app_semeado.run()
    assert float(banco.obter_produto("00004")["saldo_atual"]) == 16
    _por_rotulo(app_semeado.number_input, "Saldo contado (o que existe de fato)").set_value(14.0)
    app_semeado.run()
    _por_rotulo(app_semeado.button, "Registrar").click().run()
    assert not app_semeado.exception and not app_semeado.error
    assert float(banco.obter_produto("00004")["saldo_atual"]) == 14


def test_senha_protege_o_painel(banco, monkeypatch):
    import streamlit as st
    monkeypatch.setenv("APP_SENHA", "segredo")
    st.cache_data.clear(); st.cache_resource.clear()
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert any(t.label == "Senha" for t in at.text_input) and not at.sidebar.radio
    _por_rotulo(at.text_input, "Senha").set_value("errada").run()
    assert any("incorreta" in e.value for e in at.error)
    _por_rotulo(at.text_input, "Senha").set_value("segredo").run()
    # Pela CHAVE de um item do menu: contar widgets quebraria a cada item novo.
    chaves = {b.key for b in at.sidebar.button}
    assert "nav_Visão geral" in chaves, chaves


def test_visao_geral_anualiza_com_historico_suficiente(app_semeado, monkeypatch):
    from estoque import paginas
    monkeypatch.setattr(paginas, "hoje_brasil", lambda: date(2026, 10, 20))  # 29 dias de histórico
    import streamlit as st
    st.cache_data.clear()
    _ir(app_semeado, "Visão geral")
    rotulos = [m.label for m in app_semeado.metric]
    assert "Giro anualizado" in rotulos and "ROI do estoque (anualizado)" in rotulos


def _saldo_na_tela(at, cod):
    """Lê o saldo de um produto na tela 'Saldo por produto'."""
    _ir(at, "Saldo por produto")
    for df in at.dataframe:
        d = df.value
        if "cod_produto" in getattr(d, "columns", []) and "saldo_atual" in d.columns:
            linha = d[d["cod_produto"] == cod]
            if not linha.empty:
                return float(linha.iloc[0]["saldo_atual"])
    raise AssertionError(f"produto {cod} não apareceu na tela de saldo")


def test_seletor_de_unidade_muda_o_saldo_mostrado(banco, monkeypatch):
    """A transferência tira da matriz e põe em Piatã, e o painel tem que mostrar isso
    nas três visões. É o teste de ponta a ponta do pedido: sem ele, o seletor poderia
    estar só trocando um rótulo."""
    import streamlit as st

    from estoque import paginas
    monkeypatch.delenv("APP_SENHA", raising=False)
    monkeypatch.setattr(paginas, "hoje_brasil", lambda: date(2026, 9, 30))
    banco.criar_produto("00001", "PRODUTO 1", 100, D0)
    dia = date(2026, 9, 23)
    banco.substituir_transferencias_periodo("PORTEIRA PIATA", dia, dia, {dia: [
        {"cod_produto": "00001", "descricao": "PRODUTO 1",
         "quantidade_transferida": 30, "valor_transferido": 0}]})
    st.cache_data.clear(); st.cache_resource.clear()
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]

    _trocar_unidade(at, "Barra da Estiva")
    assert _saldo_na_tela(at, "00001") == 70

    _trocar_unidade(at, "Piatã")
    assert _saldo_na_tela(at, "00001") == 30

    _trocar_unidade(at, paginas.CONSOLIDADO)
    # Mercadoria mudou de lugar, não saiu do grupo.
    assert _saldo_na_tela(at, "00001") == 100
