"""estoque/ui_util.py - formatação pt-BR e utilidades de tela (sem Streamlit aqui)."""
from __future__ import annotations

import io
import math

import pandas as pd


def _sep_br(s: str) -> str:
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def brl(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return "R$ " + _sep_br(f"{v:,.2f}")


def num(v, casas: int = 0) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return _sep_br(f"{v:,.{casas}f}")


def pct(v, casas: int = 1) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return _sep_br(f"{v:,.{casas}f}") + "%"


def vazio(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or (v is pd.NA)


def para_excel(df: pd.DataFrame, aba: str = "Dados") -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, sheet_name=aba[:31], index=False)
    return buf.getvalue()


COR_SITUACAO = {
    "Negativo": "background-color: rgba(220, 38, 38, 0.30)",
    "Ruptura": "background-color: rgba(220, 38, 38, 0.18)",
    "Repor": "background-color: rgba(245, 158, 11, 0.28)",
    "Sem giro": "background-color: rgba(107, 114, 128, 0.25)",
    "Excesso": "background-color: rgba(59, 130, 246, 0.25)",
}


def estilo_situacao(v) -> str:
    return COR_SITUACAO.get(v, "")


ROTULO_SITUACAO = {
    "Negativo": "Saldo negativo (erro de lançamento/transferência?)",
    "Ruptura": "Zerado",
    "Repor": "Abaixo do ponto de pedido / mínimo",
    "Sem giro": "Sem nenhuma saída no período",
    "Excesso": "Cobertura acima do limite de excesso",
    "OK": "Sem alerta",
}
