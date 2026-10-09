"""
estoque/parser_compras_sgi.py - Lê o .xls exportado pelo SGI em
Relatórios > Compras > Relação de Custo de Compras (botão "Gerar Arq.").

Diferente do CMV e da DANFE: aqui o arquivo já é um Excel genuíno (BIFF, formato antigo
.xls) com cabeçalho de colunas fixo — não precisa de regex nem de pdfplumber. O código
do produto já é o nosso próprio código (mesmo SGI que gera as vendas).

Colunas confirmadas no arquivo real (período 01-07/10/2026, Grupo=DEFENSIVOS):
    COD_PROD | DESCRICAO | COMPRA_TOTAL | BONIFICACAO_TOTAL | QTD_TOTAL |
    IMPOSTOS_FEDERAIS | VL_ICMS | cl_preco_medio
"""
from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO

import pandas as pd

_COLUNAS_ESPERADAS = {
    "COD_PROD", "DESCRICAO", "COMPRA_TOTAL", "BONIFICACAO_TOTAL", "QTD_TOTAL",
    "IMPOSTOS_FEDERAIS", "VL_ICMS", "cl_preco_medio",
}


@dataclass
class ItemCompra:
    cod_produto: str
    descricao: str
    qtd_total: float
    compra_total: float
    bonificacao_total: float


@dataclass
class RelatorioCompras:
    itens: list[ItemCompra] = field(default_factory=list)
    aviso: str | None = None


def extrair_compras(xls_bytes: bytes) -> RelatorioCompras:
    try:
        df = pd.read_excel(BytesIO(xls_bytes), dtype={"COD_PROD": str})
    except Exception as e:  # noqa: BLE001
        return RelatorioCompras(itens=[], aviso=f"Não consegui ler o arquivo como Excel: {e}")

    faltando = _COLUNAS_ESPERADAS - set(df.columns)
    if faltando:
        return RelatorioCompras(
            itens=[],
            aviso=f"Colunas esperadas não encontradas: {sorted(faltando)} (o layout do relatório pode ter mudado).",
        )

    itens = []
    for _, r in df.iterrows():
        cod = str(r["COD_PROD"]).strip()
        if not cod or cod.lower() == "nan":
            continue
        qtd = float(r["QTD_TOTAL"] or 0)
        if qtd == 0:
            continue  # linha sem quantidade (ex.: só bonificação sem unidade) não altera saldo
        itens.append(ItemCompra(
            cod_produto=cod,
            descricao=str(r["DESCRICAO"]).strip(),
            qtd_total=qtd,
            compra_total=float(r["COMPRA_TOTAL"] or 0),
            bonificacao_total=float(r["BONIFICACAO_TOTAL"] or 0),
        ))
    aviso = None if itens else "Nenhum item com quantidade encontrado no período/filtro."
    return RelatorioCompras(itens=itens, aviso=aviso)


def para_linhas_movimentacao(rel: RelatorioCompras) -> list[dict]:
    """Formato esperado por db.substituir_movimentacao_entrada_dia."""
    return [
        {
            "cod_produto": i.cod_produto,
            "descricao": i.descricao,
            "quantidade_entrada": i.qtd_total,
            "valor_entrada": i.compra_total,
        }
        for i in rel.itens
    ]
