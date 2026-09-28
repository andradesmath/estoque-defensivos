"""
estoque/relatorios.py - Gera PDFs para impressão a partir do que já está na tela
(mesmos dados de estoque/paginas.py, sem consulta própria ao banco).

Cada `gerar_pdf_*` recebe DataFrames já filtrados/calculados pela tela e devolve
`bytes` prontos para `st.download_button`.
"""
from __future__ import annotations

from datetime import date, datetime
from io import BytesIO

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .ui_util import brl, num

AZUL = colors.HexColor("#1B4B66")
CINZA_CLARO = colors.HexColor("#F4F6F8")
CINZA_TEXTO = colors.HexColor("#1A1F24")

_styles = getSampleStyleSheet()
_titulo = ParagraphStyle("TituloRel", parent=_styles["Title"], textColor=AZUL, fontSize=18, spaceAfter=2)
_subtitulo = ParagraphStyle("SubtituloRel", parent=_styles["Normal"], textColor=colors.grey, fontSize=9, spaceAfter=14)
_secao = ParagraphStyle("SecaoRel", parent=_styles["Heading2"], textColor=AZUL, fontSize=12, spaceBefore=14, spaceAfter=6)
_produto = ParagraphStyle("ProdutoRel", parent=_styles["Heading3"], textColor=CINZA_TEXTO, fontSize=10, spaceBefore=8, spaceAfter=3)
_celula = ParagraphStyle("CelulaRel", parent=_styles["Normal"], fontSize=8.5, leading=11)


def _rodape(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.grey)
    canvas.drawString(20 * mm, 12 * mm, "Grupo Porteira — Controle de Estoque de Defensivos")
    canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, f"Página {doc.page}")
    canvas.restoreState()


def _tabela(cabecalho: list[str], linhas: list[list], larguras: list[float], destacar_negativo_col: int | None = None) -> Table:
    dados = [[Paragraph(f"<b>{c}</b>", _celula) for c in cabecalho]] + linhas
    t = Table(dados, colWidths=larguras, repeatRows=1)
    estilo = [
        ("BACKGROUND", (0, 0), (-1, 0), AZUL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, CINZA_CLARO]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D8DEE3")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]
    t.setStyle(TableStyle(estilo))
    return t


def gerar_pdf_vendas_zerados(ini: date, fim: date, resumo: pd.DataFrame, zerados: pd.DataFrame,
                              mov: pd.DataFrame) -> bytes:
    """Relatório: total vendido por loja + produtos zerados com o detalhe de dia/loja das vendas."""
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=20 * mm,
                            leftMargin=18 * mm, rightMargin=18 * mm)
    story = [
        Paragraph("Vendas por loja e produtos zerados", _titulo),
        Paragraph(f"Período: {ini:%d/%m/%Y} a {fim:%d/%m/%Y}  ·  "
                  f"Gerado em {datetime.now():%d/%m/%Y às %H:%M}", _subtitulo),
    ]

    story.append(Paragraph("Total vendido por loja", _secao))
    linhas = [[Paragraph(r["loja"], _celula), Paragraph(f"{num(r['quantidade_saida'], 0)} un.", _celula),
               Paragraph(brl(r["valor_saida"]), _celula)] for _, r in resumo.iterrows()]
    story.append(_tabela(["Loja", "Unidades vendidas", "Valor vendido"], linhas, [70 * mm, 45 * mm, 45 * mm]))

    story.append(Paragraph("Produtos com saldo zerado ou negativo", _secao))
    if zerados.empty:
        story.append(Paragraph("Nenhum produto com saldo zerado ou negativo no período.", _celula))
    else:
        linhas = [[
            Paragraph(p["cod_produto"], _celula), Paragraph(str(p["descricao"]), _celula),
            Paragraph(num(p["saldo_inicial"], 0), _celula), Paragraph(num(p["saldo_atual"], 0), _celula),
            Paragraph(num(p["saidas_total"], 0), _celula), Paragraph(brl(p["valor_saidas_total"]), _celula),
        ] for _, p in zerados.iterrows()]
        story.append(_tabela(
            ["Código", "Produto", "Saldo contagem", "Saldo atual", "Vendido", "Valor vendido"],
            linhas, [20 * mm, 55 * mm, 24 * mm, 22 * mm, 20 * mm, 29 * mm]))

        story.append(Paragraph("Detalhe das vendas — dia e loja de cada saída", _secao))
        for _, p in zerados.iterrows():
            det = mov[mov["cod_produto"] == p["cod_produto"]].sort_values("data")
            bloco = [Paragraph(f"{p['cod_produto']} — {p['descricao']} "
                               f"(saldo atual: {num(p['saldo_atual'], 0)})", _produto)]
            if det.empty:
                bloco.append(Paragraph("Sem vendas no período filtrado.", _celula))
            else:
                linhas_det = [[Paragraph(f"{d:%d/%m/%Y}", _celula), Paragraph(loja, _celula),
                               Paragraph(num(qtd, 0), _celula), Paragraph(brl(val), _celula)]
                              for d, loja, qtd, val in zip(det["data"], det["loja"], det["quantidade_saida"],
                                                            det["valor_saida"])]
                bloco.append(_tabela(["Dia", "Loja", "Qtd. vendida", "Valor"], linhas_det,
                                     [30 * mm, 55 * mm, 30 * mm, 35 * mm]))
            story.append(KeepTogether(bloco))

    doc.build(story, onFirstPage=_rodape, onLaterPages=_rodape)
    return buf.getvalue()
