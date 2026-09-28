import io

import pandas as pd

from estoque.importacao import validar_planilha
from estoque.util import normalizar_cod


def _xlsx(df: pd.DataFrame, aba="Defensivos") -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, sheet_name=aba, index=False)
    return buf.getvalue()


def test_planilha_real(base_xlsx_bytes):
    r = validar_planilha(base_xlsx_bytes, "base.xlsx")
    assert r.ok and r.aba == "Defensivos"
    assert len(r.df) == 227
    assert r.df.iloc[0]["cod_produto"] == "00004"  # zeros preservados
    assert len(r.ignorados) == 37 and r.ignorados["motivo"].notna().all()


def test_codigo_numerico_no_excel_vira_5_digitos():
    df = pd.DataFrame({"Código": [4, 50, 9922], "Produto": ["A", "B", "C"], "Quantidade": [1, 2, 3]})
    r = validar_planilha(_xlsx(df), "x.xlsx")
    assert r.ok and list(r.df["cod_produto"]) == ["00004", "00050", "09922"]


def test_colunas_obrigatorias_ausentes():
    r = validar_planilha(_xlsx(pd.DataFrame({"Código": ["1"], "Produto": ["A"]})), "x.xlsx")
    assert not r.ok and "Quantidade" in r.erros[0]


def test_codigo_duplicado_e_erro():
    df = pd.DataFrame({"Código": ["00004", "4"], "Produto": ["A", "B"], "Quantidade": [1, 2]})
    r = validar_planilha(_xlsx(df), "x.xlsx")
    assert not r.ok and any("repetido" in e for e in r.erros)


def test_linhas_invalidas_reportam_numero_da_linha():
    df = pd.DataFrame({"Código": ["00004", "abc", "00006"], "Produto": ["A", "B", ""], "Quantidade": [1, "x", 3]})
    r = validar_planilha(_xlsx(df), "x.xlsx")
    assert not r.ok
    assert any(e.startswith("linha 3") for e in r.erros) and any(e.startswith("linha 4") for e in r.erros)


def test_csv_ponto_e_virgula_com_decimal_brasileiro():
    csv = "Código;Produto;Quantidade;Preço de custo\n00004;EPINGLE LT;25,5;1.234,50\n".encode("utf-8")
    r = validar_planilha(csv, "base.csv")
    assert r.ok
    assert float(r.df.iloc[0]["saldo_inicial"]) == 25.5 and float(r.df.iloc[0]["preco_custo"]) == 1234.5


def test_negativo_gera_aviso_nao_erro():
    df = pd.DataFrame({"Código": ["00004"], "Produto": ["A"], "Quantidade": [-2]})
    r = validar_planilha(_xlsx(df), "x.xlsx")
    assert r.ok and r.avisos


def test_normalizar_cod():
    assert normalizar_cod(4.0) == "00004" and normalizar_cod("00004") == "00004"
    assert normalizar_cod("12345678") == "12345678"
    assert normalizar_cod("A1") is None and normalizar_cod(None) is None and normalizar_cod(float("nan")) is None
    assert normalizar_cod(4.5) is None
