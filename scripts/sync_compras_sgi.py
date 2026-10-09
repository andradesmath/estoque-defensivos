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
import ctypes
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

RAIZ_PROJETO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_PROJETO)

# Sem isto, se o Windows estiver com escala de tela > 100% (comum em notebook), o
# Python 32-bit (processo nao "DPI-aware") enxerga coordenadas de tela numa escala
# diferente da real - cliques sinteticos do pywinauto (click_input) calculados a partir
# de rectangle() ficam levemente errados, acertando ou errando o alvo de forma
# inconsistente entre execucoes (foi exatamente o sintoma visto em 09/10/2026 testando
# o combo Empresa: as mesmas coordenadas abriam o dropdown numa execucao e nao noutra).
# Precisa rodar ANTES de qualquer janela ser tocada pelo pywinauto.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
except Exception:  # noqa: BLE001 - Windows antigo sem shcore, ou ja setado - nao impede seguir
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(RAIZ_PROJETO, ".env"))
except ImportError:
    pass

from pywinauto import Application, Desktop  # noqa: E402
from pywinauto.findwindows import ElementNotFoundError  # noqa: E402
from pywinauto.timings import TimeoutError as PywinautoTimeoutError  # noqa: E402

from estoque import parser_compras_sgi, sync_core  # noqa: E402
from estoque.util import hoje_brasil  # noqa: E402

TITULO_PRINCIPAL = "SGI - Sistema de Ger"
TITULO_RELATORIO = "Relação de Custo de Compras"
CLASSE_RELATORIO = "TF_REL_CUSTO_DE_COMPRAS"
PASTA_EXPORT = Path(os.environ.get("SYNC_COMPRAS_PASTA", r"D:\SGI\export_compras"))
LOJA = "Porteira"  # so Porteira compra; o saldo e compartilhado entre as lojas (ver schema.sql)
SGI_EXE = Path(os.environ.get("SGI_EXE_PATH", r"D:\SGI\SGI.exe"))
# Abrir o atalho (nao o SGI.exe cru): o .lnk carrega com o "Iniciar em" apontando pra
# D:\SGI, onde ficam recursos com caminho relativo (ex. fundo.jpg) - rodar o .exe
# direto sem esse diretorio de trabalho certo deu erro "Cannot open file fundo.jpg"
# (confirmado 09/10/2026).
SGI_ATALHO = Path(os.environ.get("SGI_ATALHO_PATH", r"C:\Users\Admin\Desktop\SGI - Atalho.lnk"))
TITULO_LOGIN = "Senha..."
# Empresa certa pro relatorio de Compras (compra consolidada das duas lojas) - confirmado
# com o usuario em 09/10/2026, dentre as opcoes do combo: ALAOR SILVA RIBEIRO, CASA DE
# ADUBOS(ANTIGA), PORTEIRA AGROCOMERCIAL, PORTEIRA PIATA, CASA DE ADUBOS CAFE BOM (esta
# ultima vem selecionada por padrao na tela - NAO e a certa pra esse relatorio).
EMPRESA_LOGIN = os.environ.get("SGI_EMPRESA", "PORTEIRA AGROCOMERCIAL")


class RoboIndisponivel(Exception):
    """SGI nao esta num estado em que o robo possa confiar no que esta vendo - para em
    vez de adivinhar (nunca tentamos login automatico nem recriar a janela do zero)."""


def _conectar_app():
    return Application(backend="win32").connect(title_re=TITULO_PRINCIPAL + ".*", timeout=5)


# Ordem fixa dos itens no popup do combo Empresa - vista ao vivo (print_control_
# identifiers + computer-use) identicamente em duas aberturas diferentes do dropdown,
# 09/10/2026. O popup e um TPopupDataList que desenha as proprias linhas (nao expoe
# texto via window_text() nos descendentes, por isso nao da pra achar o item por texto -
# so por posicao/indice).
_EMPRESAS_ORDEM = [
    "ALAOR SILVA RIBEIRO",
    "CASA DE ADUBOS(ANTIGA)",
    "PORTEIRA AGROCOMERCIAL",
    "PORTEIRA PIATA",
    "CASA DE ADUBOS CAFE BOM",
]


