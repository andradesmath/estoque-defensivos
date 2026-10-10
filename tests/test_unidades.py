"""Estoque por UNIDADE (Barra da Estiva e Piatã).

O que estes testes fixam, e que é a razão de a view ter sido reescrita: a
transferência DEBITA a matriz e CREDITA a filial. Antes ela só debitava, porque Piatã
não existia como estoque aqui - o produto simplesmente sumia do controle.
"""
from datetime import date

import pytest

MATRIZ, FILIAL = "Barra da Estiva", "Piatã"
D0 = date(2026, 9, 21)    # contagem da matriz
D1 = date(2026, 9, 23)
D2 = date(2026, 9, 25)


def _saldo(db, cod, unidade):
    df = db.saldo_por_unidade(unidade)
    linha = df[df["cod_produto"] == cod]
    assert not linha.empty, f"{cod} não apareceu na unidade {unidade}"
    return float(linha.iloc[0]["saldo_atual"])


def _transferir(db, cod, dia, qtd, destino="PORTEIRA PIATA"):
    db.substituir_transferencias_periodo(destino, dia, dia, {dia: [
        {"cod_produto": cod, "descricao": "P", "quantidade_transferida": qtd,
         "valor_transferido": 0},
    ]})


@pytest.fixture
def base(banco):
    banco.criar_produto("00001", "PRODUTO 1", 100, D0)
    return banco


def test_transferencia_sai_da_matriz_e_entra_na_filial(base):
    _transferir(base, "00001", D1, 30)
    assert _saldo(base, "00001", MATRIZ) == 70
    assert _saldo(base, "00001", FILIAL) == 30


def test_consolidado_nao_muda_com_transferencia(base):
    antes = float(base.obter_produto("00001")["saldo_atual"])
    _transferir(base, "00001", D1, 30)
    # Mercadoria mudou de lugar, não saiu do grupo: o consolidado tem que ficar igual.
    assert float(base.obter_produto("00001")["saldo_atual"]) == antes


def test_venda_na_filial_desconta_so_da_filial(base):
    _transferir(base, "00001", D1, 30)
    base.substituir_movimentacao_dia("Piatã", D2, [
        {"cod_produto": "00001", "descricao": "P", "quantidade_saida": 12, "valor_saida": 0},
    ])
    assert _saldo(base, "00001", FILIAL) == 18
    assert _saldo(base, "00001", MATRIZ) == 70


def test_venda_na_matriz_nao_toca_na_filial(base):
    _transferir(base, "00001", D1, 30)
    base.substituir_movimentacao_dia("Porteira", D2, [
        {"cod_produto": "00001", "descricao": "P", "quantidade_saida": 5, "valor_saida": 0},
    ])
    assert _saldo(base, "00001", MATRIZ) == 65
    assert _saldo(base, "00001", FILIAL) == 30


def test_casa_de_adubo_continua_no_mesmo_estoque_da_porteira(base):
    """As duas lojas de Barra da Estiva dividem o estoque físico - era assim antes das
    unidades e tem que continuar sendo."""
    base.substituir_movimentacao_dia("Casa de Adubo", D2, [
        {"cod_produto": "00001", "descricao": "P", "quantidade_saida": 4, "valor_saida": 0},
    ])
    assert _saldo(base, "00001", MATRIZ) == 96


def test_ajuste_da_filial_nao_afeta_a_matriz(base):
    _transferir(base, "00001", D1, 30)
    base.registrar_ajuste("00001", D2, "perda", 3, unidade_estoque=FILIAL)
    assert _saldo(base, "00001", FILIAL) == 27
    assert _saldo(base, "00001", MATRIZ) == 70


def test_ajuste_sem_unidade_cai_na_matriz(base):
    """Compatibilidade: todo ajuste lançado antes das unidades era da matriz, e o
    painel antigo chama registrar_ajuste sem informar unidade."""
    base.registrar_ajuste("00001", D2, "perda", 3)
    assert _saldo(base, "00001", MATRIZ) == 97
    assert _saldo(base, "00001", FILIAL) == 0


def test_contagem_propria_da_filial_vira_o_marco_zero(base):
    """Piatã conta em outra data, e o que veio ANTES da contagem dela já está dentro do
    número contado - não pode ser somado de novo."""
    _transferir(base, "00001", D1, 30)          # antes da contagem da filial
    base.definir_saldo_inicial_unidade("00001", FILIAL, 25, D1)
    assert _saldo(base, "00001", FILIAL) == 25  # e não 25 + 30
    _transferir(base, "00001", D2, 7)           # depois da contagem: soma
    assert _saldo(base, "00001", FILIAL) == 32


def test_produto_aparece_em_toda_unidade_mesmo_sem_contagem(base):
    """Sem isto, um item transferido para Piatã antes de ser contado lá não teria linha
    nenhuma na filial e a transferência sumiria do consolidado."""
    unidades = {u["nome"] for u in base.listar_unidades()}
    assert unidades == {MATRIZ, FILIAL}
    for u in unidades:
        assert not base.saldo_por_unidade(u).empty


