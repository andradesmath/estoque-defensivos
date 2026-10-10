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