def _definir_empresa(campo, texto: str) -> None:
    """Confirmado via print_control_identifiers() em 09/10/2026: Empresa e um
    TDBLookupComboBox (combo ligado a um dataset), NAO um TComboBox nativo - por isso
    nem .select() nem as mensagens CB_* (CB_GETCOUNT voltou vazio) funcionam nele. O
    dropdown dele abre como uma janela popup separada (TPopupDataList) que desenha as
    proprias linhas - clica na seta do combo pra abrir, acha a janela nova que aparece,
    e clica na linha certa por posicao (ordem fixa em _EMPRESAS_ORDEM)."""
    alvo = texto.strip().upper()
    indices = [i for i, nome in enumerate(_EMPRESAS_ORDEM) if nome.strip().upper() == alvo]
    if not indices:
        raise RoboIndisponivel(
            f"Empresa {texto!r} nao esta na ordem conhecida do popup (_EMPRESAS_ORDEM={_EMPRESAS_ORDEM})."
        )
    indice = indices[0]

    # Garante que a janela de login esta em primeiro plano antes de interagir - uma
    # tentativa sintetica pode nao abrir o dropdown se o dialogo pai nao estiver ativo.
    try:
        campo.top_level_parent().set_focus()
    except Exception:  # noqa: BLE001 - foco e so uma ajuda, nao impede a tentativa
        pass
    time.sleep(0.2)

    rect = campo.rectangle()

    def _esperar_popup() -> list:
        novas = []
        for _ in range(10):
            time.sleep(0.2)
            novas = [w for w in Desktop(backend="win32").windows() if w.handle not in antes]
            if novas:
                break
        return novas

    # So clicar na seta nao foi confiavel (3 tentativas identicas, 0 sucesso numa
    # execucao, sucesso noutra) - tenta tambem os atalhos de teclado padrao de qualquer
    # combobox do Windows (F4, Alt+Down), que costumam funcionar mesmo em controles
    # customizados que nao respondem as mensagens CB_* nem sempre reagem bem a clique
    # sintetico.
    tentativas = [
        ("clique na seta", lambda: campo.click_input(coords=(rect.width() - 10, rect.height() // 2))),
        ("clique na seta (mais perto da borda)",
         lambda: campo.click_input(coords=(rect.width() - 4, rect.height() // 2))),
        ("tecla F4", lambda: (campo.click_input(coords=(10, rect.height() // 2)), campo.type_keys("{F4}"))),
        ("Alt+Down", lambda: (campo.click_input(coords=(10, rect.height() // 2)), campo.type_keys("%{DOWN}"))),
    ]
    novas = []
    descricoes_tentadas = []
    for nome, acao in tentativas:
        antes = {w.handle for w in Desktop(backend="win32").windows()}
        acao()
        novas = _esperar_popup()
        descricoes_tentadas.append(nome)
        if novas:
            break
    if not novas:
        raise RoboIndisponivel(
            f"Tentei abrir o dropdown da Empresa de {len(descricoes_tentadas)} jeitos "
            f"({', '.join(descricoes_tentadas)}) e nenhum popup apareceu."
        )
    popup = novas[0]

    prect = popup.rectangle()
    altura_linha = prect.height() / len(_EMPRESAS_ORDEM)
    y = int((indice + 0.5) * altura_linha)
    popup.click_input(coords=(prect.width() // 2, y))
    time.sleep(0.3)

    atual = campo.window_text()
    if texto not in atual:
        raise RoboIndisponivel(
            f"Campo Empresa ficou {atual!r} depois de clicar na linha {indice} do popup "
            f"(esperado {texto!r}; popup rect={prect}, altura_linha={altura_linha:.1f})."
        )


def _logar_sgi() -> None:
    """Abre o SGI.exe e faz login sozinho (SGI_LOGIN + SGI_SENHA_DESKTOP do .env). So
    roda quando o SGI nao estava aberto - NUNCA mata/reinicia uma sessao ja logada.

    SGI_SENHA_DESKTOP (nao SGI_SENHA): a senha do app desktop e DIFERENTE da senha do
    portal web (confirmado pelo usuario em 09/10/2026) - SGI_SENHA e usada por
    scripts/sync_sgi.py (robo de vendas via portal web, GitHub Actions) e nao deve ser
    sobrescrita por essa senha diferente."""
    usuario = os.environ.get("SGI_LOGIN")
    senha = os.environ.get("SGI_SENHA_DESKTOP")
    if not usuario or not senha:
        raise RoboIndisponivel(
            "SGI nao esta aberto e SGI_LOGIN/SGI_SENHA_DESKTOP nao estao no .env - "
            "nao da pra logar sozinho. Abra e logue manualmente."
        )
    if not SGI_ATALHO.exists():
        raise RoboIndisponivel(
            f"Atalho do SGI nao encontrado em {SGI_ATALHO} (ajuste SGI_ATALHO_PATH no .env)."
        )

    # os.startfile (nao Application.start, que so sabe rodar um .exe direto sem
    # resolver o atalho): abre como um duplo-clique no .lnk, com o diretorio de
    # trabalho certo.
    os.startfile(str(SGI_ATALHO))
    try:
        app = Application(backend="win32").connect(title_re=TITULO_PRINCIPAL + ".*", timeout=40)
    except (ElementNotFoundError, PywinautoTimeoutError):
        raise RoboIndisponivel(
            "SGI abriu (via atalho) mas a janela principal nao apareceu em 40s - tela de "
            "login pode ter um layout diferente do esperado, ou algum dialogo (ex. erro) "
            "ficou aberto bloqueando. Confira a tela."
        )
    # A tela de login ("Senha...") e uma janela PROPRIA (dialogo top-level do mesmo
    # processo), NAO campos dentro da janela principal - confirmado ao vivo em
    # 09/10/2026 (main.children(class_name="Edit") nao achava nada, por isso o login
    # nunca rodava e o fluxo seguia pro menu com o dialogo bloqueando tudo).
    login = app.window(title=TITULO_LOGIN)
    try:
        login.wait("visible", timeout=15)
    except PywinautoTimeoutError:
        raise RoboIndisponivel(
            f"SGI abriu mas a janela de login ('{TITULO_LOGIN}') nao apareceu em 15s."
        )

    # "Edit" generico nao bateu (achei 0) - mesmo padrao dos outros controles Delphi
    # (TDateTimePicker, TBitBtn): provavelmente a classe real e "TEdit". Filtra por
    # "edit" no nome em vez de exigir o nome exato.
    todos = login.children()
    campos = [c for c in todos if "edit" in c.class_name().lower()]
    if len(campos) != 2:
        raise RoboIndisponivel(
            f"Esperava 2 campos (Usuario/Senha) na tela de login, achei {len(campos)} "
            f"(classes vistas: {[c.class_name() for c in todos]})."
        )
    campos.sort(key=lambda w: w.rectangle().left)
    campo_usuario, campo_senha = campos  # esquerda = Usuario, direita = Senha (mesma linha)
    campo_usuario.set_edit_text(usuario)
    campo_senha.set_edit_text(senha)

    combos = [c for c in todos if "combobox" in c.class_name().lower()]
    if len(combos) != 2:
        raise RoboIndisponivel(
            f"Esperava 2 campos tipo combo (Empresa/Modulo) na tela de login, achei "
            f"{len(combos)} (classes vistas: {[c.class_name() for c in todos]})."
        )
    combos.sort(key=lambda w: w.rectangle().left)
    campo_empresa, _campo_modulo = combos  # esquerda = Empresa, direita = Modulo
    _definir_empresa(campo_empresa, EMPRESA_LOGIN)

    login.child_window(title_re="&?Confirmar", class_name="TBitBtn").click()
    time.sleep(2.0)


def conectar_relatorio():
    """Garante SGI aberto+logado com a janela do relatorio disponivel, e devolve (app, rep).
    `app` fica pra achar outras janelas de nivel superior depois (ex.: o dialogo
    "Exportar Dados") - um wrapper ja resolvido (`rep.top_level_parent()`) nao serve
    pra isso, so uma WindowSpecification (app.window(...)) tem .child_window().

    Ordem: (1) SGI ja aberto + relatorio ja aberto -> so reaproveita (caminho mais
    robusto, sem digitar senha nem navegar menu); (2) SGI aberto mas relatorio fechado
    -> abre pelo menu Relatorios>Compras>Relacao de Custo de Compras (confirmado
    manualmente em 09/10/2026); (3) SGI fechado -> abre e loga sozinho (ver _logar_sgi,
    NAO testado ao vivo ainda) e cai no caso (2)."""
    try:
        app = _conectar_app()
    except (ElementNotFoundError, PywinautoTimeoutError):
        _logar_sgi()
        try:
            app = _conectar_app()
        except (ElementNotFoundError, PywinautoTimeoutError) as e:
            raise RoboIndisponivel("SGI nao abriu/logou a tempo.") from e

    main = app.window(title_re=TITULO_PRINCIPAL + ".*")
    rep = main.child_window(title=TITULO_RELATORIO, class_name=CLASSE_RELATORIO)
    if not rep.exists():
        # Logo apos o login, o SGI mostra um splash "Conectado! Carregando inventarios"
        # que deixa o menu principal desabilitado ate terminar - sem esperar isso,
        # menu_select da ElementNotEnabled (visto ao vivo em 09/10/2026). Checagem
        # barata (is_enabled() so le o estado da janela) - nao atrapalha o caminho em
        # que o SGI ja estava pronto havia tempo.
        fim = time.time() + 60.0
        while not main.is_enabled():
            if time.time() > fim:
                raise RoboIndisponivel(
                    "SGI nao ficou pronto (menu habilitado) em 60s depois do login - "
                    "pode estar travado carregando inventarios."
                )
            time.sleep(1.0)
        main.menu_select("Relatórios->Compras->Relação de Custo de Compras")
        rep = main.child_window(title=TITULO_RELATORIO, class_name=CLASSE_RELATORIO)
        try:
            rep.wait("exists", timeout=15)
        except PywinautoTimeoutError:
            raise RoboIndisponivel(
                f"Cliquei no menu mas a janela '{TITULO_RELATORIO}' nao apareceu em 15s "
                "(o caminho do menu pode ter mudado)."
            )
    rep.restore()  # garante que da pra interagir mesmo se estava minimizada
    return app, rep


def _campos_data(rep):
    dtps = rep.children(class_name="TDateTimePicker")
    if len(dtps) != 2:
        raise RoboIndisponivel(f"Esperava 2 campos de data na tela, achei {len(dtps)} "
                                "(o layout do relatorio pode ter mudado).")
    dtps.sort(key=lambda w: w.rectangle().left)
    return dtps[0], dtps[1]  # esquerda = Data Inicial, direita = Data Final


def _definir_data(campo, dia: date, tentativas: int = 3) -> None:
    """Foca o campo e digita dia/mes/ano, avançando de segmento com {RIGHT} explícito
    em vez de confiar no auto-avanço do TDateTimePicker a cada 2 dígitos: isso se
    mostrou pouco confiável (pedido 02/10/2026 saiu 26/10/2026 de forma repetível,
    inclusive com pausa maior entre teclas - não era timing, o auto-avanço comeu/
    trocou o segmento do dia). Confere lendo de volta - a unica forma de saber se
    realmente pegou, sem alguem olhando a tela."""
    esperado = dia.strftime("%d/%m/%Y")
    for tentativa in range(1, tentativas + 1):
        campo.set_focus()
        campo.type_keys("{HOME}")
        campo.type_keys(f"{dia:%d}")
        campo.type_keys("{RIGHT}")
        campo.type_keys(f"{dia:%m}")
        campo.type_keys("{RIGHT}")
        campo.type_keys(f"{dia:%Y}")
        time.sleep(0.3)
        texto = campo.window_text()
        if esperado in texto:
            return
    raise RoboIndisponivel(f"Campo de data ficou {texto!r} depois de {tentativas} tentativa(s), esperado {esperado}.")


def _exportar_xls(app, rep, dia: date) -> Path:
    PASTA_EXPORT.mkdir(parents=True, exist_ok=True)
    destino = PASTA_EXPORT / f"compras_{dia:%Y%m%d}.xls"
    if destino.exists():
        destino.unlink()  # evita o popup de "sobrescrever?" no Explorer

    rep.child_window(title="&Gerar Arq.", class_name="TBitBtn").click()
    dlg = app.window(title_re="Exportar Dados.*")
    dlg.wait("visible", timeout=10)
    campo_nome = dlg.child_window(class_name="Edit", found_index=0)
    campo_nome.set_edit_text(str(destino))
    # Enter no campo confirma o diálogo (Salvar/OK), sem depender do texto exato do
    # botão - diálogo comum do Windows (#32770), cuja legenda do botão varia
    # ("Salvar" sem o "&" não bateu; em vez de caçar a grafia certa, usa Enter).
    campo_nome.type_keys("{ENTER}")

    for _ in range(20):
        if destino.exists() and destino.stat().st_size > 0:
            return destino
        time.sleep(0.5)
    raise RoboIndisponivel(f"Arquivo {destino} nao apareceu apos clicar em Gerar Arq.")


def processar_dia(app, rep, dia: date, gravar, dry_run: bool) -> dict:
    dtp_ini, dtp_fim = _campos_data(rep)
    _definir_data(dtp_ini, dia)
    _definir_data(dtp_fim, dia)
    rep.child_window(title="&Buscar  ", class_name="TBitBtn").click()
    time.sleep(1.0)  # consulta no banco remoto (138.255.35.101) - da um tempo antes de exportar

    arq = _exportar_xls(app, rep, dia)
    rel = parser_compras_sgi.extrair_compras(arq.read_bytes())
    if rel.aviso and not rel.itens:
        print(f"  [{dia:%d/%m/%Y}] aviso: {rel.aviso}")
    linhas = parser_compras_sgi.para_linhas_movimentacao(rel)

    if dry_run:
        print(f"  [dry-run] {dia:%d/%m/%Y}: {len(linhas)} produto(s) - nada gravado.")
        for l in linhas:
            print(f"    {l['cod_produto']}  {l['descricao']:<30}  qtd={l['quantidade_entrada']:g}  "
                  f"valor=R${l['valor_entrada']:.2f}")
        return {"gravados": len(linhas), "removidos": 0}

    out = gravar(LOJA, dia, linhas)
    print(f"  [{LOJA}] {dia:%d/%m/%Y}: {len(linhas)} produto(s) (removidos: {out['removidos']})")
    return out


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
    erros = []
    for dia in dias:
        try:
            processar_dia(app, rep, dia, gravar, args.dry_run)
        except Exception as e:  # noqa: BLE001 - um dia ruim nao pode travar os outros
            erros.append(f"{dia:%d/%m/%Y}: {e}")
            print(f"  [{dia:%d/%m/%Y}] ERRO: {e}")

    if erros:
        print("\nFalhas nessa execucao:")
        for e in erros:
            print(" -", e)
        return 1
    print("\nSincronizacao de compras concluida sem erros.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
