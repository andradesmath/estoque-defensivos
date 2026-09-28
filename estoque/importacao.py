"""
estoque/importacao.py - Leitura e validação da planilha da base de produtos
(Excel ou CSV). Sem acesso a banco: devolve DataFrame normalizado + erros/avisos,
para o painel mostrar tudo ANTES de gravar qualquer coisa.

Pontos que quebram importações desse tipo (e por isso são tratados aqui):
  - códigos com zeros à esquerda que o Excel/pandas viram número (00004 -> 4): lemos
    tudo como texto e normalizamos para 5 dígitos;
  - nomes de coluna com acento/caixa diferentes ("Código", "codigo", "COD");
  - duplicatas de código (ambíguas -> erro, nunca "vence o último" em silêncio);
  - várias abas: usa a aba "Defensivos" se existir, senão a primeira.
"""
from __future__ import annotations

import io
import unicodedata
from dataclasses import dataclass, field

import pandas as pd

from .util import normalizar_cod, para_decimal, para_int, texto_ou_none

ALIASES = {
    "cod_produto": {"codigo", "cod", "cod_produto", "cod produto", "codigo produto", "codigo do produto"},
    "descricao": {"produto", "descricao", "nome", "descricao produto", "descricao do produto"},
    "saldo_inicial": {"quantidade", "saldo", "saldo inicial", "saldo_inicial", "qtd", "estoque", "contagem"},
    "unidade": {"unidade", "un", "und", "unid"},
    "preco_custo": {"preco custo", "preco de custo", "custo", "preco_custo", "custo unitario"},
    "preco_venda": {"preco venda", "preco de venda", "preco_venda", "venda", "preco"},
    "lead_time_dias": {"lead time", "lead_time", "lead_time_dias", "prazo de reposicao", "prazo reposicao"},
    "estoque_minimo": {"estoque minimo", "minimo", "estoque_minimo"},
    "fornecedor": {"fornecedor", "marca"},
}
OBRIGATORIAS = ("cod_produto", "descricao", "saldo_inicial")
ABA_PREFERIDA = "defensivos"
ABA_REMOVIDOS_PREFIXO = "removidos"


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(c))


def _chave(s: str) -> str:
    return _sem_acento(s).strip().lower().replace("_", " ")


@dataclass
class ResultadoImportacao:
    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    erros: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    aba: str | None = None
    ignorados: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def ok(self) -> bool:
        return not self.erros and not self.df.empty


def _ler_bruto(conteudo: bytes, nome: str) -> tuple[pd.DataFrame, str | None, pd.DataFrame]:
    """Lê tudo como texto. Retorna (df da base, nome da aba, df da aba de removidos)."""
    if nome.lower().endswith((".csv", ".txt")):
        for enc in ("utf-8-sig", "latin-1"):
            try:
                texto = conteudo.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        primeira = texto.splitlines()[0] if texto else ""
        sep = ";" if primeira.count(";") >= primeira.count(",") else ","
        return pd.read_csv(io.StringIO(texto), sep=sep, dtype=str, keep_default_na=False), None, pd.DataFrame()

    xl = pd.ExcelFile(io.BytesIO(conteudo), engine="openpyxl")
    abas = xl.sheet_names
    aba = next((a for a in abas if _chave(a) == ABA_PREFERIDA), abas[0])
    df = xl.parse(aba, dtype=str, keep_default_na=False)
    aba_rem = next((a for a in abas if _chave(a).startswith(ABA_REMOVIDOS_PREFIXO)), None)
    rem = xl.parse(aba_rem, dtype=str, keep_default_na=False) if aba_rem else pd.DataFrame()
    return df, aba, rem


def _mapear_colunas(df: pd.DataFrame) -> dict[str, str]:
    """{nome_canonico: nome_original_na_planilha}"""
    mapa: dict[str, str] = {}
    for original in df.columns:
        k = _chave(original)
        for canon, aliases in ALIASES.items():
            if k in aliases and canon not in mapa:
                mapa[canon] = original
    return mapa


