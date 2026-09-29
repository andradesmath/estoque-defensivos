"""
estoque/parser_cmv.py - Extrai o relatório "Relação de Custo de Vendas por Produto" do SGI
(CMV por produto, em R$) para recalcular o custo unitário do cadastro.

Diferente da nota fiscal: aqui o código do produto já é o nosso próprio código (o relatório
vem do mesmo SGI que gera as vendas), então não precisa de associação fornecedor → produto.
O relatório só tem valores em R$ (sem quantidade) — por isso o custo unitário é calculado
depois, casando o CMV daqui com a quantidade vendida que já está no nosso banco (mesmo
período), não só a partir deste arquivo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from io import BytesIO

import pdfplumber

_RE_NUM = re.compile(r"-?[\d.]+,\d{2}%?")
_RE_COD = re.compile(r"^(\d{4,6})\s+(.*)$")
_RE_PERIODO = re.compile(r"Per[íi]odo de (\d{2}/\d{2}/\d{4}) a (\d{2}/\d{2}/\d{4})")
_RE_EMPRESA = re.compile(r"Empresa:\s*(.+)")
_RE_NITENS = re.compile(r"N[ºo]\s*Itens\s*:\s*(\d+)")


def _num_br(s: str) -> float | None:
    s = (s or "").strip().rstrip("%")
    if not s:
        return None
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return None


@dataclass
class ItemCMV:
    cod_produto: str
    descricao: str
    saida_total: float
    cmv: float


@dataclass
class RelatorioCMV:
    empresa: str | None
    periodo_ini: str | None  # 'DD/MM/AAAA'
    periodo_fim: str | None
    n_itens_esperado: int | None
    itens: list[ItemCMV] = field(default_factory=list)
    aviso: str | None = None


def extrair_cmv(pdf_bytes: bytes) -> RelatorioCMV:
    itens: list[ItemCMV] = []
    empresa = periodo_ini = periodo_fim = None
    n_itens_esperado = None
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for i, pagina in enumerate(pdf.pages):
            texto = pagina.extract_text() or ""
            if i == 0:
                m = _RE_EMPRESA.search(texto)
                empresa = m.group(1).strip() if m else None
                m = _RE_PERIODO.search(texto)
                if m:
                    periodo_ini, periodo_fim = m.groups()
            m = _RE_NITENS.search(texto)
            if m:
                n_itens_esperado = int(m.group(1))
            for linha in texto.split("\n"):
                m = _RE_COD.match(linha)
                if not m:
                    continue
                cod, resto = m.groups()
                nums = list(_RE_NUM.finditer(resto))
                if len(nums) < 8:  # linha de item tem 8-9 colunas numéricas; menos que isso não é item
                    continue
                desc = resto[: nums[0].start()].strip()
                valores = [n.group() for n in nums]
                saida_total = _num_br(valores[0])
                cmv = _num_br(valores[4])
                if saida_total is None or cmv is None:
                    continue
                itens.append(ItemCMV(cod_produto=cod, descricao=desc, saida_total=saida_total, cmv=cmv))

    aviso = None
    if n_itens_esperado is not None and len(itens) != n_itens_esperado:
        aviso = (f"O relatório diz ter {n_itens_esperado} item(ns), mas reconheci {len(itens)} — "
                "confira com atenção antes de aplicar.")
    elif not itens:
        aviso = "Não consegui reconhecer nenhum item neste PDF."
    return RelatorioCMV(empresa=empresa, periodo_ini=periodo_ini, periodo_fim=periodo_fim,
                        n_itens_esperado=n_itens_esperado, itens=itens, aviso=aviso)
