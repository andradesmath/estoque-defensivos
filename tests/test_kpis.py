"""Valores esperados calculados À MÃO (ver comentários), não por reexecução da fórmula."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from estoque import kpis

HOJE = date(2026, 9, 30)


def _produtos(**over):
    base = {
        "cod_produto": "00001", "descricao": "PRODUTO A", "unidade": "UN",
        "saldo_inicial": 100, "data_saldo_inicial": date(2026, 9, 21),
        "preco_custo": 10.0, "preco_venda": 15.0, "lead_time_dias": 5, "estoque_minimo": 10, "ativo": True,
    }
    base.update(over)
    return pd.DataFrame([base])


MOV = pd.DataFrame([
    {"cod_produto": "00001", "data": date(2026, 9, 22), "quantidade_saida": 10, "valor_saida": 150.0},
    {"cod_produto": "00001", "data": date(2026, 9, 25), "quantidade_saida": 20, "valor_saida": 290.0},
])
AJ = pd.DataFrame([{"cod_produto": "00001", "data": date(2026, 9, 27), "quantidade": 50}])


def test_serie_saldo_diaria():
    s = kpis.serie_saldo_diaria(_produtos(), MOV, AJ, HOJE).set_index("data")["saldo"]
    # fim de dia: 21:100 | 22:90 | 23:90 | 24:90 | 25:70 | 26:70 | 27:120 | 28..30:120
    assert s[pd.Timestamp("2026-09-21")] == 100
    assert s[pd.Timestamp("2026-09-22")] == 90
    assert s[pd.Timestamp("2026-09-25")] == 70
    assert s[pd.Timestamp("2026-09-30")] == 120


def test_movimentos_ate_a_data_da_contagem_sao_ignorados():
    mov = pd.concat([MOV, pd.DataFrame([{"cod_produto": "00001", "data": date(2026, 9, 21),
                                          "quantidade_saida": 999, "valor_saida": 1.0}])])
    s = kpis.serie_saldo_diaria(_produtos(), mov, AJ, HOJE).set_index("data")["saldo"]
    assert s[pd.Timestamp("2026-09-30")] == 120


def test_indicadores_calculo_manual():
    r = kpis.indicadores(_produtos(), MOV, AJ, HOJE, janela_dias=30).iloc[0]
    # período: 22/09..30/09 = 9 dias (a contagem foi em 21/09)
    assert r["dias_periodo"] == 9
    assert r["saldo_atual"] == 120
    assert r["saidas_qtd"] == 30 and r["receita"] == 440.0
    assert r["venda_media_dia"] == pytest.approx(30 / 9)                       # 3,3333
    assert r["cobertura_dias"] == pytest.approx(36.0)                          # 120 / 3,3333
    assert r["estoque_medio_qtd"] == pytest.approx(890 / 9)                    # (90*3+70*2+120*4)/9 = 98,889
    assert r["valor_estoque_custo"] == 1200.0 and r["valor_estoque_venda"] == 1800.0
    assert r["lucro_potencial"] == 600.0 and r["margem_lista_pct"] == pytest.approx(100 * 5 / 15)
    assert r["cmv"] == 300.0 and r["lucro_bruto"] == 140.0
    assert r["margem_realizada_pct"] == pytest.approx(140 / 440 * 100)
    assert r["estoque_medio_valor"] == pytest.approx(988.8889, rel=1e-4)
    assert r["giro_periodo"] == pytest.approx(300 / 988.8889, rel=1e-4)        # 0,30337
    assert r["giro_anual"] == pytest.approx(0.30337 * 365 / 9, rel=1e-3)       # 12,30
    assert r["dias_de_giro"] == pytest.approx(9 / 0.30337, rel=1e-3)           # 29,67
    assert r["roi_periodo"] == pytest.approx(140 / 988.8889, rel=1e-4)         # 0,14157
    assert r["roi_anual"] == pytest.approx(0.14157 * 365 / 9, rel=1e-3)        # 5,742
    assert r["ponto_pedido"] == pytest.approx(30 / 9 * 5 + 10)                 # 26,67
    assert r["sugestao_compra"] == 0                                           # 3,33*35 - 120 < 0
    assert r["situacao"] == "OK"
    assert r["curva_abc"] == "A"


def test_sugestao_de_compra_e_situacao_repor():
    ajustes_vazio = pd.DataFrame(columns=["cod_produto", "data", "quantidade"])
    # saldo cai a 70; venda média 3,33/dia; ponto = 26,67 -> ainda OK. Elevamos o mínimo:
    p = _produtos(estoque_minimo=70)
    r = kpis.indicadores(p, MOV, ajustes_vazio, HOJE).iloc[0]
    assert r["saldo_atual"] == 70 and r["situacao"] == "Repor"
    # (5 + 30) * 3,3333 - 70 = 46,67 -> arredonda p/ cima = 47
    assert r["sugestao_compra"] == 47


@pytest.mark.parametrize("saldo_ini,esperado", [(-3, "Negativo"), (0, "Ruptura")])
def test_situacao_negativo_e_ruptura(saldo_ini, esperado):
    mov = pd.DataFrame(columns=["cod_produto", "data", "quantidade_saida", "valor_saida"])
    aj = pd.DataFrame(columns=["cod_produto", "data", "quantidade"])
    r = kpis.indicadores(_produtos(saldo_inicial=saldo_ini), mov, aj, HOJE).iloc[0]
    assert r["situacao"] == esperado


def test_sem_giro_so_apos_dias_minimos():
    mov = pd.DataFrame(columns=["cod_produto", "data", "quantidade_saida", "valor_saida"])
    aj = pd.DataFrame(columns=["cod_produto", "data", "quantidade"])
    p = _produtos(lead_time_dias=None, estoque_minimo=None)
    assert kpis.indicadores(p, mov, aj, HOJE, dias_sem_giro=30).iloc[0]["situacao"] == "OK"       # só 9 dias
    assert kpis.indicadores(p, mov, aj, HOJE, dias_sem_giro=7).iloc[0]["situacao"] == "Sem giro"


def test_excesso_de_estoque():
    aj = pd.DataFrame(columns=["cod_produto", "data", "quantidade"])
    mov = pd.DataFrame([{"cod_produto": "00001", "data": date(2026, 9, 22), "quantidade_saida": 9, "valor_saida": 90.0}])
    p = _produtos(saldo_inicial=1000, lead_time_dias=None, estoque_minimo=None)
    r = kpis.indicadores(p, mov, aj, HOJE, excesso_dias=120).iloc[0]
    # venda média 1/dia, saldo 991 -> cobertura 991 dias
    assert r["cobertura_dias"] == pytest.approx(991) and r["situacao"] == "Excesso"


def test_sem_custo_nao_quebra_e_fica_fora_do_resumo():
    p = _produtos(preco_custo=None)
    ind = kpis.indicadores(p, MOV, AJ, HOJE)
    r = ind.iloc[0]
    assert bool(r["sem_custo"]) and np.isnan(r["valor_estoque_custo"]) and np.isnan(r["giro_anual"])
    res = kpis.resumo_geral(ind)
    assert res["n_sem_custo"] == 1 and res["valor_estoque_custo"] == 0


def test_contagem_hoje_sem_periodo_nao_gera_divisao_por_zero():
    p = _produtos(data_saldo_inicial=HOJE)
    r = kpis.indicadores(p, MOV, AJ, HOJE).iloc[0]
    assert r["dias_periodo"] == 0 and np.isnan(r["venda_media_dia"]) and r["saldo_atual"] == 100


def test_curva_abc():
    receita = pd.Series([800.0, 100.0, 60.0, 30.0, 10.0, 0.0], index=list("abcdef"))
    abc = kpis._classe_abc(receita)
    # acumulado antes: a=0% (A) | b=80% (B) | c=90% (B) | d=96% (C) | e=99% (C) | f sem venda
    assert list(abc) == ["A", "B", "B", "C", "C", "—"]


def test_resumo_geral_dois_produtos():
    p = pd.concat([_produtos(), _produtos(cod_produto="00002", descricao="B", saldo_inicial=50, preco_custo=4.0,
                                          preco_venda=6.0, lead_time_dias=None, estoque_minimo=None)])
    mov = pd.concat([MOV, pd.DataFrame([{"cod_produto": "00002", "data": date(2026, 9, 23), "quantidade_saida": 5, "valor_saida": 30.0}])])
    ind = kpis.indicadores(p, mov, AJ, HOJE)
    res = kpis.resumo_geral(ind)
    # A: 120 x 10 = 1200 | B: (50-5) x 4 = 180
    assert res["valor_estoque_custo"] == pytest.approx(1380.0)
    assert res["cmv_periodo"] == pytest.approx(300 + 5 * 4)
    assert res["receita_periodo"] == pytest.approx(440 + 30)
    assert res["n_produtos"] == 2 and res["n_ruptura"] == 0
    # estoque médio a custo: A = 98,889 x 10 = 988,89 | B = (50 + 8 x 45)/9 x 4 = 182,22 -> 1171,11
    # lucro bruto: A = 440 - 300 = 140 | B = 30 - 20 = 10 -> 150
    assert res["roi_periodo"] == pytest.approx(150 / 1171.111, rel=1e-4)
    assert res["giro_periodo"] == pytest.approx(320 / 1171.111, rel=1e-4)
    assert res["roi_anual"] == pytest.approx(150 / 1171.111 * 365 / 9, rel=1e-4)


def test_evolucao_valor_estoque():
    ev = kpis.evolucao_valor_estoque(_produtos(), MOV, AJ, HOJE).set_index("data")["valor"]
    assert ev[pd.Timestamp("2026-09-21")] == 1000.0 and ev[pd.Timestamp("2026-09-30")] == 1200.0
