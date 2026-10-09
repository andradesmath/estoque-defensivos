"""
estoque/parser_transferencias_sgi.py - Lê o .csv exportado pelo SGI em
Relatórios > Relação de Transferências (botão "Gerar Arq.").

Diferente do relatório de Compras, que sai em .xls e é agregado no período (sem data
por linha, por isso lá o robô precisa de um relatório por dia), aqui:
  - o arquivo sai em CSV (é o único formato que o botão oferece nesta tela);
  - cada linha já traz a DATA da saída (DT_SAIDA).
Ou seja, uma única exportação cobre o período inteiro — e o robô não precisa repetir
o ciclo dia a dia.

Formato confirmado no arquivo real (PORTEIRA PIATA, 22/09 a 09/10/2026, 120 linhas):
    DT_SAIDA;COD_PROD;DESCRICAO;UNIDADE;QTD;PREC_COMP;cl_total;
    23/09/2026;09938;MAP GRAO 11-52-00  GRANPHOS 50KG;UN;20;280,00;5.600,00;
UTF-8, separador ';', uma coluna vazia sobrando no fim de cada linha, e números no
padrão brasileiro (ponto de milhar, vírgula decimal).

Sem pandas DE PROPÓSITO, como o parser de compras: este módulo é importado pelo robô
local, que roda em Python de 32 bits (mesma arquitetura do SGI.exe), onde não existe
build pronta de pandas. `csv` é da biblioteca padrão.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime

_COLUNAS_ESPERADAS = {"DT_SAIDA", "COD_PROD", "DESCRICAO", "QTD"}


@dataclass
class ItemTransferencia:
    data: date
    cod_produto: str
    descricao: str
    unidade: str
    quantidade: float
    valor_total: float


@dataclass
class RelatorioTransferencias:
    itens: list[ItemTransferencia] = field(default_factory=list)
    aviso: str | None = None


def _numero_br(valor: str) -> float:
    """'5.600,00' -> 5600.0 ; '280,00' -> 280.0 ; '' -> 0.0.

    O SGI exporta no padrão brasileiro: ponto separa milhar, vírgula separa decimal.
    Ler com float() direto daria 5.6 em vez de 5600."""
    t = (valor or "").strip()
    if not t:
        return 0.0
    t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return 0.0


def _decodificar(dados: bytes) -> str:
    """O arquivo veio em UTF-8 no teste, mas sistemas Delphi antigos às vezes gravam em
    Windows-1252 - tentar as duas evita quebrar por causa de um acento."""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return dados.decode(enc)
        except UnicodeDecodeError:
            continue
    return dados.decode("utf-8", errors="replace")


def extrair_transferencias(csv_bytes: bytes) -> RelatorioTransferencias:
    texto = _decodificar(csv_bytes)
    linhas = list(csv.reader(io.StringIO(texto), delimiter=";"))
    if not linhas:
        return RelatorioTransferencias(itens=[], aviso="Arquivo sem nenhuma linha.")

    cabecalho = [c.strip() for c in linhas[0]]
    idx = {nome: i for i, nome in enumerate(cabecalho) if nome}
    faltando = _COLUNAS_ESPERADAS - set(idx)
    if faltando:
        return RelatorioTransferencias(
            itens=[],
            aviso=f"Colunas esperadas não encontradas: {sorted(faltando)} "
                  "(o layout do relatório pode ter mudado).",
        )

    itens, ignoradas = [], 0
    for linha in linhas[1:]:
        if not linha or not (linha[idx["COD_PROD"]] or "").strip():
            continue
        bruto_data = (linha[idx["DT_SAIDA"]] or "").strip()
        try:
            dia = datetime.strptime(bruto_data, "%d/%m/%Y").date()
        except ValueError:
            ignoradas += 1
            continue
        qtd = _numero_br(linha[idx["QTD"]])
        if qtd == 0:
            continue  # linha sem quantidade não altera saldo
        itens.append(ItemTransferencia(
            data=dia,
            cod_produto=(linha[idx["COD_PROD"]] or "").strip(),
            descricao=(linha[idx["DESCRICAO"]] or "").strip() if "DESCRICAO" in idx else "",
            unidade=(linha[idx["UNIDADE"]] or "").strip() if "UNIDADE" in idx else "",
            quantidade=qtd,
            valor_total=_numero_br(linha[idx["cl_total"]]) if "cl_total" in idx else 0.0,
        ))

    aviso = None
    if not itens:
        aviso = "Nenhuma transferência com quantidade encontrada no período/filtro."
    elif ignoradas:
        aviso = f"{ignoradas} linha(s) com data ilegível foram ignoradas."
    return RelatorioTransferencias(itens=itens, aviso=aviso)


def agrupar_por_dia(rel: RelatorioTransferencias) -> dict[date, list[dict]]:
    """{dia: [linhas prontas pra gravar]}, somando se o mesmo produto aparecer duas
    vezes no mesmo dia (duas transferências do mesmo item). No arquivo real de
    22/09-09/10 não houve repetição, mas somar é o comportamento correto e evita
    perder uma das linhas por colisão de chave (cod_produto + data + destino)."""
    por_dia: dict[date, dict[str, dict]] = {}
    for i in rel.itens:
        do_dia = por_dia.setdefault(i.data, {})
        linha = do_dia.get(i.cod_produto)
        if linha is None:
            do_dia[i.cod_produto] = {
                "cod_produto": i.cod_produto,
                "descricao": i.descricao,
                "quantidade_transferida": i.quantidade,
                "valor_transferido": i.valor_total,
            }
        else:
            # Arredonda na soma, nas mesmas casas das colunas do banco
            # (NUMERIC(14,3) e NUMERIC(14,2)): somar float acumula ruído - 44,30 +
            # 22,15 dava 66.44999999999999.
            linha["quantidade_transferida"] = round(linha["quantidade_transferida"] + i.quantidade, 3)
            linha["valor_transferido"] = round(linha["valor_transferido"] + i.valor_total, 2)
    return {dia: list(linhas.values()) for dia, linhas in sorted(por_dia.items())}
