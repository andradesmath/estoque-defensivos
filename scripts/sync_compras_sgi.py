"""
scripts/sync_compras_sgi.py - Sincroniza do SGI DESKTOP (nao do portal web) as ENTRADAS POR
COMPRA de DEFENSIVOS, sempre pela empresa PORTEIRA (compra para as duas lojas - o saldo e
compartilhado entre elas, entao nao precisa logar em "Casa de Adubo" separado).

Por que isto roda LOCAL e nao no GitHub Actions (como sync_sgi.py): o relatorio de Compras
so existe no SGI desktop, que fala com o banco via um canal que rejeita conexao direta
(fora do escopo deste robo - ver conversa de 07/10/2026). A unica automacao possivel e
dirigir a UI do proprio SGI.exe nesta maquina.

Se o SGI ja estiver aberto, logado, com a janela "Relacao de Custo de Compras" aberta
(pode estar minimizada), o robo so reaproveita tudo isso - e o caminho mais robusto
(nao depende de login nem de navegar menu). SE NAO ESTIVER: o robo abre o SGI.exe
sozinho (SGI_LOGIN/SGI_SENHA_DESKTOP do .env - senha diferente da do SGI_SENHA usado
pelo portal web), espera logar, e abre o relatorio pelo menu
Relatorios > Compras > Relacao de Custo de Compras (confirmado manualmente em
09/10/2026 - e esse o caminho certo). Isso cobre o caso de rodar com --dia logo que
o Windows liga (Agendador de Tarefas, gatilho "ao fazer logon"), sem voce precisar
abrir nada manualmente.

Mecanica por dia (Data Inicial = Data Final = o dia, como no sync de vendas - o relatorio
e agregado no periodo, entao um relatorio por dia e o jeito de saber o dia de cada entrada):
    1. digita a data nos dois TDateTimePicker (confere lendo de volta - nao segue calado
       se a data nao "pegou")
    2. clica Buscar
    3. clica Gerar Arq., preenche o caminho completo na caixa "Exportar Dados" (Explorer
       aceita caminho completo direto, sem navegar pasta por pasta), Salvar
    4. le o .xls com parser_compras_sgi e grava com db.substituir_movimentacao_entrada_dia
       (upsert idempotente por dia - rodar de novo nao soma a mesma compra duas vezes)

Toda a mecanica comum (abrir o SGI, logar, escolher a empresa, achar a janela do
relatorio, preencher datas, exportar) vive em estoque/sgi_desktop.py, compartilhada com
o robo de transferencias - inclusive os comentarios que explicam POR QUE cada passo e
daquele jeito. Aqui fica so o que e especifico de Compras.

Requisitos (Python 32-bit - mesma arquitetura do SGI.exe, senao pywinauto nao enxerga os
controles direito):
    py -3.11-32 -m pip install pywinauto pandas xlrd sqlalchemy psycopg2-binary python-dotenv

Uso (sempre em D:\\SGI, com o SGI aberto e logado e a janela do relatorio aberta):
    py -3.11-32 scripts\\sync_compras_sgi.py --dry-run --dia 01/10/2026   # 1 dia, so mostra
    py -3.11-32 scripts\\sync_compras_sgi.py --dry-run                    # janela toda, so mostra
    py -3.11-32 scripts\\sync_compras_sgi.py                              # grava no banco
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

RAIZ_PROJETO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_PROJETO)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(RAIZ_PROJETO, ".env"))
except ImportError:
    pass

from estoque import parser_compras_sgi, sgi_desktop, sync_core  # noqa: E402
from estoque.sgi_desktop import RoboIndisponivel  # noqa: E402
from estoque.util import hoje_brasil  # noqa: E402

sgi_desktop.ativar_dpi_aware()  # antes de qualquer janela ser tocada

TITULO_RELATORIO = "Relação de Custo de Compras"
CLASSE_RELATORIO = "TF_REL_CUSTO_DE_COMPRAS"
MENU_RELATORIO = "Relatórios->Compras->Relação de Custo de Compras"
PASTA_EXPORT = Path(os.environ.get("SYNC_COMPRAS_PASTA", r"D:\SGI\export_compras"))
LOJA = "Porteira"  # so Porteira compra; o saldo e compartilhado entre as lojas (ver schema.sql)
ROBO = "robo-compras"  # coluna `origem` em sync_execucoes (historico de execucoes)


def conectar_relatorio():
    return sgi_desktop.conectar_relatorio(TITULO_RELATORIO, CLASSE_RELATORIO, MENU_RELATORIO)


def processar_dia(app, rep, dia: date, gravar, dry_run: bool) -> dict:
    dtp_ini, dtp_fim = sgi_desktop.campos_data(rep)
    sgi_desktop.definir_data(dtp_ini, dia)
    sgi_desktop.definir_data(dtp_fim, dia)
    rep.child_window(title="&Buscar  ", class_name="TBitBtn").click()
    time.sleep(1.0)  # consulta no banco remoto (138.255.35.101) - da um tempo antes de exportar

    arq = sgi_desktop.exportar_arquivo(
        app, rep, PASTA_EXPORT / f"compras_{dia:%Y%m%d}.xls")
    rel = parser_compras_sgi.extrair_compras(arq.read_bytes())
    if rel.aviso and not rel.itens:
        print(f"  [{dia:%d/%m/%Y}] aviso: {rel.aviso}")
    linhas = parser_compras_sgi.para_linhas_movimentacao(rel)
    _avisar_duplicatas(dia, linhas)

    if dry_run:
        print(f"  [dry-run] {dia:%d/%m/%Y}: {len(linhas)} produto(s) - nada gravado.")
        for l in linhas:
            print(f"    {l['cod_produto']}  {l['descricao']:<30}  qtd={l['quantidade_entrada']:g}  "
                  f"valor=R${l['valor_entrada']:.2f}")
        return {"gravados": len(linhas), "removidos": 0}

    out = gravar(LOJA, dia, linhas)
    print(f"  [{LOJA}] {dia:%d/%m/%Y}: {len(linhas)} produto(s) (removidos: {out['removidos']})")
    return out


def _avisar_duplicatas(dia: date, linhas: list) -> None:
    """Avisa se algum produto MONITORADO dessa compra ja tem entrada manual lancada no
    mesmo dia - os dois somam no saldo (v_saldo_produto soma ajustes E entradas por
    compra), entao seria contagem em dobro. Aconteceu de verdade com o JOINER em
    02/10/2026 (ajuste 'ENTRADA NF' de +12 por cima da compra de 12).

    So checa produtos cadastrados - compra de coleira, racao etc. nao entra no controle
    de defensivos e nao tem como ter ajuste. Falha de banco aqui nunca derruba o dia:
    o aviso e um extra, nao parte da sincronizacao."""
    if not linhas:
        return
    try:
        from estoque import db as _db
        dups = _db.possiveis_duplicatas_compra(dia, [l["cod_produto"] for l in linhas])
    except Exception as e:  # noqa: BLE001
        print(f"  (nao deu pra checar lancamentos manuais duplicados em {dia:%d/%m}: {e})")
        return
    for d in dups:
        print(f"  !! ATENCAO {dia:%d/%m/%Y}: {d['cod_produto']} {d['descricao']} tem ajuste "
              f"manual #{d['id']} de +{d['quantidade']} ({d['tipo']}"
              f"{', ' + d['observacao'] if d['observacao'] else ''}) no mesmo dia - "
              "vai contar em dobro com esta compra. Exclua o ajuste no painel.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sincroniza ENTRADAS POR COMPRA do SGI desktop (Porteira).")
    ap.add_argument("--dia", help="So este dia, DD/MM/AAAA (senao, janela movel normal).")
    ap.add_argument("--desde", help="Data inicial do controle, DD/MM/AAAA (padrao: SYNC_DATA_INICIAL ou 22/09/2026).")
    ap.add_argument("--janela", type=int, help="Dias recentes sempre rebuscados (padrao: 3).")
    ap.add_argument("--permitir-zerar", action="store_true", help="Aceita dia sem compras mesmo se o dia ja tinha.")
    ap.add_argument("--dry-run", action="store_true", help="Exporta e mostra, mas NAO grava no banco.")
    args = ap.parse_args(argv)

    hoje = hoje_brasil()
    db = None
    if args.dia:
        dias = [datetime.strptime(args.dia, "%d/%m/%Y").date()]
    else:
        inicio = datetime.strptime(
            args.desde or os.environ.get("SYNC_DATA_INICIAL", "22/09/2026"), "%d/%m/%Y").date()
        janela = args.janela if args.janela is not None else int(os.environ.get("SYNC_JANELA_DIAS", "3"))
        feitos = set()
        if not args.dry_run:
            from estoque import db
            try:
                db.init_schema()
            except Exception as e:  # noqa: BLE001
                # init_schema roda um script com varios "CREATE ... IF NOT EXISTS" numa
                # chamada so - funciona com psycopg2 (libpq simple query protocol), nao
                # testado ainda com pg8000 (que o robo usa - ver estoque/db.py). As
                # tabelas ja existem em producao (criadas pelo painel/GitHub Actions,
                # que usam psycopg2), entao pular aqui e seguro - so avisa.
                print(f"AVISO: init_schema() falhou ({e}); seguindo sem recriar schema "
                      "(ja deve existir, criado pelo painel).")
                # Garante ao menos o que os robos precisam, comando a comando (o que o
                # pg8000 aceita e o script inteiro nao) - entre eles sync_execucoes, o
                # historico de execucoes.
                try:
                    db.garantir_schema_robos()
                except Exception as e2:  # noqa: BLE001
                    print(f"AVISO: garantir_schema_robos() tambem falhou ({e2}).")
            feitos = db.dias_sincronizados_compras(LOJA)
        dias = sync_core.dias_a_sincronizar(inicio, hoje, feitos, janela)

    if not dias:
        print("Nenhum dia pendente.")
        return 0

    try:
        app, rep = conectar_relatorio()
    except RoboIndisponivel as e:
        print(f"ERRO: {e}")
        return 1

    gravar = None
    if not args.dry_run:
        if db is None:
            from estoque import db
        gravar = lambda loja, dia, linhas: db.substituir_movimentacao_entrada_dia(  # noqa: E731
            loja, dia, linhas, permitir_zerar=args.permitir_zerar)

    print(f"{len(dias)} dia(s) a sincronizar: " + ", ".join(f"{d:%d/%m}" for d in dias))

    total_linhas = 0

    def _rodar(lista):
        """Processa a lista de dias e devolve (falhados, mensagens de erro)."""
        nonlocal total_linhas
        falhou, msgs = [], []
        for dia in lista:
            try:
                out = processar_dia(app, rep, dia, gravar, args.dry_run)
                total_linhas += out.get("gravados", 0)
            except Exception as e:  # noqa: BLE001 - um dia ruim nao pode travar os outros
                falhou.append(dia)
                msgs.append(f"{dia:%d/%m/%Y}: {e}")
                print(f"  [{dia:%d/%m/%Y}] ERRO: {e}")
        return falhou, msgs

    def _registrar(status, detalhe=None):
        """Anota a execucao em sync_execucoes (historico). Compras continua decidindo o
        que buscar por sync_dias_compras, que e dia a dia e mais preciso - este registro
        e so historico/auditoria ("o robo rodou hoje?")."""
        if args.dry_run or db is None:
            return
        db.registrar_execucao_robo(ROBO, min(dias), max(dias), status, dias=len(dias),
                                   linhas=total_linhas, resumo=detalhe)

    falhou, erros = _rodar(dias)

    # Uma segunda passada nos dias que falharam. A exportacao do SGI escorrega de vez
    # em quando - quase sempre no PRIMEIRO dia da execucao, e sempre no mesmo ponto:
    # o .xls as vezes nao aparece a tempo ou fica preso ("arquivo em uso", "arquivo
    # nao encontrado"). Repetir o mesmo dia sempre resolveu, e repetir e seguro
    # porque a gravacao e idempotente (upsert por cod_produto + data + loja).
    if falhou:
        print(f"\nRefazendo {len(falhou)} dia(s) que falharam: "
              + ", ".join(f"{d:%d/%m}" for d in falhou))
        falhou, erros = _rodar(falhou)

    if erros:
        print("\nFalhas nessa execucao (mesmo apos refazer):")
        for e in erros:
            print(" -", e)
        _registrar("erro", detalhe="; ".join(erros)[:500])
        return 1
    print("\nSincronizacao de compras concluida sem erros.")
    _registrar("ok")
    return 0


if __name__ == "__main__":
    try:
        codigo = main()
    finally:
        # Sempre devolve as janelas ao normal - o SGI nao pode ficar "sempre visivel"
        # por cima de tudo depois que o robo termina, nem quando ele termina com erro.
        sgi_desktop.liberar_topmost()
    sys.exit(codigo)
