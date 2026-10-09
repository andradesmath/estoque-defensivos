"""
scripts/sync_transferencias_sgi.py - Sincroniza do SGI DESKTOP as TRANSFERÊNCIAS DE
SAÍDA da Porteira para Piatã (Relatórios > Relação de Transferências), que DIMINUEM o
saldo monitorado.

Roda local, pelo mesmo motivo do robô de compras: esse relatório só existe no SGI
desktop e o banco do SGI não aceita conexão direta. Toda a mecânica comum (abrir,
logar, empresa certa, janela em primeiro plano, datas, exportar) vem de
estoque/sgi_desktop.py — aqui fica só o que é desta tela.

Uma diferença importante em relação a Compras, que simplifica tudo: esta tela aceita um
PERÍODO e o arquivo traz a data em cada linha (coluna DT_SAIDA). Então uma única
exportação cobre o intervalo inteiro — não há ciclo dia a dia. A gravação apaga o
período e regrava (db.substituir_transferencias_periodo), de modo que uma transferência
estornada no SGI também desaparece do nosso lado.

Filtros da tela: "Transferido para" (combo, = PORTEIRA PIATA), Data Inicial, Data Final.
O arquivo sai em CSV — é o único formato que o botão "Gerar Arq." oferece aqui.

ATENÇÃO à contagem em dobro: hoje essas saídas são lançadas à mão como ajuste no
painel. Ao ligar este robô, os ajustes manuais dos mesmos dias precisam ser excluídos,
senão o estoque é descontado duas vezes — o robô avisa (`!! ATENCAO`) quando encontra
um ajuste negativo no mesmo dia/produto.

Requisitos: Python 32-bit (mesma arquitetura do SGI.exe) —
    py -3.11-32 -m pip install -r requirements-local-robo-compras.txt

Sem --desde, o robô começa na última transferência já gravada (menos a janela de dias
recentes) e vai até hoje - então os dias em que o notebook ficou desligado entram na
próxima execução sozinhos, sem precisar de tabela de controle (ver _inicio_automatico).

Uso:
    py -3.11-32 scripts\\sync_transferencias_sgi.py --dry-run           # mostra, não grava
    py -3.11-32 scripts\\sync_transferencias_sgi.py                     # grava
    py -3.11-32 scripts\\sync_transferencias_sgi.py --desde 22/09/2026  # força o início
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

RAIZ_PROJETO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_PROJETO)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(RAIZ_PROJETO, ".env"))
except ImportError:
    pass

from estoque import parser_transferencias_sgi as parser  # noqa: E402
from estoque import sgi_desktop  # noqa: E402
from estoque.sgi_desktop import RoboIndisponivel  # noqa: E402
from estoque.util import hoje_brasil  # noqa: E402

sgi_desktop.ativar_dpi_aware()  # antes de qualquer janela ser tocada

TITULO_RELATORIO = "Relação de Transferências"
MENU_RELATORIO = "Relatórios->Relação de Transferências"
PASTA_EXPORT = Path(os.environ.get("SYNC_COMPRAS_PASTA", r"D:\SGI\export_compras"))
DESTINO = os.environ.get("SYNC_TRANSF_DESTINO", "PORTEIRA PIATA")
DATA_INICIAL_PADRAO = os.environ.get("SYNC_DATA_INICIAL", "22/09/2026")


def _combo_destino(rep):
    """Combo "Transferido para". Mesmo tipo do combo de empresa da tela de login
    (TDBLookupComboBox): não expõe texto e não responde às mensagens CB_* - por isso a
    seleção passa por sgi_desktop._definir_empresa, que já sabe lidar com ele."""
    combos = [c for c in rep.descendants() if "combobox" in (c.class_name() or "").lower()]
    if not combos:
        raise RoboIndisponivel(
            "Não achei o combo 'Transferido para' na tela de transferências "
            f"(controles: {sorted({c.class_name() for c in rep.descendants()})})."
        )
    combos.sort(key=lambda w: w.rectangle().top)  # o de destino é o de cima
    return combos[0]


def _inicio_automatico(janela: int) -> date:
    """De quando começar quando ninguém passou --desde (caso do agendamento diário).

    Volta `janela` dias antes da última transferência já gravada. Como a gravação é por
    período (apaga o intervalo e regrava), isso cobre sozinho os dias em que o notebook
    ficou desligado - não existe "dia perdido" para recuperar depois. A janela extra
    rebusca os dias recentes, para pegar lançamento que o SGI recebeu com atraso ou que
    foi estornado.

    Sem nada gravado ainda (primeira execução), começa em DATA_INICIAL_PADRAO - a data
    em que o controle de estoque começou; antes dela não há saldo com que comparar."""
    padrao = datetime.strptime(DATA_INICIAL_PADRAO, "%d/%m/%Y").date()
    try:
        from estoque import db
        ultima = db.ultima_data_transferencia(DESTINO)
    except Exception as e:  # noqa: BLE001 - sem banco, o padrão ainda dá um período válido
        print(f"  (não deu pra consultar o banco pra saber de quando começar: {e})")
        return padrao
    if ultima is None:
        return padrao
    return max(padrao, ultima - timedelta(days=janela))


def exportar_periodo(app, rep, inicio: date, fim: date) -> Path:
    """Preenche destino + período, busca e exporta - uma vez só para todo o intervalo."""
    dtp_ini, dtp_fim = sgi_desktop.campos_data(rep)
    sgi_desktop.definir_data(dtp_ini, inicio)
    sgi_desktop.definir_data(dtp_fim, fim)

    rep.child_window(title_re="&?Buscar.*", class_name="TBitBtn").click()
    # A consulta é SÍNCRONA: o SGI congela a própria interface enquanto fala com o banco
    # remoto. Em Compras um sleep curto bastava (consulta de um dia); aqui é o período
    # inteiro, e dormir um tempo chutado fazia o clique seguinte ("Gerar Arq.") ficar na
    # fila de mensagens - o diálogo de salvar só abria depois do timeout de quem o
    # esperava. Então esperamos a janela VOLTAR A RESPONDER, não um relógio.
    print("  [SGI] consultando o período (pode demorar)...")
    if not sgi_desktop.esperar_janela_responder(rep, timeout=180):
        raise RoboIndisponivel(
            "A consulta do período não terminou em 3 min - o SGI continua travado. "
            "Tente um período menor com --desde/--ate.")
    time.sleep(0.5)  # respiro pra grade terminar de pintar depois que a thread volta

    destino = PASTA_EXPORT / f"transferencias_{inicio:%Y%m%d}_{fim:%Y%m%d}.csv"
    return sgi_desktop.exportar_arquivo(app, rep, destino)


def _avisar_duplicatas(por_dia: dict) -> None:
    """Avisa quando um produto monitorado já tem SAÍDA manual lançada no mesmo dia.

    Espelha o aviso do robô de compras, no outro sentido: transferência e ajuste
    negativo descontam os dois do saldo, então conviver com ambos tira o dobro. Como
    essas transferências eram lançadas à mão até agora, é o caso esperado no primeiro
    uso. Falha de banco aqui não derruba a sincronização - o aviso é um extra."""
    try:
        from estoque import db as _db
    except Exception as e:  # noqa: BLE001
        print(f"  (não deu pra checar lançamentos manuais duplicados: {e})")
        return
    total = 0
    for dia, linhas in por_dia.items():
        try:
            dups = _db.possiveis_duplicatas_transferencia(dia, [l["cod_produto"] for l in linhas])
        except Exception as e:  # noqa: BLE001
            print(f"  (não deu pra checar lançamentos manuais em {dia:%d/%m}: {e})")
            return
        for d in dups:
            total += 1
            print(f"  !! ATENCAO {dia:%d/%m/%Y}: {d['cod_produto']} {d['descricao']} já tem "
                  f"ajuste manual #{d['id']} de {d['quantidade']} ({d['tipo']}"
                  f"{', ' + d['observacao'] if d['observacao'] else ''}) no mesmo dia - "
                  "vai descontar em dobro com esta transferência. Exclua o ajuste no painel.")
    if total:
        print(f"  !! {total} lançamento(s) manual(is) podem duplicar o desconto - veja acima.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Sincroniza TRANSFERÊNCIAS DE SAÍDA (Porteira -> Piatã) do SGI desktop.")
    ap.add_argument("--desde", help="Data inicial, DD/MM/AAAA (padrão: da última "
                                    "transferência gravada menos --janela).")
    ap.add_argument("--ate", help="Data final, DD/MM/AAAA (padrão: hoje).")
    ap.add_argument("--janela", type=int, help="Dias recentes sempre rebuscados (padrão: 3).")
    ap.add_argument("--dry-run", action="store_true", help="Mostra o que entraria, sem gravar.")
    args = ap.parse_args(argv)

    janela = args.janela if args.janela is not None else int(os.environ.get("SYNC_JANELA_DIAS", "3"))
    inicio = (datetime.strptime(args.desde, "%d/%m/%Y").date() if args.desde
              else _inicio_automatico(janela))
    fim = datetime.strptime(args.ate, "%d/%m/%Y").date() if args.ate else hoje_brasil()
    if inicio > fim:
        print(f"ERRO: início ({inicio:%d/%m/%Y}) é depois do fim ({fim:%d/%m/%Y}).")
        return 1

    try:
        app, rep = sgi_desktop.conectar_relatorio(TITULO_RELATORIO, None, MENU_RELATORIO)
    except RoboIndisponivel as e:
        print(f"ERRO: {e}")
        return 1

    # O destino ("Transferido para") é escolhido como a empresa no login: o controle é
    # o mesmo TDBLookupComboBox, que só seleciona pela linha sob o mouse.
    print(f"  [SGI] destino: {DESTINO}")
    sgi_desktop._definir_empresa(_combo_destino(rep), DESTINO)

    print(f"Período: {inicio:%d/%m/%Y} a {fim:%d/%m/%Y} (uma exportação só)")
    try:
        arq = exportar_periodo(app, rep, inicio, fim)
    except RoboIndisponivel as e:
        print(f"ERRO: {e}")
        return 1

    rel = parser.extrair_transferencias(arq.read_bytes())
    if rel.aviso:
        print(f"  aviso: {rel.aviso}")
    por_dia = parser.agrupar_por_dia(rel)
    if not por_dia:
        print("Nenhuma transferência no período - nada a gravar.")
        return 0

    _avisar_duplicatas(por_dia)

    total_itens = sum(len(l) for l in por_dia.values())
    print(f"\n{len(por_dia)} dia(s), {total_itens} linha(s):")
    for dia, linhas in por_dia.items():
        print(f"  {dia:%d/%m/%Y}: {len(linhas)} produto(s)")
        for l in linhas:
            print(f"    {l['cod_produto']}  {l['descricao'][:40]:<40} "
                  f"qtd={l['quantidade_transferida']:g}  valor=R${l['valor_transferido']:.2f}")

    if args.dry_run:
        print("\n[dry-run] nada gravado.")
        return 0

    from estoque import db
    # Garante tabela + view antes de gravar. Sem isto, a primeira execução real morria
    # com 'relation "movimentacao_transferencia" does not exist' DEPOIS de exportar e
    # processar tudo (09/10/2026): o schema.sql tinha a tabela, o banco de produção não
    # - ninguém roda migração aqui, as tabelas nascem quando o código novo sobe.
    try:
        feitos = db.garantir_schema_transferencias()
        print(f"  [banco] schema conferido ({len(feitos)} comando(s) idempotente(s))")
    except Exception as e:  # noqa: BLE001
        print(f"ERRO: não consegui garantir a tabela/view de transferências: {e}")
        return 1

    out = db.substituir_transferencias_periodo(DESTINO, inicio, fim, por_dia)
    print(f"\nGravado: {out['gravados']} linha(s) em {out['dias']} dia(s) "
          f"(o período foi regravado; {out['removidos']} linha(s) antigas substituídas).")
    return 0


if __name__ == "__main__":
    try:
        codigo = main()
    finally:
        sgi_desktop.liberar_topmost()
    sys.exit(codigo)
