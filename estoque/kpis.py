"""
estoque/kpis.py - Indicadores de estoque (funções puras sobre DataFrames; sem banco).

Entradas (as mesmas tabelas do banco, já como DataFrame):
  produtos : cod_produto, descricao, unidade, saldo_inicial, data_saldo_inicial,
             preco_custo, preco_venda, lead_time_dias, estoque_minimo, ativo
  mov      : cod_produto, data, quantidade_saida, valor_saida   (somado entre lojas)
  ajustes  : cod_produto, data, quantidade (assinada)

Definições (todas no PERÍODO de análise = últimos `janela_dias` dias, cortado no dia
seguinte à contagem inicial de cada produto):

  venda_media_dia   = unidades vendidas / dias do período
  cobertura_dias    = saldo_atual / venda_media_dia            (quanto tempo dura o que tenho)
  estoque_medio_qtd = média dos saldos de fim de dia no período
  cmv               = unidades vendidas x preço de custo
  receita           = soma de valor_saida do SGI (preço realmente praticado)
  lucro_bruto       = receita - cmv
  estoque_medio_val = estoque_medio_qtd x preço de custo
  giro_periodo      = cmv / estoque_medio_val                  (vezes que o estoque "virou")
  giro_anual        = giro_periodo x 365 / dias
  dias_de_giro      = dias / giro_periodo                      (prazo médio de permanência)
  roi_periodo       = lucro_bruto / estoque_medio_val          (GMROI: retorno do capital em estoque)
  roi_anual         = roi_periodo x 365 / dias                 (anualização linear, não composta)
  ponto_pedido      = venda_media_dia x lead_time + estoque_minimo
  sugestao_compra   = venda_media_dia x (lead_time + cobertura_alvo) - saldo   (>= 0, arredonda p/ cima)

Limites: com poucos dias de histórico giro/ROI anualizados são instáveis (a coluna
`dias_periodo` existe para o painel avisar); estoque médio usa saldos de fim de dia;
custo é o cadastrado hoje (não há custo histórico); receita depende de o relatório do
SGI trazer valor líquido de descontos (a confirmar).
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

NUMERICAS_PRODUTO = ["saldo_inicial", "preco_custo", "preco_venda", "lead_time_dias", "estoque_minimo"]
COLUNAS_INDICADORES = [
    "cod_produto", "descricao", "unidade", "ativo", "saldo_atual", "dias_periodo", "saidas_qtd", "receita",
    "venda_media_dia", "cobertura_dias", "estoque_medio_qtd", "preco_custo", "preco_venda",
    "valor_estoque_custo", "valor_estoque_venda", "lucro_potencial", "margem_lista_pct",
    "cmv", "lucro_bruto", "margem_realizada_pct", "estoque_medio_valor", "giro_periodo",
    "giro_anual", "dias_de_giro", "roi_periodo", "roi_anual", "lead_time_dias", "estoque_minimo",
    "ponto_pedido", "sugestao_compra", "situacao", "curva_abc", "sem_custo",
]


def _prep_produtos(produtos: pd.DataFrame) -> pd.DataFrame:
    p = produtos.copy()
    for c in NUMERICAS_PRODUTO:
        p[c] = pd.to_numeric(p[c], errors="coerce") if c in p else np.nan
    p["data_saldo_inicial"] = pd.to_datetime(p["data_saldo_inicial"])
    if "ativo" not in p:
        p["ativo"] = True
    if "unidade" not in p:
        p["unidade"] = None
    return p


def _prep_mov(mov: pd.DataFrame) -> pd.DataFrame:
    m = mov.copy()
    if m.empty:
        return pd.DataFrame({"cod_produto": pd.Series(dtype="object"), "data": pd.Series(dtype="datetime64[ns]"),
                             "quantidade_saida": pd.Series(dtype="float64"), "valor_saida": pd.Series(dtype="float64")})
    m["data"] = pd.to_datetime(m["data"])
    m["quantidade_saida"] = pd.to_numeric(m["quantidade_saida"], errors="coerce").fillna(0.0)
    m["valor_saida"] = pd.to_numeric(m["valor_saida"], errors="coerce").fillna(0.0)
    return m.groupby(["cod_produto", "data"], as_index=False)[["quantidade_saida", "valor_saida"]].sum()


def _prep_ajustes(ajustes: pd.DataFrame) -> pd.DataFrame:
    a = ajustes.copy()
    if a.empty:
        return pd.DataFrame({"cod_produto": pd.Series(dtype="object"), "data": pd.Series(dtype="datetime64[ns]"),
                             "quantidade": pd.Series(dtype="float64")})
    a["data"] = pd.to_datetime(a["data"])
    a["quantidade"] = pd.to_numeric(a["quantidade"], errors="coerce").fillna(0.0)
    return a.groupby(["cod_produto", "data"], as_index=False)[["quantidade"]].sum()


def _prep_entradas(entradas) -> pd.DataFrame:
    """Entradas por COMPRA (movimentacao_entrada_compra) agregadas por produto+dia.

    Aceita None pra manter compatibilidade com quem chama sem elas (e com os testes
    antigos): nesse caso devolve vazio, que soma zero."""
    vazio = pd.DataFrame({"cod_produto": pd.Series(dtype="object"),
                          "data": pd.Series(dtype="datetime64[ns]"),
                          "quantidade_entrada": pd.Series(dtype="float64")})
    if entradas is None or len(entradas) == 0:
        return vazio
    e = entradas.copy()
    e["data"] = pd.to_datetime(e["data"])
    e["quantidade_entrada"] = pd.to_numeric(e["quantidade_entrada"], errors="coerce").fillna(0.0)
    return e.groupby(["cod_produto", "data"], as_index=False)[["quantidade_entrada"]].sum()


def _prep_transferencias(transferencias) -> pd.DataFrame:
    """Transferências de SAÍDA (movimentacao_transferencia) agregadas por produto+dia.
    Opcional, como as entradas por compra - None devolve vazio, que soma zero."""
    vazio = pd.DataFrame({"cod_produto": pd.Series(dtype="object"),
                          "data": pd.Series(dtype="datetime64[ns]"),
                          "quantidade_transferida": pd.Series(dtype="float64")})
    if transferencias is None or len(transferencias) == 0:
        return vazio
    t = transferencias.copy()
    t["data"] = pd.to_datetime(t["data"])
    t["quantidade_transferida"] = pd.to_numeric(t["quantidade_transferida"], errors="coerce").fillna(0.0)
    return t.groupby(["cod_produto", "data"], as_index=False)[["quantidade_transferida"]].sum()


def serie_saldo_diaria(produtos: pd.DataFrame, mov: pd.DataFrame, ajustes: pd.DataFrame,
                       hoje: date, entradas=None, transferencias=None,
                       transferencias_entrada=None) -> pd.DataFrame:
    """Saldo de FIM de dia por produto, de data_saldo_inicial até `hoje`. No dia da
    contagem o saldo é o saldo_inicial; movimentos com data <= contagem são ignorados
    (mesma regra de v_saldo_produto).

    `entradas` são as ENTRADAS POR COMPRA (movimentacao_entrada_compra, do robô do SGI
    desktop). Sem elas o painel mostrava saldo menor que o real: o JOINER aparecia
    com -2 em 09/10/2026 (0 inicial + 1 de ajuste - 3 vendidos) enquanto a compra de
    12 estava gravada e a view v_saldo_produto já devolvia 10 corretamente - a conta
    aqui é que ignorava as compras.

    `transferencias` é o que SAIU desta unidade e `transferencias_entrada` o que ENTROU
    nela (a mesma transferência é as duas coisas, vista das duas pontas). Na visão de
    Piatã é a entrada que forma o estoque; na matriz, só a saída. No consolidado as
    duas se anulam, como tem que ser - a mercadoria mudou de lugar, não saiu do
    grupo."""
    p = _prep_produtos(produtos)
    m = _prep_mov(mov)
    a = _prep_ajustes(ajustes)
    ent = _prep_entradas(entradas)
    tra = _prep_transferencias(transferencias)
    tre = _prep_transferencias(transferencias_entrada).rename(
        columns={"quantidade_transferida": "quantidade_transferida_entrada"})
    hoje_ts = pd.Timestamp(hoje)

    frames = []
    for r in p[["cod_produto", "saldo_inicial", "data_saldo_inicial"]].itertuples(index=False):
        if r.data_saldo_inicial > hoje_ts:
            continue
        idx = pd.date_range(r.data_saldo_inicial, hoje_ts, freq="D")
        frames.append(pd.DataFrame({"cod_produto": r.cod_produto, "data": idx,
                                    "saldo_inicial": r.saldo_inicial, "d0": r.data_saldo_inicial}))
    if not frames:
        return pd.DataFrame(columns=["cod_produto", "data", "saldo"])
    g = pd.concat(frames, ignore_index=True)
    g = g.merge(m[["cod_produto", "data", "quantidade_saida"]], on=["cod_produto", "data"], how="left")
    g = g.merge(a, on=["cod_produto", "data"], how="left")
    g = g.merge(ent, on=["cod_produto", "data"], how="left")
    g = g.merge(tra, on=["cod_produto", "data"], how="left")
    g = g.merge(tre, on=["cod_produto", "data"], how="left")
    cols = ["quantidade_saida", "quantidade", "quantidade_entrada", "quantidade_transferida",
            "quantidade_transferida_entrada"]
    g[cols] = g[cols].fillna(0.0)
    delta = (g["quantidade"] + g["quantidade_entrada"] + g["quantidade_transferida_entrada"]
             - g["quantidade_saida"] - g["quantidade_transferida"])
    g["delta"] = np.where(g["data"] > g["d0"], delta, 0.0)
    g = g.sort_values(["cod_produto", "data"])
    g["saldo"] = g["saldo_inicial"] + g.groupby("cod_produto")["delta"].cumsum()
    return g[["cod_produto", "data", "saldo"]].reset_index(drop=True)


def _classe_abc(receita: pd.Series, corte_a: float = 0.80, corte_b: float = 0.95) -> pd.Series:
    out = pd.Series("—", index=receita.index, dtype="object")
    pos = receita[receita > 0].sort_values(ascending=False)
    if pos.empty:
        return out
    acum_antes = pos.cumsum().shift(fill_value=0.0) / pos.sum()  # % acumulado ANTES do item
    # um item pertence à classe A enquanto o acumulado anterior ainda não atingiu 80%
    classe = np.where(acum_antes < corte_a, "A", np.where(acum_antes < corte_b, "B", "C"))
    out.loc[pos.index] = classe
    return out


def indicadores(
    produtos: pd.DataFrame,
    mov: pd.DataFrame,
    ajustes: pd.DataFrame,
    hoje: date,
    janela_dias: int = 30,
    cobertura_alvo: int = 30,
    excesso_dias: int = 120,
    dias_sem_giro: int = 30,
    entradas=None,
    transferencias=None,
    transferencias_entrada=None,
) -> pd.DataFrame:
    p = _prep_produtos(produtos).reset_index(drop=True)
    if p.empty:
        return pd.DataFrame(columns=COLUNAS_INDICADORES)
    m = _prep_mov(mov)
    hoje_ts = pd.Timestamp(hoje)
    serie = serie_saldo_diaria(produtos, mov, ajustes, hoje, entradas, transferencias,
                               transferencias_entrada)

    ini_global = hoje_ts - pd.Timedelta(days=janela_dias - 1)
    p["inicio_periodo"] = p["data_saldo_inicial"].apply(lambda d0: max(ini_global, d0 + pd.Timedelta(days=1)))
    p["dias_periodo"] = ((hoje_ts - p["inicio_periodo"]).dt.days + 1).clip(lower=0)

    # vendas no período de cada produto
    mm = m.merge(p[["cod_produto", "inicio_periodo"]], on="cod_produto", how="inner")
    mm = mm[(mm["data"] >= mm["inicio_periodo"]) & (mm["data"] <= hoje_ts)]
    vend = mm.groupby("cod_produto")[["quantidade_saida", "valor_saida"]].sum().rename(
        columns={"quantidade_saida": "saidas_qtd", "valor_saida": "receita"})

    # saldo atual e estoque médio (fim de dia) no período
    sr = serie.merge(p[["cod_produto", "inicio_periodo"]], on="cod_produto", how="inner")
    saldo_atual = sr[sr["data"] == hoje_ts].set_index("cod_produto")["saldo"].rename("saldo_atual")
    em = sr[sr["data"] >= sr["inicio_periodo"]].groupby("cod_produto")["saldo"].mean().rename("estoque_medio_qtd")

    d = p.set_index("cod_produto").join([vend, saldo_atual, em])
    d["saidas_qtd"] = d["saidas_qtd"].fillna(0.0)
    d["receita"] = d["receita"].fillna(0.0)
    d["saldo_atual"] = d["saldo_atual"].fillna(d["saldo_inicial"])  # contagem feita hoje ou depois

    dias = d["dias_periodo"].replace(0, np.nan)
    custo, pv = d["preco_custo"], d["preco_venda"]
    d["venda_media_dia"] = d["saidas_qtd"] / dias
    d["cobertura_dias"] = np.where(d["venda_media_dia"] > 0, d["saldo_atual"].clip(lower=0) / d["venda_media_dia"], np.nan)

    d["valor_estoque_custo"] = d["saldo_atual"].clip(lower=0) * custo
    d["valor_estoque_venda"] = d["saldo_atual"].clip(lower=0) * pv
    d["lucro_potencial"] = d["valor_estoque_venda"] - d["valor_estoque_custo"]
    d["margem_lista_pct"] = np.where(pv > 0, (pv - custo) / pv * 100, np.nan)

    d["cmv"] = d["saidas_qtd"] * custo
    d["lucro_bruto"] = d["receita"] - d["cmv"]
    d["margem_realizada_pct"] = np.where((d["receita"] > 0) & custo.notna(), d["lucro_bruto"] / d["receita"] * 100, np.nan)
    d["estoque_medio_valor"] = d["estoque_medio_qtd"].clip(lower=0) * custo

    val_ok = d["estoque_medio_valor"] > 0
    d["giro_periodo"] = np.where(val_ok & custo.notna(), d["cmv"] / d["estoque_medio_valor"], np.nan)
    d["giro_anual"] = d["giro_periodo"] * 365 / dias
    d["dias_de_giro"] = np.where(d["giro_periodo"] > 0, dias / d["giro_periodo"], np.nan)
    d["roi_periodo"] = np.where(val_ok & custo.notna(), d["lucro_bruto"] / d["estoque_medio_valor"], np.nan)
    d["roi_anual"] = d["roi_periodo"] * 365 / dias

    lead, minimo = d["lead_time_dias"], d["estoque_minimo"]
    consumo_lead = d["venda_media_dia"] * lead
    d["ponto_pedido"] = np.where(lead.notna(), consumo_lead.fillna(0) + minimo.fillna(0),
                                 np.where(minimo.notna(), minimo, np.nan))
    sug = d["venda_media_dia"] * (lead + cobertura_alvo) - d["saldo_atual"]
    d["sugestao_compra"] = np.where(lead.notna() & (d["venda_media_dia"] > 0),
                                    np.ceil(np.maximum(sug, 0).fillna(0)), np.nan)

    saldo = d["saldo_atual"]
    situacao = np.select(
        [
            saldo < 0,
            saldo == 0,
            d["ponto_pedido"].notna() & (d["ponto_pedido"] > 0) & (saldo <= d["ponto_pedido"]),
            (saldo > 0) & (d["saidas_qtd"] == 0) & (d["dias_periodo"] >= dias_sem_giro),
            d["cobertura_dias"] > excesso_dias,
        ],
        ["Negativo", "Ruptura", "Repor", "Sem giro", "Excesso"],
        default="OK",
    )
    d["situacao"] = situacao
    d["curva_abc"] = _classe_abc(d["receita"])
    d["sem_custo"] = custo.isna()

    return d[COLUNAS_INDICADORES[1:]].reset_index()


def resumo_geral(ind: pd.DataFrame) -> dict:
    """Totais para os cartões do painel. Só produtos ATIVOS e com custo entram nas
    métricas de valor; `sem_custo` informa quantos ficaram de fora."""
    a = ind[ind["ativo"]] if "ativo" in ind else ind
    com_custo = a[~a["sem_custo"]]
    dias = float(a["dias_periodo"].max()) if len(a) else 0.0
    cmv = float(com_custo["cmv"].sum())
    em_val = float(com_custo["estoque_medio_valor"].sum())
    lucro = float(com_custo["lucro_bruto"].sum())
    valor_custo = float(com_custo["valor_estoque_custo"].sum())
    parado = com_custo[com_custo["situacao"].isin(["Sem giro", "Excesso"])]["valor_estoque_custo"].sum()
    giro_periodo = cmv / em_val if em_val > 0 else math.nan
    return {
        "n_produtos": int(len(a)),
        "n_sem_custo": int(a["sem_custo"].sum()),
        "valor_estoque_custo": valor_custo,
        "valor_estoque_venda": float(com_custo["valor_estoque_venda"].sum(skipna=True)),
        "lucro_potencial": float(com_custo["lucro_potencial"].sum(skipna=True)),
        "capital_parado": float(parado),
        "pct_capital_parado": float(parado) / valor_custo * 100 if valor_custo > 0 else math.nan,
        "receita_periodo": float(a["receita"].sum()),
        "cmv_periodo": cmv,
        "lucro_bruto_periodo": lucro,
        "giro_periodo": giro_periodo,
        "roi_periodo": lucro / em_val if em_val > 0 else math.nan,
        "giro_anual": giro_periodo * 365 / dias if dias > 0 and not math.isnan(giro_periodo) else math.nan,
        "roi_anual": (lucro / em_val) * 365 / dias if dias > 0 and em_val > 0 else math.nan,
        "dias_de_giro": dias / giro_periodo if giro_periodo and giro_periodo > 0 else math.nan,
        "dias_periodo": dias,
        "n_negativos": int((a["situacao"] == "Negativo").sum()),
        "n_ruptura": int((a["situacao"] == "Ruptura").sum()),
        "n_repor": int((a["situacao"] == "Repor").sum()),
        "n_sem_giro": int((a["situacao"] == "Sem giro").sum()),
        "n_excesso": int((a["situacao"] == "Excesso").sum()),
    }


def evolucao_valor_estoque(produtos: pd.DataFrame, mov: pd.DataFrame, ajustes: pd.DataFrame,
                           hoje: date, entradas=None, transferencias=None,
                           transferencias_entrada=None) -> pd.DataFrame:
    """Valor total do estoque a custo por dia (custo atual aplicado ao histórico)."""
    serie = serie_saldo_diaria(produtos, mov, ajustes, hoje, entradas, transferencias,
                               transferencias_entrada)
    p = _prep_produtos(produtos)[["cod_produto", "preco_custo"]]
    s = serie.merge(p, on="cod_produto")
    s = s[s["preco_custo"].notna()]
    s["valor"] = s["saldo"].clip(lower=0) * s["preco_custo"]
    return s.groupby("data", as_index=False)["valor"].sum()