def _extrair_ignorados(rem: pd.DataFrame) -> pd.DataFrame:
    if rem.empty:
        return pd.DataFrame(columns=["cod_produto", "descricao", "motivo"])
    cols = {_chave(c): c for c in rem.columns}
    c_cod = next((cols[k] for k in cols if k in ALIASES["cod_produto"]), None)
    c_desc = next((cols[k] for k in cols if k in ALIASES["descricao"]), None)
    c_mot = next((cols[k] for k in cols if k.startswith("motivo")), None)
    if c_cod is None:
        return pd.DataFrame(columns=["cod_produto", "descricao", "motivo"])
    out = pd.DataFrame({
        "cod_produto": rem[c_cod].map(normalizar_cod),
        "descricao": rem[c_desc].map(texto_ou_none) if c_desc else None,
        "motivo": rem[c_mot].map(texto_ou_none) if c_mot else None,
    })
    return out.dropna(subset=["cod_produto"]).drop_duplicates("cod_produto").reset_index(drop=True)


def validar_planilha(conteudo: bytes, nome_arquivo: str) -> ResultadoImportacao:
    res = ResultadoImportacao()
    try:
        bruto, aba, rem = _ler_bruto(conteudo, nome_arquivo)
    except Exception as e:  # arquivo corrompido, formato errado etc.
        res.erros.append(f"Não consegui ler o arquivo: {e}")
        return res
    res.aba = aba
    res.ignorados = _extrair_ignorados(rem)

    mapa = _mapear_colunas(bruto)
    faltando = [c for c in OBRIGATORIAS if c not in mapa]
    if faltando:
        nomes = {"cod_produto": "Código", "descricao": "Produto", "saldo_inicial": "Quantidade"}
        res.erros.append(
            "Colunas obrigatórias ausentes: " + ", ".join(nomes[c] for c in faltando)
            + f". Colunas encontradas: {list(bruto.columns)}"
        )
        return res

    linhas, erros_linha = [], []
    for idx, r in bruto.iterrows():
        n_linha = idx + 2  # +1 cabeçalho, +1 base 1
        if all(not str(v).strip() for v in r.values):
            continue  # linha totalmente em branco
        cod = normalizar_cod(r[mapa["cod_produto"]])
        desc = texto_ou_none(r[mapa["descricao"]])
        saldo = para_decimal(r[mapa["saldo_inicial"]])
        problemas = []
        if cod is None:
            problemas.append(f"código inválido '{r[mapa['cod_produto']]}'")
        if not desc:
            problemas.append("descrição vazia")
        if saldo is None:
            problemas.append(f"quantidade inválida '{r[mapa['saldo_inicial']]}'")
        if problemas:
            erros_linha.append(f"linha {n_linha}: " + "; ".join(problemas))
            continue
        reg = {"cod_produto": cod, "descricao": desc, "saldo_inicial": saldo}
        for canon in ("unidade", "fornecedor"):
            if canon in mapa:
                reg[canon] = texto_ou_none(r[mapa[canon]])
        for canon in ("preco_custo", "preco_venda", "estoque_minimo"):
            if canon in mapa:
                reg[canon] = para_decimal(r[mapa[canon]])
        if "lead_time_dias" in mapa:
            reg["lead_time_dias"] = para_int(r[mapa["lead_time_dias"]])
        reg["_linha"] = n_linha
        linhas.append(reg)

    if erros_linha:
        res.erros.extend(erros_linha[:30])
        if len(erros_linha) > 30:
            res.erros.append(f"... e mais {len(erros_linha) - 30} linha(s) com erro.")

    df = pd.DataFrame(linhas)
    if df.empty:
        if not res.erros:
            res.erros.append("Planilha sem linhas de dados.")
        return res

    dup = df[df.duplicated("cod_produto", keep=False)]
    if not dup.empty:
        for cod, grp in dup.groupby("cod_produto"):
            res.erros.append(f"código {cod} repetido nas linhas {', '.join(str(x) for x in grp['_linha'])}")

    negativos = df[df["saldo_inicial"].map(lambda d: d < 0)]
    if not negativos.empty:
        res.avisos.append(f"{len(negativos)} produto(s) com quantidade negativa: "
                          + ", ".join(negativos["cod_produto"].head(10)))
    if not res.ignorados.empty:
        interseccao = set(res.ignorados["cod_produto"]) & set(df["cod_produto"])
        if interseccao:
            res.avisos.append(f"{len(interseccao)} código(s) aparecem na base E na aba de removidos: "
                              + ", ".join(sorted(interseccao)[:10]))

    res.df = df.drop(columns=["_linha"]).reset_index(drop=True)
    return res
