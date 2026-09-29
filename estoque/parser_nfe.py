"""
estoque/parser_nfe.py - Extrai (melhor esforço) os itens de um DANFE em PDF, para
pré-preencher a tela "Entrada por nota fiscal".

Só existe suporte a PDF (não XML): a extração de tabela por PDF é frágil — o layout do
DANFE varia um pouco por emissor — então isto NUNCA grava nada sozinho. O resultado é
sempre mostrado na tela para o usuário conferir, corrigir ou completar manualmente antes
de confirmar a entrada.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from io import BytesIO

import pdfplumber

_RE_CNPJ = re.compile(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}")
_RE_NUMERO = re.compile(r"N[ºo]\.\s*([\d.]+)")
_RE_EMITENTE = re.compile(r"RECEBEMOS DE (.+?) OS PRODUTOS", re.IGNORECASE)
_RE_EMISSAO = re.compile(r"EMISS[ÃA]O:\s*(\d{2}/\d{2}/\d{4})")
# Linha de item do DANFE padrão: código  descrição  NCM(8 díg.)  CST  CFOP(4 díg.)  UN  QUANT  VALOR_UNIT  VALOR_TOTAL ...
_RE_ITEM = re.compile(
    r"^(\S+)\s+(.*?)\s+(\d{8})\s+(\S+)\s+(\d{4})\s+(\S+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)"
)


def _num_br(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return None


@dataclass
class ItemNFe:
    cod_fornecedor: str
    descricao: str
    quantidade: float
    valor_unitario: float


@dataclass
class NFeExtraida:
    cnpj_emitente: str | None
    nome_emitente: str | None
    numero: str | None
    data_emissao: str | None  # 'DD/MM/AAAA' ou None
    itens: list[ItemNFe] = field(default_factory=list)
    aviso: str | None = None


def extrair_danfe(pdf_bytes: bytes) -> NFeExtraida:
    texto = ""
    itens: list[ItemNFe] = []
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for pagina in pdf.pages:
            texto += (pagina.extract_text() or "") + "\n"
            for tabela in pagina.extract_tables():
                cabecalho = " ".join((tabela[0][0] or "").split()).upper() if tabela and tabela[0] else ""
                if "CÓDIGO" not in cabecalho and "CODIGO" not in cabecalho:
                    continue
                for linha in tabela[1:]:
                    if not linha or not linha[0]:
                        continue
                    primeira_linha_celula = linha[0].split("\n")[0]
                    m = _RE_ITEM.match(primeira_linha_celula)
                    if not m:
                        continue
                    cod, desc, _ncm, _cst, _cfop, _un, quant, val_unit, _val_tot = m.groups()
                    itens.append(ItemNFe(
                        cod_fornecedor=cod, descricao=desc.strip(),
                        quantidade=_num_br(quant) or 0.0, valor_unitario=_num_br(val_unit) or 0.0,
                    ))

    cnpjs = _RE_CNPJ.findall(texto)
    m_nome = _RE_EMITENTE.search(texto)
    m_num = _RE_NUMERO.search(texto)
    m_emissao = _RE_EMISSAO.search(texto)
    aviso = None if itens else ("Não consegui reconhecer a tabela de itens automaticamente neste PDF — "
                                "adicione as linhas manualmente abaixo (código, descrição, quantidade, valor).")
    return NFeExtraida(
        cnpj_emitente=cnpjs[0] if cnpjs else None,
        nome_emitente=m_nome.group(1).strip() if m_nome else None,
        numero=m_num.group(1) if m_num else None,
        data_emissao=m_emissao.group(1) if m_emissao else None,
        itens=itens,
        aviso=aviso,
    )
