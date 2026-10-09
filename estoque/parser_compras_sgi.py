"""
estoque/parser_compras_sgi.py - Lê o .xls exportado pelo SGI em
Relatórios > Compras > Relação de Custo de Compras (botão "Gerar Arq.").

Diferente do CMV e da DANFE: aqui o arquivo já é um Excel genuíno (BIFF, formato antigo
.xls) com cabeçalho de colunas fixo — não precisa de regex nem de pdfplumber. O código
do produto já é o nosso próprio código (mesmo SGI que gera as vendas).

Usa `xlrd` em vez de `pandas` DE PROPÓSITO: este módulo é importado pelo robô local
scripts/sync_compras_sgi.py, que roda em Python de 32 bits (mesma arquitetura do
SGI.exe) — e pandas não publica mais build pronta para Windows 32 bits em nenhuma
versão compatível com Python 3.11, só compila do zero (precisa de toolchain que a
máquina do robô não tem). xlrd é puro Python, instala em qualquer arquitetura sem
compilar nada.

Colunas confirmadas no arquivo real (período 01-07/10/2026, Grupo=DEFENSIVOS):
    COD_PROD | DESCRICAO | COMPRA_TOTAL | BONIFICACAO_TOTAL | QTD_TOTAL |
    IMPOSTOS_FEDERAIS | VL_ICMS | cl_preco_medio
"""
from __future__ import annotations

from dataclasses import dataclass, field

import xlrd

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


def _texto(valor) -> str:
    """Normaliza uma célula pra string, sem virar '123.0' quando o Excel guardou
    como número (ex.: código de produto exportado sem zero à esquerda)."""
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor).strip()


def _numero(valor) -> float:
    return float(valor) if valor not in ("", None) else 0.0


def extrair_compras(xls_bytes: bytes) -> RelatorioCompras:
    try:
        livro = xlrd.open_workbook(file_contents=xls_bytes)
        planilha = livro.sheet_by_index(0)
    except Exception as e:  # noqa: BLE001
        return RelatorioCompras(itens=[], aviso=f"Não consegui ler o arquivo como Excel: {e}")

    if planilha.nrows == 0:
        return RelatorioCompras(itens=[], aviso="Arquivo sem nenhuma linha.")

    cabecalho = [str(c.value).strip() for c in planilha.row(0)]
    idx = {nome: i for i, nome in enumerate(cabecalho)}

    faltando = _COLUNAS_ESPERADAS - set(idx)
    if faltando:
        return RelatorioCompras(
            itens=[],
            aviso=f"Colunas esperadas não encontradas: {sorted(faltando)} (o layout do relatório pode ter mudado).",
        )

    itens = []
    for r in range(1, planilha.nrows):
        linha = planilha.row(r)
        cod = _texto(linha[idx["COD_PROD"]].value)
        if not cod or cod.lower() == "nan":
            continue
        qtd = _numero(linha[idx["QTD_TOTAL"]].value)
        if qtd == 0:
            continue  # linha sem quantidade (ex.: só bonificação sem unidade) não altera saldo
        itens.append(ItemCompra(
            cod_produto=cod,
            descricao=_texto(linha[idx["DESCRICAO"]].value),
            qtd_total=qtd,
            compra_total=_numero(linha[idx["COMPRA_TOTAL"]].value),
            bonificacao_total=_numero(linha[idx["BONIFICACAO_TOTAL"]].value),
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
