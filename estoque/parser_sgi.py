"""
estoque/parser_sgi.py - Parser do PDF "Relatorio Totais de Vendas por Produtos" do
SGI Solution, no layout gerado com Agrupamento = "Produto" e Grupos = DEFENSIVOS.

Layout validado contra um PDF real (tests/fixtures/relatorio_defensivos_22_24.pdf):
uma linha por produto com  N° | Cod | Descrição | Marca/Fornecedor | Posit | Vendas |
Qtd | Qtd Cx | Valor Total | %,  e uma linha final "Total:" com 6 agregados.

O pypdf entrega esse relatório com UMA CÉLULA POR LINHA (não uma linha por linha de
tabela), então a estratégia principal é uma máquina de estados sobre a lista de
linhas. Se um dia o extrator passar a devolver a linha inteira, a estratégia
secundária (regex por linha) assume. Se nenhuma reconciliar com o "Total:" impresso,
`validar` devolve problemas e o sync NÃO grava nada.

Diferenças em relação ao parser de vendedores (dashboard existente):
  - não há cabeçalho de vendedor por seção;
  - Marca/Fornecedor vem como um único "-" (no layout vendedor/produto vinha
    "00122 - NOME"); aceitamos os dois formatos.

Sem dependência de banco nem de Streamlit — só pypdf.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader

PADRAO_NUM = re.compile(r"^-?[\d.,]+%?$")
PADRAO_NSEQ = re.compile(r"^\d{1,5}$")
PADRAO_COD = re.compile(r"^\d{4,6}$")
PADRAO_FORNECEDOR = re.compile(r"^\d{5} - .+")
PADRAO_PERIODO = re.compile(r"(\d{2})/(\d{2})/(\d{4})\s+a\s+(\d{2})/(\d{2})/(\d{4})")
PADRAO_TIMESTAMP = re.compile(r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}")
CABECALHOS_COLUNA = {
    "N°", "Cod", "Descrição Produto", "Marca", "Fornecedor",
    "Posit", "Vendas", "Qtd", "Qtd Cx", "Valor Total", "%",
}
PREFIXOS_BOILERPLATE = ("S.G.I.", "©", "SGI SOLUTION", "- (")
TITULO_ESPERADO = "relatorio totais de vendas por produtos"
TOLERANCIA_VALOR = Decimal("0.05")

# Inline: "0 09490 DEXTER PLUS 1LT - 4 10 10 10 1.397,24 11,18"
PADRAO_LINHA_INLINE = re.compile(
    r"^(?P<seq>\d{1,5})\s+(?P<cod>\d{4,6})\s+(?P<desc>.+?)\s+"
    r"(?P<marca>-|\d{5} - .+?)\s+"
    r"(?P<posit>[\d.,]+)\s+(?P<vendas>[\d.,]+)\s+(?P<qtd>[\d.,]+)\s+"
    r"(?P<qtd_cx>[\d.,]+)\s+(?P<valor>[\d.,]+)\s+(?P<pct>[\d.,]+)%?$"
)


def parse_decimal_brl(texto: str) -> Decimal | None:
    """'1.397,24' -> Decimal('1397.24'); '100%' -> Decimal('100'); '-' -> None."""
    t = (texto or "").strip().replace("R$", "").replace("%", "").strip()
    if not t or t == "-":
        return None
    negativo = t.startswith("-")
    t = t.lstrip("-").strip().replace(".", "").replace(",", ".")
    try:
        v = Decimal(t)
    except InvalidOperation:
        return None
    return -v if negativo else v


@dataclass
class ResultadoRelatorio:
    periodo: tuple[date, date] | None = None
    grupos: list[str] = field(default_factory=list)
    titulo_ok: bool = False
    itens: list[dict] = field(default_factory=list)
    totais: list[dict] = field(default_factory=list)
    registros_descartados: int = 0
    estrategia: str = ""


def extrair_linhas_pdf(pdf_bytes: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    linhas: list[str] = []
    for pagina in reader.pages:
        linhas.extend((pagina.extract_text() or "").splitlines())
    return linhas


def _e_boilerplate(l: str) -> bool:
    return bool(
        PADRAO_PERIODO.fullmatch(l)
        or PADRAO_TIMESTAMP.match(l)
        or l.startswith(PREFIXOS_BOILERPLATE)
        or l.lower().startswith("relatorio ")
    )


def _item(seq, cod, desc, posit, vendas, qtd, qtd_cx, valor, pct) -> dict:
    return {
        "seq": int(seq),
        "cod_produto": cod.zfill(5),
        "descricao": desc.strip(),
        "posit": parse_decimal_brl(posit),
        "vendas": parse_decimal_brl(vendas),
        "qtd": parse_decimal_brl(qtd),
        "qtd_cx": parse_decimal_brl(qtd_cx),
        "valor_total": parse_decimal_brl(valor),
        "pct": parse_decimal_brl(pct),
    }


def _total(valores: list[str]) -> dict:
    v = [parse_decimal_brl(x) for x in valores] + [None] * (6 - len(valores))
    return {"posit": v[0], "vendas": v[1], "qtd": v[2], "qtd_cx": v[3], "valor_total": v[4], "pct": v[5]}


def _parse_celula_por_linha(linhas: list[str], res: ResultadoRelatorio) -> None:
    n = len(linhas)
    i = 0
    while i < n:
        l = linhas[i]

        if l == "Grupos:":
            i += 1
            partes = []
            # A lista de grupos é "A,", "B," ... e a última pode vir sem vírgula. Para na
            # primeira linha sem vírgula final (ou no cabeçalho de coluna), para nunca
            # engolir linhas de dados quando o cabeçalho "N°" não vier logo depois.
            while (
                i < n
                and linhas[i] not in CABECALHOS_COLUNA
                and not _e_boilerplate(linhas[i])
                and not linhas[i][0].isdigit()  # linha de dado, não nome de grupo
                and linhas[i] != "Total:"
            ):
                partes.append(linhas[i])
                i += 1
                if not partes[-1].rstrip().endswith(","):
                    break
            for g in " ".join(partes).split(","):
                g = g.strip()
                if g and g not in res.grupos:
                    res.grupos.append(g)
            continue

        if l.lower().startswith(TITULO_ESPERADO):
            res.titulo_ok = True
            i += 1
            continue

        if _e_boilerplate(l) or l in CABECALHOS_COLUNA:
            i += 1
            continue

        if l == "Total:":
            i += 1
            vals: list[str] = []
            while i < n and len(vals) < 6 and PADRAO_NUM.match(linhas[i]):
                vals.append(linhas[i])
                i += 1
            res.totais.append(_total(vals))
            continue

        if PADRAO_NSEQ.match(l) and i + 1 < n and PADRAO_COD.match(linhas[i + 1]):
            j = i + 2
            limite = min(n, j + 30)  # trava contra formato inesperado
            achou = None
            while j < limite:
                if linhas[j] == "-" or PADRAO_FORNECEDOR.match(linhas[j]):
                    k = j
                    while k < n and k < j + 2 and (linhas[k] == "-" or PADRAO_FORNECEDOR.match(linhas[k])):
                        k += 1
                    if k + 6 <= n and all(PADRAO_NUM.match(x) for x in linhas[k:k + 6]):
                        achou = (j, k)
                        break
                j += 1
            if achou is None:
                res.registros_descartados += 1
                i += 1
                continue
            j, k = achou
            desc = " ".join(linhas[i + 2:j])
            posit, vendas, qtd, qtd_cx, valor, pct = linhas[k:k + 6]
            res.itens.append(_item(l, linhas[i + 1], desc, posit, vendas, qtd, qtd_cx, valor, pct))
            i = k + 6
            continue

        i += 1


def _parse_inline(linhas: list[str], res: ResultadoRelatorio) -> None:
    for l in linhas:
        l = l.strip()
        m = PADRAO_LINHA_INLINE.match(l)
        if m:
            g = m.groupdict()
            res.itens.append(_item(g["seq"], g["cod"], g["desc"], g["posit"], g["vendas"],
                                   g["qtd"], g["qtd_cx"], g["valor"], g["pct"]))
        elif l.startswith("Total:"):
            res.totais.append(_total(l[len("Total:"):].split()))


def parse_linhas(linhas: list[str]) -> ResultadoRelatorio:
    linhas = [l.strip() for l in linhas if l and l.strip()]
    texto = "\n".join(linhas)

    res = ResultadoRelatorio()
    m = PADRAO_PERIODO.search(texto)
    if m:
        d1, m1, a1, d2, m2, a2 = (int(x) for x in m.groups())
        try:
            res.periodo = (date(a1, m1, d1), date(a2, m2, d2))
        except ValueError:
            pass

    _parse_celula_por_linha(linhas, res)
    res.estrategia = "celula_por_linha"

    if not res.itens:
        alt = ResultadoRelatorio(periodo=res.periodo, grupos=res.grupos, titulo_ok=res.titulo_ok)
        _parse_inline(linhas, alt)
        if alt.itens:
            alt.estrategia = "inline"
            return alt
    return res


def parse_relatorio_defensivos(pdf_bytes: bytes) -> ResultadoRelatorio:
    return parse_linhas(extrair_linhas_pdf(pdf_bytes))


def validar(
    res: ResultadoRelatorio,
    dia_esperado: date | None = None,
    grupo_esperado: str | None = "DEFENSIVOS",
) -> list[str]:
    """Devolve a lista de problemas (vazia = PDF confiável). Cada checagem existe por
    um risco concreto:
      - período: o widget de data do SGI já gerou relatório com a data errada (hoje) em
        vez da pedida; sem essa checagem gravaríamos o dia errado;
      - grupo: se o filtro DEFENSIVOS não pegou, o PDF traz produtos que não são
        defensivos e o saldo de todos os outros grupos seria descontado;
      - totais: reconcilia soma dos itens com a linha "Total:" — pega linha perdida
        pelo parser e mudança de layout."""
    problemas: list[str] = []

    if not res.titulo_ok:
        problemas.append("título 'Relatorio Totais de Vendas por Produtos' não encontrado — PDF inesperado.")

    if res.periodo is None:
        problemas.append("período (DD/MM/AAAA a DD/MM/AAAA) não encontrado no PDF.")
    elif dia_esperado is not None and res.periodo != (dia_esperado, dia_esperado):
        problemas.append(
            f"período do PDF ({res.periodo[0]:%d/%m/%Y} a {res.periodo[1]:%d/%m/%Y}) "
            f"difere do dia pedido ({dia_esperado:%d/%m/%Y})."
        )

    if grupo_esperado is not None:
        grupos = {g.upper() for g in res.grupos}
        if grupos != {grupo_esperado.upper()}:
            problemas.append(
                f"filtro de grupo no PDF = {sorted(grupos) or 'vazio'}; esperado apenas '{grupo_esperado}'."
            )

    if res.registros_descartados:
        problemas.append(f"{res.registros_descartados} registro(s) com formato não reconhecido foram descartados.")

    if res.itens and not res.totais:
        problemas.append("PDF com produtos mas sem linha 'Total:' — não dá para reconciliar.")
    elif res.totais and not res.itens:
        problemas.append("linha 'Total:' presente mas nenhum produto reconhecido — parser não leu as linhas.")
    elif res.itens and res.totais:
        soma_valor = sum((i["valor_total"] or Decimal(0)) for i in res.itens)
        soma_qtd = sum((i["qtd"] or Decimal(0)) for i in res.itens)
        # Aceita um total geral (último) OU subtotais por página somando ao mesmo valor.
        candidatos = [res.totais[-1]]
        if len(res.totais) > 1:
            candidatos.append({
                "valor_total": sum((t["valor_total"] or Decimal(0)) for t in res.totais),
                "qtd": sum((t["qtd"] or Decimal(0)) for t in res.totais),
            })
        ok = any(
            abs(soma_valor - (t["valor_total"] or Decimal(0))) <= TOLERANCIA_VALOR
            and abs(soma_qtd - (t["qtd"] or Decimal(0))) <= Decimal("0.001")
            for t in candidatos
        )
        if not ok:
            t = res.totais[-1]
            problemas.append(
                f"soma dos produtos (qtd {soma_qtd}, R$ {soma_valor}) não bate com o 'Total:' impresso "
                f"(qtd {t['qtd']}, R$ {t['valor_total']})."
            )
    return problemas


def agregar_por_codigo(itens: list[dict]) -> list[dict]:
    """Soma linhas repetidas do mesmo código (não esperado neste layout, mas a
    chave (cod, data, loja) do banco exige uma linha por código)."""
    por_cod: dict[str, dict] = {}
    for it in itens:
        cod = it["cod_produto"]
        if cod not in por_cod:
            por_cod[cod] = {
                "cod_produto": cod,
                "descricao": it["descricao"],
                "quantidade_saida": Decimal(0),
                "valor_saida": Decimal(0),
            }
        por_cod[cod]["quantidade_saida"] += it["qtd"] or Decimal(0)
        por_cod[cod]["valor_saida"] += it["valor_total"] or Decimal(0)
    return list(por_cod.values())
