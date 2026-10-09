"""
estoque/sync_core.py - Lógica do sync SEM navegador: escolha dos dias, validação do
PDF e gravação. Fica separada de scripts/sync_sgi.py (Playwright) para ser testada
com PDFs de fixture e sem SGI ao vivo.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

LOJAS = ("Porteira", "Casa de Adubo")


class RelatorioInvalido(Exception):
    def __init__(self, problemas: list[str]):
        self.problemas = problemas
        super().__init__("; ".join(problemas))


def dias_a_sincronizar(inicio: date, hoje: date, ja_sincronizados: set[date], janela: int = 3) -> list[date]:
    """Dias de `inicio` até `hoje` que (a) nunca foram sincronizados ou (b) estão nos
    últimos `janela` dias (vendas de ontem podem ser canceladas/lançadas depois, e o
    dia de hoje muda a cada rodada). Em ordem cronológica.

    Por que não rebuscar tudo sempre: o SGI só gera relatório por período agregado,
    então cada dia é UM relatório por loja; a janela móvel mantém o custo constante
    conforme os meses passam. `--forcar-tudo` no script usa janela = tamanho total."""
    if hoje < inicio:
        return []
    todos = [inicio + timedelta(days=i) for i in range((hoje - inicio).days + 1)]
    recentes = set(todos[-janela:]) if janela > 0 else set()
    return [d for d in todos if d not in ja_sincronizados or d in recentes]


def processar_pdf_dia(
    pdf_bytes: bytes,
    loja: str,
    dia: date,
    gravar: Callable[..., dict],
    grupo: str = "DEFENSIVOS",
    permitir_zerar: bool = False,
) -> dict:
    """Parse -> valida -> grava (idempotente). Lança RelatorioInvalido sem gravar nada se
    o PDF não for confiável. `gravar` é db.substituir_movimentacao_dia (injetável).

    Import de parser_sgi é local (não no topo do módulo) de propósito: parser_sgi usa
    pypdf, que scripts/sync_compras_sgi.py (Python 32-bit, sem pypdf instalado) não
    precisa - só quem realmente processa PDF de vendas (esta função) paga esse custo."""
    from . import parser_sgi
    res = parser_sgi.parse_relatorio_defensivos(pdf_bytes)
    problemas = parser_sgi.validar(res, dia_esperado=dia, grupo_esperado=grupo if res.itens else None)
    # Relatório sem nenhum item: só aceitamos como "dia sem vendas" se o PDF for curto
    # (cabeçalho + rodapé). PDF longo sem itens reconhecidos = parser falhou.
    if not res.itens and not res.totais:
        n_linhas = len(parser_sgi.extrair_linhas_pdf(pdf_bytes))
        if n_linhas > 40:
            problemas.append(f"PDF com {n_linhas} linhas mas nenhum produto reconhecido.")
    if problemas:
        raise RelatorioInvalido(problemas)

    linhas = parser_sgi.agregar_por_codigo(res.itens)
    out = gravar(loja, dia, linhas, permitir_zerar=permitir_zerar)
    out.update({
        "loja": loja,
        "dia": dia,
        "n_produtos": len(linhas),
        "qtd_total": sum(l["quantidade_saida"] for l in linhas),
        "valor_total": sum(l["valor_saida"] for l in linhas),
    })
    return out


def sincronizar_dias(
    loja: str,
    dias: list[date],
    baixar_pdf: Callable[[date], bytes],
    gravar: Callable[..., dict],
    salvar_pdf: Callable[[bytes, str, date], None] | None = None,
    permitir_zerar: bool = False,
    log: Callable[[str], None] = print,
) -> tuple[list[dict], list[str]]:
    """Um dia por vez; a falha de um dia não interrompe os outros. Retorna
    (resultados_ok, erros)."""
    ok, erros = [], []
    for dia in dias:
        try:
            pdf = baixar_pdf(dia)
            if salvar_pdf:
                salvar_pdf(pdf, loja, dia)
            r = processar_pdf_dia(pdf, loja, dia, gravar, permitir_zerar=permitir_zerar)
            ok.append(r)
            log(f"[{loja}] {dia:%d/%m/%Y}: {r['n_produtos']} produto(s), {r['qtd_total']} un., "
                f"R$ {r['valor_total']:,.2f} (removidos: {r['removidos']})")
        except Exception as e:  # noqa: BLE001 - registra e segue para o próximo dia
            erros.append(f"{loja} {dia:%d/%m/%Y}: {e}")
            log(f"[{loja}] {dia:%d/%m/%Y}: ERRO - {e}")
    return ok, erros