def test_aplicar_schema_por_partes_cria_tudo_e_repete_sem_erro(banco):
    """Autocura do painel: aplicar o schema comando a comando tem que criar o que
    falta E poder rodar de novo sem quebrar. É o caminho que conserta o banco quando
    ele fica atrás do código (o @st.cache_resource do init_schema não roda de novo num
    recarregamento sem reinício de processo)."""
    from sqlalchemy import text

    with banco.get_engine().begin() as c:
        c.execute(text("DROP VIEW IF EXISTS v_saldo_produto"))
        c.execute(text("DROP VIEW IF EXISTS v_saldo_produto_unidade"))
        c.execute(text("DROP TABLE IF EXISTS unidades_loja"))
        c.execute(text("DROP TABLE IF EXISTS saldo_inicial_unidade"))
        c.execute(text("DROP TABLE IF EXISTS unidades CASCADE"))
    with pytest.raises(Exception):
        banco.listar_unidades()

    assert banco.aplicar_schema_por_partes() > 0
    assert {u["nome"] for u in banco.listar_unidades()} == {MATRIZ, FILIAL}
    banco.aplicar_schema_por_partes()  # idempotente
    assert {u["nome"] for u in banco.listar_unidades()} == {MATRIZ, FILIAL}


def test_consolidado_do_painel_bate_com_a_view(banco, monkeypatch):
    """O painel calcula o saldo em pandas (kpis) e a view calcula em SQL. Os dois têm
    que dar o mesmo número - quando divergem, quem olha a tela decide errado.

    Este teste existe por um bug real: o consolidado do painel ignorava o saldo inicial
    contado de Piatã (lia produtos.saldo_inicial, que é só o da matriz) e as duas pontas
    da transferência se anulavam, então o total ficava igual ao da matriz. Dava 100 onde
    a view dava 95."""
    from estoque import paginas
    monkeypatch.setattr(paginas, "hoje_brasil", lambda: date(2026, 10, 15))
    banco.criar_produto("00001", "PRODUTO 1", 100, D0)
    _transferir(banco, "00001", D1, 30)
    banco.definir_saldo_inicial_unidade("00001", FILIAL, 25, date(2026, 10, 1))

    def saldo_painel(unidade):
        paginas.carregar_base.clear()
        ind = paginas.calcular_indicadores(unidade=unidade)
        return float(ind[ind["cod_produto"] == "00001"].iloc[0]["saldo_atual"])

    assert saldo_painel(MATRIZ) == 70
    assert saldo_painel(FILIAL) == 25
    assert saldo_painel(paginas.CONSOLIDADO) == 95
    assert float(banco.obter_produto("00001")["saldo_atual"]) == 95  # a view concorda


def test_consolidado_nao_conta_duas_vezes_o_que_foi_transferido_antes_da_contagem(banco, monkeypatch):
    """A transferência de 03/10 já está dentro das 25 contadas em Piatã no dia 10. Somar
    as unidades com UM corte de data só a contaria de novo - é por isso que o
    consolidado soma as séries por unidade, cada uma com o seu corte."""
    from estoque import paginas
    monkeypatch.setattr(paginas, "hoje_brasil", lambda: date(2026, 10, 15))
    banco.criar_produto("00001", "PRODUTO 1", 100, D0)
    _transferir(banco, "00001", date(2026, 10, 3), 30)
    banco.definir_saldo_inicial_unidade("00001", FILIAL, 25, date(2026, 10, 10))
    paginas.carregar_base.clear()
    ind = paginas.calcular_indicadores(unidade=paginas.CONSOLIDADO)
    # 70 na matriz + 25 contados em Piatã. Nada de 100 nem de 125.
    assert float(ind[ind["cod_produto"] == "00001"].iloc[0]["saldo_atual"]) == 95


def test_sync_comeca_na_data_da_contagem_da_unidade(banco):
    """Venda anterior à contagem da unidade já está dentro do número contado: buscar
    antes disso é baixar relatório que não altera saldo nenhum. Piatã foi contada em
    10/10 - sem esta regra, a primeira execução baixaria 19 dias à toa."""
    import importlib.util
    from pathlib import Path

    raiz = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("sync_sgi", raiz / "scripts" / "sync_sgi.py")
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)

    padrao = date(2026, 9, 22)
    banco.criar_produto("00001", "PRODUTO 1", 100, D0)
    # Matriz não tem contagem própria (herda a do cadastro): segue o padrão.
    assert sync._inicio_da_loja("Porteira", padrao) == padrao
    assert sync._inicio_da_loja("Piatã", padrao) == padrao   # ainda sem contagem

    banco.definir_saldo_inicial_unidade("00001", FILIAL, 20, date(2026, 10, 10))
    # +1 porque a contagem vale para o FIM do dia.
    assert sync._inicio_da_loja("Piatã", padrao) == date(2026, 10, 11)
    assert sync._inicio_da_loja("Porteira", padrao) == padrao  # a outra não muda

    # Contagem mais ANTIGA que o padrão não faz o sync voltar no tempo.
    banco.definir_saldo_inicial_unidade("00001", FILIAL, 20, date(2026, 9, 1))
    assert sync._inicio_da_loja("Piatã", padrao) == padrao
