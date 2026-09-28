"""estoque/util.py - normalizações pequenas usadas por importação, banco e sync."""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

FUSO = ZoneInfo("America/Bahia")  # UTC-3 fixo; o runner do GitHub roda em UTC


def hoje_brasil() -> date:
    return datetime.now(FUSO).date()


def normalizar_cod(valor) -> str | None:
    """Código de produto do SGI: string de 5 dígitos com zeros à esquerda.
    Aceita '00004', '4', 4, 4.0 (o Excel devolve número quando a coluna não é texto).
    Retorna None se vazio ou não numérico (não inventa código)."""
    if valor is None:
        return None
    if isinstance(valor, float):
        if math.isnan(valor):
            return None
        if valor != int(valor):
            return None
        valor = int(valor)
    s = str(valor).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    if not re.fullmatch(r"\d+", s):
        return None
    return s.zfill(5)


def para_decimal(valor, casas: int | None = None) -> Decimal | None:
    """Converte para Decimal aceitando float/int/str ('1.234,56' e '1234.56').
    None/NaN/'' -> None."""
    if valor is None:
        return None
    if isinstance(valor, Decimal):
        d = valor
    elif isinstance(valor, float):
        if math.isnan(valor):
            return None
        d = Decimal(str(valor))
    elif isinstance(valor, int):
        d = Decimal(valor)
    else:
        s = str(valor).strip().replace("R$", "").strip()
        if not s:
            return None
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        try:
            d = Decimal(s)
        except InvalidOperation:
            return None
    if casas is not None:
        d = d.quantize(Decimal(1).scaleb(-casas))
    return d


def para_int(valor) -> int | None:
    d = para_decimal(valor)
    if d is None:
        return None
    return int(d)


def texto_ou_none(valor) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, float) and math.isnan(valor):
        return None
    s = str(valor).strip()
    return s or None
