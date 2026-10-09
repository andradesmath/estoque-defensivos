"""
estoque/sgi_desktop.py - Núcleo compartilhado dos robôs que dirigem o SGI **desktop**
pela interface (pywinauto): abrir o programa, logar na empresa certa, achar a janela do
relatório, preencher datas, exportar o arquivo.

Existe porque há mais de um relatório a sincronizar (Compras e Transferências, por ora)
e tudo que vem antes do relatório é idêntico. Cada robô fica só com o que é seu: a
janela, os filtros próprios, o parser e a gravação.

Cada detalhe aqui foi descoberto testando contra o SGI real em 09/10/2026, e os
comentários registram o porquê — são o que impede alguém (inclusive eu, depois) de
"simplificar" de volta para algo que não funciona:

  - Python 32 bits, DPI-aware, janela em primeiro plano (TOPMOST + AttachThreadInput);
  - a tela de login é uma janela própria ("Senha..."), não campos da janela principal;
  - o combo Empresa é um TDBLookupComboBox: não expõe texto, ignora as mensagens CB_*
    e seleciona pela linha sob o MOUSE (precisa mover o cursor, não só clicar);
  - a empresa logada só se confere pela faixa "Licenciado para ..." do rodapé;
  - as datas entram pela API nativa do controle (DTM_SETSYSTEMTIME), porque navegar
    entre os segmentos por teclado é indeterminado;
  - nenhum clique acontece sem confirmar que a janela sob o cursor é mesmo do SGI.

Sem pandas e sem psycopg2: roda no Python de 32 bits da máquina do SGI (ver
requirements-local-robo-compras.txt).
"""
from __future__ import annotations

import ctypes
import os
import time
from datetime import date
from pathlib import Path

from pywinauto import Application, Desktop, mouse
from pywinauto.findwindows import ElementNotFoundError
from pywinauto.timings import TimeoutError as PywinautoTimeoutError

TITULO_PRINCIPAL = "SGI - Sistema de Ger"
TITULO_LOGIN = "Senha..."
SGI_ATALHO = Path(os.environ.get("SGI_ATALHO_PATH", r"C:\Users\Admin\Desktop\SGI - Atalho.lnk"))
# Empresa certa pros relatorios do grupo (compra e transferencia saem da matriz) -
# confirmado com o usuario em 09/10/2026, dentre as opcoes do combo. CASA DE ADUBOS
# CAFE BOM vem selecionada por padrao na tela e NAO e a certa.
EMPRESA_LOGIN = os.environ.get("SGI_EMPRESA", "PORTEIRA AGROCOMERCIAL")


def ativar_dpi_aware() -> None:
    """Sem isto, com a escala do Windows acima de 100% (comum em notebook), o processo
    32 bits enxerga coordenadas de tela numa escala diferente da real e os cliques
    sinteticos acertam ou erram o alvo de forma inconsistente entre execucoes.
    Precisa rodar ANTES de qualquer janela ser tocada pelo pywinauto."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:  # noqa: BLE001 - Windows antigo sem shcore, ou ja setado
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


class RoboIndisponivel(Exception):
    """SGI nao esta num estado em que o robo possa confiar no que esta vendo - para em
    vez de adivinhar (nunca tentamos login automatico nem recriar a janela do zero)."""


def _conectar_app():
    return Application(backend="win32").connect(title_re=TITULO_PRINCIPAL + ".*", timeout=5)


_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_SHOWWINDOW = 0x0040
_SW_RESTORE = 9

# Janelas que o robo colocou em "sempre visivel" e precisa devolver ao normal no fim.
_TOPMOST_APLICADO: list = []


def trazer_para_frente(janela) -> None:
    """Poe a janela do SGI REALMENTE na frente, e nao so com foco nominal.

    Necessario porque o robo roda a partir do terminal: o SGI abre em segundo plano e
    o Windows recusa SetForegroundWindow vindo de um processo que nao esta em
    primeiro plano. Resultado em 09/10/2026: o popup de empresas ficou atras das
    janelas do Claude/VSCode e o clique teria caido no programa errado.

    Dois mecanismos, porque um so nao basta:
    1. SetWindowPos TOPMOST - poe a janela acima das outras sem depender de foreground;
    2. AttachThreadInput + SetForegroundWindow - da o foco de teclado de verdade
       (o Windows so permite quando a nossa thread esta anexada a da janela alvo).
    O TOPMOST e desfeito no fim da execucao (_liberar_topmost)."""
    u = ctypes.windll.user32
    try:
        hwnd = janela.handle
    except Exception:  # noqa: BLE001
        return
    try:
        u.ShowWindow(hwnd, _SW_RESTORE)
        u.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                       _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW)
        if hwnd not in _TOPMOST_APLICADO:
            _TOPMOST_APLICADO.append(hwnd)
        tid_alvo = u.GetWindowThreadProcessId(hwnd, None)
        tid_nosso = ctypes.windll.kernel32.GetCurrentThreadId()
        u.AttachThreadInput(tid_nosso, tid_alvo, True)
        u.SetForegroundWindow(hwnd)
        u.AttachThreadInput(tid_nosso, tid_alvo, False)
    except Exception:  # noqa: BLE001 - e so uma ajuda; o clique seguro ainda protege
        pass
    time.sleep(0.4)


def liberar_topmost() -> None:
    """Devolve as janelas ao comportamento normal - o SGI nao pode ficar
    'sempre visivel' depois que o robo termina."""
    u = ctypes.windll.user32
    for hwnd in _TOPMOST_APLICADO:
        try:
            u.SetWindowPos(hwnd, _HWND_NOTOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE)
        except Exception:  # noqa: BLE001
            continue
    _TOPMOST_APLICADO.clear()


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _pid_sob_o_ponto(x: int, y: int) -> int:
    """PID do processo dono da janela que esta SOB o ponto (x, y) da tela."""
    hwnd = ctypes.windll.user32.WindowFromPoint(_POINT(x, y))
    pid = ctypes.c_ulong(0)
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _clique_seguro(x: int, y: int, pid_esperado: int, oque: str, trazer_frente=None) -> None:
    """Clica em (x, y) SO SE a janela sob o ponto for do processo do SGI.

    Sem essa guarda, um clique sintetico cai em qualquer app que esteja por cima:
    em 09/10/2026 o robo clicou na janela do Claude porque o SGI perdeu o primeiro
    plano no meio da operacao. Alem de nao selecionar nada, clicar as cegas na tela
    de outro programa e inaceitavel - entao: confere, tenta trazer o SGI pra frente
    uma vez, e se ainda assim o ponto nao for dele, para com erro claro em vez de
    clicar."""
    for tentativa in range(2):
        if _pid_sob_o_ponto(x, y) == pid_esperado:
            mouse.click(button="left", coords=(x, y))
            return
        if tentativa == 0 and trazer_frente is not None:
            try:
                trazer_frente()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.6)
    raise RoboIndisponivel(
        f"Ia clicar em {oque} na posicao ({x}, {y}), mas quem esta ali e outro "
        "programa, nao o SGI (alguma janela ficou por cima). Nao cliquei. Rode de "
        "novo sem mexer no computador enquanto o robo trabalha."
    )


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


def _definir_empresa(campo, texto: str, metodo: str = "clique") -> None:
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

    # Poe a janela de login REALMENTE na frente antes de interagir (ver
    # _trazer_para_frente): rodando a partir do terminal, o SGI fica em segundo plano
    # e tanto o dropdown quanto o clique acabariam atras/em cima de outro programa.
    trazer_para_frente(campo.top_level_parent())

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

    # CLIQUE na linha certa, por posicao. VALIDADO AO VIVO em 09/10/2026: com o popup
    # aberto em (L547,T388,R725,B455) - 67px de altura, 5 itens, ~13.4px por linha -
    # clicar no centro da 3a linha (y=421 absoluto = 33 relativo ao topo, que e
    # exatamente (2+0,5)*13,4) selecionou PORTEIRA AGROCOMERCIAL e o campo passou a
    # exibi-la.
    #
    # Teclado NAO funciona aqui (testado: Home/Down/Enter tanto no popup quanto no
    # campo nao mudam nada - o SGI logou em CASA DE ADUBO assim mesmo). O caminho por
    # teclado chegou a ser adotado por engano porque a verificacao da epoca
    # (campo.window_text()) era impossivel de passar - window_text() de um
    # TDBLookupComboBox e SEMPRE '' (ele desenha o proprio texto a partir do dataset),
    # entao o clique, que ja funcionava, parecia estar falhando.
    # ESPERA a lista renderizar E ficar do tamanho da lista COMPLETA. Sem isso o popup
    # aparece vazio/curto (o usuario capturou a moldura vazia em 09/10/2026) e o
    # clique cai na linha errada ou em nada: a altura do popup e o unico indicador
    # disponivel de quantos itens ja entraram (o conteudo e desenhado, nao da pra ler).
    # Com as 5 empresas o popup mede 67px (~13.4px por linha) - exigir pelo menos
    # ~90% disso garante que todas as linhas estao la antes de mirar numa posicao.
    altura_esperada = 13.4 * len(_EMPRESAS_ORDEM)
    for _ in range(15):
        time.sleep(0.3)
        try:
            if popup.rectangle().height() >= altura_esperada * 0.9:
                break
        except Exception:  # noqa: BLE001
            continue
    try:
        h = popup.rectangle().height()
    except Exception:  # noqa: BLE001
        h = 0
    if h < altura_esperada * 0.9:
        raise RoboIndisponivel(
            f"O popup da Empresa ficou com {h}px de altura, esperado ~{altura_esperada:.0f}px "
            f"para {len(_EMPRESAS_ORDEM)} itens - a lista nao carregou inteira, entao "
            "clicar numa posicao selecionaria a empresa errada."
        )
    time.sleep(0.4)

    if metodo == "setas":
        # Caminho que o usuario faz na mao: a lista abre com a ULTIMA empresa
        # destacada, entao sobe com {UP} ate a linha certa e confirma com {TAB}
        # (TAB, nao ENTER - foi o que ele descreveu).
        subir = (len(_EMPRESAS_ORDEM) - 1) - indice
        campo.type_keys(("{UP " + str(subir) + "}" if subir > 0 else "") + "{TAB}")
    else:
        # CLIQUE na linha certa - mas MOVENDO o mouse ate ela antes, como um humano.
        #
        # Diagnostico de 09/10/2026 (scripts/debug_empresa_sgi.py): com
        # popup.click_input() o clique CHEGA (o popup fecha, visivel=False depois),
        # mas a empresa continua a mesma. Ou seja, a lista confirma a linha que esta
        # DESTACADA, nao a que esta sob o cursor - e o destaque desse TPopupDataList
        # acompanha o movimento do mouse (hover), que o click_input nao gera: ele
        # posiciona e clica no mesmo instante. Por isso a selecao caia sempre no
        # padrao (CASA DE ADUBO), enquanto o mesmo clique feito a mao funcionava (ali
        # o mouse se movia de verdade antes).
        #
        # Entao: move ate a linha (em dois passos, pra garantir WM_MOUSEMOVE dentro do
        # popup), espera o destaque acompanhar, e so entao clica.
        prect = popup.rectangle()
        altura_linha = prect.height() / len(_EMPRESAS_ORDEM)
        x_abs = prect.left + prect.width() // 2
        y_abs = prect.top + int((indice + 0.5) * altura_linha)
        mouse.move(coords=(x_abs, prect.top + int(altura_linha // 2)))
        time.sleep(0.2)
        mouse.move(coords=(x_abs, y_abs))
        time.sleep(0.4)  # deixa o destaque pousar na linha certa
        _clique_seguro(x_abs, y_abs, campo.process_id(), f"a linha '{texto}' da lista de empresas",
                       trazer_frente=lambda: campo.top_level_parent().set_focus())
    time.sleep(0.4)

    # Fecha o popup se ele tiver ficado aberto (senao o Confirmar nao e clicavel).
    # popup e um wrapper ja resolvido: nao tem .exists() - checa pelo is_visible(),
    # protegido, porque a janela pode ter sumido nesse meio tempo.
    try:
        ainda_aberto = popup.is_visible()
    except Exception:  # noqa: BLE001 - sumiu = fechou = selecionou
        ainda_aberto = False
    if ainda_aberto:
        campo.type_keys("{TAB}")
        time.sleep(0.3)

    # Sem verificacao aqui de proposito: nao da pra ler o valor do combo (window_text()
    # sempre ''). Quem confere e conectar_relatorio(), lendo a faixa "Licenciado para
    # ..." depois do login - e ela PARA a execucao se a empresa estiver errada, antes
    # de buscar qualquer dado.


def _logar_sgi():
    """Abre o SGI pelo atalho, loga (SGI_LOGIN + SGI_SENHA_DESKTOP do .env) e devolve
    o Application ja conectado ao processo CERTO. So roda quando o SGI nao estava
    aberto - NUNCA mata/reinicia uma sessao ja logada.

    Devolve o app de proposito: conectar depois por titulo da janela principal pode
    cair num SGI ANTIGO ainda aberto. Foi o que aconteceu em 09/10/2026 - dois SGI
    rodando (um logado em CASA DE ADUBO, outro recem-aberto na tela de login), o robo
    conectou no logado e ficou 120s procurando ali a tela de login, que estava no
    outro processo.

    SGI_SENHA_DESKTOP (nao SGI_SENHA): a senha do app desktop e DIFERENTE da senha do
    portal web (confirmado pelo usuario em 09/10/2026) - SGI_SENHA e usada por
    scripts/sync_sgi.py (robo de vendas via portal web, GitHub Actions) e nao deve ser
    sobrescrita por essa senha diferente."""
    if not SGI_ATALHO.exists():
        raise RoboIndisponivel(
            f"Atalho do SGI nao encontrado em {SGI_ATALHO} (ajuste SGI_ATALHO_PATH no .env)."
        )

    # os.startfile (nao Application.start, que so sabe rodar um .exe direto sem
    # resolver o atalho): abre como um duplo-clique no .lnk, com o diretorio de
    # trabalho certo.
    os.startfile(str(SGI_ATALHO))

    # Espera a tela de login aparecer em QUALQUER processo e conecta ao dono DELA -
    # assim o robo trabalha no SGI que acabou de abrir, nao em outro que ja estivesse
    # rodando.
    espera = float(os.environ.get("SYNC_COMPRAS_TIMEOUT_LOGIN", "120"))
    fim = time.time() + espera
    janela_login = None
    while time.time() < fim and janela_login is None:
        time.sleep(1.0)
        try:
            for w in Desktop(backend="win32").windows():
                if (w.window_text() or "").strip() == TITULO_LOGIN:
                    janela_login = w
                    break
        except Exception:  # noqa: BLE001 - enumerar janelas pode falhar num instante ruim
            continue
    if janela_login is None:
        raise RoboIndisponivel(
            f"Abri o SGI pelo atalho mas a tela de login ('{TITULO_LOGIN}') nao "
            f"apareceu em {espera:.0f}s."
        )

    app = Application(backend="win32").connect(process=janela_login.process_id())
    _preencher_login(app)
    return app


def _preencher_login(app, metodo_empresa: str = "clique") -> None:
    """Preenche e confirma a tela "Senha..." que ja esta aberta. Separado de
    _logar_sgi porque essa tela aparece em dois momentos: quando o robo abre o SGI do
    zero, e quando o SGI ja esta aberto mas deslogado (alguem clicou em Logoff) - nesse
    segundo caso a janela principal existe e enganava o robo, que seguia como se a
    sessao fosse valida e lia a empresa da sessao ANTERIOR na faixa do rodape (a faixa
    nao se atualiza no logoff) - visto em 09/10/2026."""
    usuario = os.environ.get("SGI_LOGIN")
    senha = os.environ.get("SGI_SENHA_DESKTOP")
    if not usuario or not senha:
        raise RoboIndisponivel(
            "SGI precisa de login e SGI_LOGIN/SGI_SENHA_DESKTOP nao estao no .env - "
            "nao da pra logar sozinho. Logue manualmente."
        )
    # A tela de login ("Senha...") e uma janela PROPRIA (dialogo top-level do mesmo
    # processo), NAO campos dentro da janela principal - confirmado ao vivo em
    # 09/10/2026 (main.children(class_name="Edit") nao achava nada, por isso o login
    # nunca rodava e o fluxo seguia pro menu com o dialogo bloqueando tudo).
    # 15s nao bastava: entre abrir a janela principal e mostrar a tela de login, o SGI
    # ainda conecta no banco remoto ("Criando Conexao com Banco de Dados") e isso
    # passou de 15s numa execucao real (09/10/2026). Espera generosa, configuravel.
    espera_login = float(os.environ.get("SYNC_COMPRAS_TIMEOUT_LOGIN", "120"))
    print("  [login] procurando a tela 'Senha...'")
    login = app.window(title=TITULO_LOGIN)
    try:
        login.wait("exists visible", timeout=espera_login)
    except PywinautoTimeoutError:
        raise RoboIndisponivel(
            f"SGI abriu mas a janela de login ('{TITULO_LOGIN}') nao apareceu em "
            f"{espera_login:.0f}s."
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
    print(f"  [login] usuario preenchido; escolhendo empresa por '{metodo_empresa}'")
    _definir_empresa(campo_empresa, EMPRESA_LOGIN, metodo_empresa)

    # O titulo real do botao e "Confir&mar" - o & (acelerador) fica no MEIO da palavra,
    # nao no comeco (confirmado no print_control_identifiers() de 09/10/2026), entao
    # title_re="&?Confirmar" nao batia. Em vez de depender de onde o & esta, compara o
    # titulo sem & nenhum.
    botoes = [c for c in todos if c.class_name() == "TBitBtn"]
    confirmar = [b for b in botoes
                 if (b.window_text() or "").replace("&", "").strip().upper() == "CONFIRMAR"]
    if not confirmar:
        raise RoboIndisponivel(
            "Nao achei o botao Confirmar na tela de login "
            f"(botoes vistos: {[b.window_text() for b in botoes]})."
        )
    print("  [login] confirmando")
    confirmar[0].click()
    time.sleep(2.0)

    # Esta e a verificacao REAL de que a empresa certa foi selecionada: nao da pra ler
    # o valor do combo (window_text() de TDBLookupComboBox e sempre '' - ver
    # _definir_empresa), mas as credenciais so valem pra PORTEIRA, entao se a empresa
    # estivesse errada o SGI responderia "Usuario/Senha Invalido(a) para esta Empresa!"
    # num dialogo de erro (visto ao vivo em 09/10/2026). Sem dialogo = logou = empresa
    # certa.
    # Espera a tela de login SUMIR - e a prova de que o login foi aceito. Sem isso
    # quem chama reencontra a janela ainda no ar e conclui que o SGI segue deslogado,
    # refazendo o login (visto no log de 09/10/2026: "confirmando" seguido de "aberto
    # porem deslogado - logando", e na 2a vez a tela ja nao existia -> 120s de espera).
    for _ in range(40):
        try:
            if not login.exists() or not login.is_visible():
                break
        except Exception:  # noqa: BLE001 - sumiu = logou
            break
        time.sleep(0.5)

    erro = app.window(title_re="Erro.*")
    if erro.exists(timeout=3):
        try:
            detalhe = " ".join(t for t in (c.window_text() for c in erro.children()) if t)
        except Exception:  # noqa: BLE001 - mensagem e so pra ajudar o diagnostico
            detalhe = ""
        raise RoboIndisponivel(
            f"O SGI recusou o login: {detalhe or 'apareceu um dialogo de erro'}. Se for "
            f"'Usuario/Senha Invalido(a) para esta Empresa', ou a selecao da empresa "
            f"({EMPRESA_LOGIN}) nao pegou, ou SGI_SENHA_DESKTOP esta errada no .env."
        )


def _empresa_ativa(main) -> str:
    """Devolve a empresa em que o SGI esta logado, lendo a faixa do rodape da janela
    principal: "Licenciado para PORTEIRA AGROCOMERCIAL - SGI Versao 70.37" (confirmado
    ao vivo em 09/10/2026).

    E a UNICA forma confiavel de saber a empresa: o combo da tela de login nao expoe
    texto nenhum (TDBLookupComboBox - window_text() sempre '') e o .xls exportado so
    traz o cabecalho das colunas, sem identificacao de empresa. Devolve '' se nao achar
    a faixa (o chamador decide o que fazer)."""
    try:
        filhos = main.descendants()
    except Exception:  # noqa: BLE001 - janela pode estar trocando de estado
        return ""
    for ctrl in filhos:
        try:
            texto = ctrl.window_text() or ""
        except Exception:  # noqa: BLE001 - nem todo descendente da pra ler
            continue
        if "Licenciado para" in texto:
            depois = texto.split("Licenciado para", 1)[1]
            return depois.split(" - SGI")[0].strip()
    return ""


# Jeitos conhecidos de escolher a empresa no combo, em ordem de preferencia. O robo
# tenta, confere pela faixa "Licenciado para ..." e, se errou, refaz o login com o
# proximo - ver conectar_relatorio.
_METODOS_EMPRESA = ["clique", "setas"]


def _esperar_sgi_pronto(main) -> None:
    """Espera o SGI terminar o carregamento pos-login (splash "Conectado! Carregando
    inventarios/clientes...", que deixa o menu desabilitado - sem isso menu_select da
    ElementNotEnabled). Pode passar de 1 minuto, porque os dados vem do servidor
    remoto. A janela principal pode nem EXISTIR ainda nesse meio tempo: is_enabled()
    estourando ElementNotFoundError significa "ainda nao esta pronta", nao erro."""
    def _pronto() -> bool:
        try:
            return bool(main.is_enabled())
        except (ElementNotFoundError, PywinautoTimeoutError):
            return False

    limite = float(os.environ.get("SYNC_COMPRAS_TIMEOUT_CARGA", "300"))
    fim = time.time() + limite
    ultimo_aviso = time.time()
    if not _pronto():
        print("  [SGI] carregando dados pos-login...")
    while not _pronto():
        if time.time() > fim:
            raise RoboIndisponivel(
                f"SGI nao ficou pronto (menu habilitado) em {limite:.0f}s depois do "
                "login - pode estar travado carregando inventarios/clientes (ou o "
                "servidor remoto esta lento/fora)."
            )
        if time.time() - ultimo_aviso > 15:
            print(f"  aguardando SGI terminar de carregar... ({int(time.time() - (fim - limite))}s)")
            ultimo_aviso = time.time()
        time.sleep(1.0)


def _fazer_logoff(main) -> bool:
    """Clica no botao Logoff da barra de ferramentas (volta pra tela de login na hora,
    confirmado ao vivo em 09/10/2026). Devolve False se nao achar o botao."""
    try:
        filhos = main.descendants()
    except Exception:  # noqa: BLE001
        return False
    for ctrl in filhos:
        try:
            if (ctrl.window_text() or "").strip().replace("&", "").upper() == "LOGOFF":
                print(f"  [SGI] clicando em Logoff ({ctrl.class_name()})")
                ctrl.click_input()
                time.sleep(1.5)
                return True
        except Exception:  # noqa: BLE001 - nem todo descendente responde
            continue
    return False


def conectar_relatorio(titulo: str, classe: str | None, caminho_menu: str):
    """Garante SGI aberto+logado com a janela do relatorio disponivel, e devolve (app, rep).
    `app` fica pra achar outras janelas de nivel superior depois (ex.: o dialogo
    "Exportar Dados") - um wrapper ja resolvido (`rep.top_level_parent()`) nao serve
    pra isso, so uma WindowSpecification (app.window(...)) tem .child_window().

    `titulo`/`classe` identificam a janela do relatorio e `caminho_menu` e o caminho
    pra abri-la (ex.: "Relatórios->Compras->Relação de Custo de Compras"). `classe`
    pode ser None quando so o titulo ja basta pra achar a janela.

    Ordem: (1) SGI ja aberto + relatorio ja aberto -> so reaproveita (caminho mais
    robusto, sem digitar senha nem navegar menu); (2) SGI aberto mas relatorio fechado
    -> abre pelo menu; (3) SGI fechado -> abre e loga sozinho (ver _logar_sgi) e cai
    no caso (2)."""
    ja_logou = False
    try:
        app = _conectar_app()
        print("  [SGI] ja estava aberto - reaproveitando")
    except (ElementNotFoundError, PywinautoTimeoutError):
        # Usa o app que _logar_sgi devolve (conectado ao processo que ele mesmo abriu),
        # em vez de reconectar por titulo - reconectar podia cair num SGI antigo.
        print("  [SGI] fechado - abrindo pelo atalho")
        app = _logar_sgi()
        ja_logou = True

    # SGI aberto mas DESLOGADO (alguem clicou em Logoff, ou abriu e nao entrou): a
    # janela principal existe e a faixa do rodape ainda mostra a empresa da sessao
    # anterior, entao sem isto o robo seguia como se a sessao fosse valida e lia a
    # empresa errada (visto em 09/10/2026 - parou achando que estava em CASA DE ADUBO
    # quando na verdade nao estava logado em nada).
    # `ja_logou` evita refazer o login que _logar_sgi acabou de fazer.
    login = app.window(title=TITULO_LOGIN)
    if not ja_logou and login.exists() and login.is_visible():
        print("  [SGI] aberto porem deslogado (tela de login na frente) - logando")
        _preencher_login(app)

    main = app.window(title_re=TITULO_PRINCIPAL + ".*")
    trazer_para_frente(main)  # o resto (datas, botoes) tambem depende de cliques reais

    # Logo apos o login, o SGI mostra um splash "Conectado! Carregando
    # inventarios/clientes..." que deixa o menu principal desabilitado ate terminar -
    # sem esperar isso, menu_select da ElementNotEnabled (visto ao vivo em 09/10/2026).
    # O carregamento (dados vindo do servidor remoto) pode legitimamente demorar mais
    # de 1 minuto. Checagem barata (is_enabled() so le o estado da janela) - nao
    # atrapalha o caminho em que o SGI ja estava pronto havia tempo. Avisa a cada 15s
    # pra nao parecer travado rodando sem interacao.
    # A janela principal pode nem EXISTIR ainda enquanto o SGI carrega (o processo ja
    # subiu, a janela nao): nesse caso is_enabled() estoura ElementNotFoundError, que
    # significa "ainda nao esta pronta", nao "deu errado" - sem tratar isso, o erro
    # escapava e derrubava a execucao inteira (visto em 09/10/2026).
    _esperar_sgi_pronto(main)

    # TRAVA + AUTOCORRECAO da empresa, ANTES de buscar qualquer dado. Em 09/10/2026 o
    # robo rodou 18 dias inteiros logado em CASA DE ADUBO por engano - o relatorio veio
    # vazio em todos (empresa errada nao tem essas compras); se tivesse vindo com
    # dados, teria gravado compras da loja errada no estoque.
    #
    # Nao da pra conferir a empresa ANTES de confirmar o login (o combo nao expoe
    # texto - ver _definir_empresa), entao a estrategia e: logar, conferir pela faixa
    # "Licenciado para ...", e se tiver caido na empresa errada, fazer Logoff e logar
    # de novo com o outro jeito de escolher a empresa. Em vez de depender de um unico
    # metodo estar certo, tenta e confere.
    # Comeca pelo metodo DIFERENTE do usado no login inicial (que foi o primeiro da
    # lista) - repetir o mesmo que acabou de errar seria so perder um login inteiro.
    for metodo in _METODOS_EMPRESA[1:] + _METODOS_EMPRESA[:1]:
        # Le com tolerancia: logo apos o login a faixa pode ainda estar mostrando a
        # sessao anterior por alguns instantes. Dar o veredito cedo demais faria o robo
        # desfazer um login que estava CERTO (e foi o que pareceu acontecer em
        # 09/10/2026: logou em PORTEIRA e mesmo assim refez tudo).
        ativa = ""
        for _ in range(6):
            ativa = _empresa_ativa(main)
            if ativa and ativa.strip().upper() == EMPRESA_LOGIN.strip().upper():
                break
            time.sleep(1.0)
        if not ativa:
            print("  AVISO: nao achei a faixa 'Licenciado para ...' pra conferir a "
                  "empresa logada - seguindo, mas sem essa garantia.")
            break
        if ativa.strip().upper() == EMPRESA_LOGIN.strip().upper():
            print(f"  [SGI] empresa confirmada: {ativa}")
            break
        print(f"  logou em {ativa!r} (esperado {EMPRESA_LOGIN!r}) - refazendo o login "
              f"escolhendo a empresa por '{metodo}'...")
        if _fazer_logoff(main):
            _preencher_login(app, metodo)
        else:
            # Os botoes da barra do SGI nem sempre expoem texto, entao o Logoff pode
            # nao ser encontrado - em 09/10/2026 isso deixava o robo num beco sem
            # saida. Fechar e reabrir chega no mesmo lugar (tela de login) sem
            # depender de achar botao nenhum.
            print("  [SGI] nao achei o botao Logoff - fechando e reabrindo o SGI")
            try:
                app.kill(soft=False)
            except Exception as e:  # noqa: BLE001
                raise RoboIndisponivel(
                    f"O SGI esta logado em {ativa!r} e nao consegui nem deslogar nem "
                    f"fechar pra corrigir ({e}). Feche o SGI na mao e rode de novo."
                ) from e
            time.sleep(3.0)
            app = _logar_sgi()
            main = app.window(title_re=TITULO_PRINCIPAL + ".*")
        _esperar_sgi_pronto(main)
    else:
        ativa = _empresa_ativa(main)
        if ativa and ativa.strip().upper() != EMPRESA_LOGIN.strip().upper():
            raise RoboIndisponivel(
                f"Mesmo tentando todos os jeitos de escolher a empresa "
                f"({', '.join(_METODOS_EMPRESA)}), o SGI segue logado em {ativa!r} e nao "
                f"em {EMPRESA_LOGIN!r} - parei antes de buscar qualquer dado."
            )

    criterio = {"title": titulo} if classe is None else {"title": titulo, "class_name": classe}
    rep = main.child_window(**criterio)
    if not rep.exists():
        print(f"  [SGI] abrindo o relatorio pelo menu: {caminho_menu.replace('->', ' > ')}")
        main.menu_select(caminho_menu)
        rep = main.child_window(**criterio)
        try:
            rep.wait("exists", timeout=15)
        except PywinautoTimeoutError:
            raise RoboIndisponivel(
                f"Cliquei no menu mas a janela '{titulo}' nao apareceu em 15s "
                "(o caminho do menu pode ter mudado)."
            )
    rep.restore()  # garante que da pra interagir mesmo se estava minimizada
    return app, rep


_DTM_SETSYSTEMTIME = 0x1002  # DTM_FIRST + 2
_GDT_VALID = 1


class _SYSTEMTIME(ctypes.Structure):
    _fields_ = [
        ("wYear", ctypes.c_ushort), ("wMonth", ctypes.c_ushort),
        ("wDayOfWeek", ctypes.c_ushort), ("wDay", ctypes.c_ushort),
        ("wHour", ctypes.c_ushort), ("wMinute", ctypes.c_ushort),
        ("wSecond", ctypes.c_ushort), ("wMilliseconds", ctypes.c_ushort),
    ]


def campos_data(rep):
    dtps = rep.children(class_name="TDateTimePicker")
    if len(dtps) != 2:
        raise RoboIndisponivel(f"Esperava 2 campos de data na tela, achei {len(dtps)} "
                                "(o layout do relatorio pode ter mudado).")
    dtps.sort(key=lambda w: w.rectangle().left)
    return dtps[0], dtps[1]  # esquerda = Data Inicial, direita = Data Final


def _definir_data_nativo(campo, dia: date) -> bool:
    """Define a data pela API nativa do controle (DTM_SETSYSTEMTIME), sem teclado
    nenhum. E o jeito determinstico: navegar entre os segmentos por teclado se mostrou
    INDETERMINADO nesse TDateTimePicker - nem {HOME} nem {LEFT} levam de forma
    confiavel ao primeiro segmento (ambos deixam o cursor em lugar imprevisivel, e o
    {RIGHT} cicla), o que fazia os digitos entrarem em segmentos trocados de um jeito
    que variava a cada dia (09/10/2026 -> 26/09/2010 com {HOME}; 22/09/2026 ->
    09/02/2022 com {LEFT}).

    A mensagem leva um ponteiro pra uma struct SYSTEMTIME, e ponteiro do nosso
    processo nao vale no processo do SGI - por isso a struct e escrita na memoria do
    PROPRIO processo alvo (RemoteMemoryBlock do pywinauto) antes de enviar.

    Devolve True se a data pegou (confere lendo de volta), False se nao deu - quem
    chama cai pro caminho por clique+digitacao."""
    try:
        from pywinauto.remote_memory_block import RemoteMemoryBlock

        st = _SYSTEMTIME()
        st.wYear = dia.year
        st.wMonth = dia.month
        st.wDay = dia.day
        st.wDayOfWeek = (dia.weekday() + 1) % 7  # SYSTEMTIME: 0=domingo
        rmb = RemoteMemoryBlock(campo)
        rmb.Write(st)
        campo.send_message(_DTM_SETSYSTEMTIME, _GDT_VALID, rmb.Address())
        del rmb
    except Exception:  # noqa: BLE001 - qualquer falha aqui so significa "usa o plano B"
        return False
    time.sleep(0.2)
    return dia.strftime("%d/%m/%Y") in (campo.window_text() or "")


def definir_data(campo, dia: date, tentativas: int = 3) -> None:
    """Coloca `dia` no campo de data, conferindo lendo de volta (window_text() desse
    controle funciona, diferente do combo de empresa).

    Caminho principal: API nativa (ver _definir_data_nativo). Plano B: posicionar o
    cursor por CLIQUE no segmento do dia - coordenada absoluta dentro do campo, que
    nao depende de onde o cursor estava (o que afundou as versoes por teclado) - e
    digitar dia/mes/ano avancando com {RIGHT}."""
    esperado = dia.strftime("%d/%m/%Y")
    if _definir_data_nativo(campo, dia):
        return

    rect = campo.rectangle()
    meio_y = rect.height() // 2
    texto = campo.window_text()
    for tentativa in range(1, tentativas + 1):
        campo.set_focus()
        # Clica no inicio do campo = segmento do DIA (formato dd/mm/aaaa). Posicao
        # absoluta: 8px da borda esquerda na 1a/3a tentativa, 12px na 2a (caso a
        # margem do controle engula o primeiro clique).
        campo.click_input(coords=(12 if tentativa == 2 else 8, meio_y))
        time.sleep(0.1)
        campo.type_keys(f"{dia:%d}")
        campo.type_keys("{RIGHT}")
        campo.type_keys(f"{dia:%m}")
        campo.type_keys("{RIGHT}")
        campo.type_keys(f"{dia:%Y}")
        time.sleep(0.3)
        texto = campo.window_text()
        if esperado in texto:
            return
    raise RoboIndisponivel(
        f"Campo de data ficou {texto!r} depois de {tentativas} tentativa(s) "
        f"(e da API nativa), esperado {esperado}."
    )


_WM_NULL = 0x0000
_SMTO_ABORTIFHUNG = 0x0002


def esperar_janela_responder(janela, timeout: float = 90.0) -> bool:
    """Espera a thread de interface do SGI voltar a processar mensagens.

    POR QUE: a consulta do relatorio e SINCRONA - o SGI trava a propria interface
    enquanto fala com o banco remoto (138.255.35.101). Um `click()` do pywinauto e uma
    mensagem POSTADA: ela fica na fila e so e processada quando a interface volta. Ou
    seja, clicar "Gerar Arq." com a consulta rodando nao da erro - o clique simplesmente
    acontece depois, e quem esta esperando o dialogo de salvar estoura o timeout antes.
    Em Compras isso nao aparecia porque cada consulta era de UM dia; em Transferencias a
    consulta e do periodo inteiro (18 dias, 120 linhas) e demora bem mais.

    SendMessageTimeout(WM_NULL) com SMTO_ABORTIFHUNG devolve 0 enquanto a thread estiver
    ocupada e != 0 quando ela volta a bombear mensagens - e o jeito de saber "a tela
    terminou", sem depender de sleep chutado."""
    try:
        hwnd = janela.handle
    except Exception:  # noqa: BLE001
        try:
            hwnd = janela.wrapper_object().handle
        except Exception:  # noqa: BLE001
            return True
    resultado = ctypes.c_ulong()
    fim = time.time() + timeout
    while time.time() < fim:
        ok = ctypes.windll.user32.SendMessageTimeoutW(
            hwnd, _WM_NULL, 0, 0, _SMTO_ABORTIFHUNG, 200, ctypes.byref(resultado))
        if ok:
            return True
        time.sleep(0.3)
    return False


def _janelas_de_topo(app) -> dict:
    """{handle: (classe, titulo)} das janelas de topo visiveis do processo do SGI."""
    achadas = {}
    try:
        for w in app.windows(visible_only=True):
            try:
                achadas[w.handle] = (w.class_name() or "", w.window_text() or "")
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return achadas


def _campo_nome_arquivo(janela):
    """O Edit do nome do arquivo no dialogo de salvar, ou None se a janela nao for um.

    Identificar o dialogo PELA ESTRUTURA, e nao pelo titulo, e deliberado: o titulo
    muda de tela pra tela ("Exportar Dados" em Compras) e de versao pra versao do
    Windows, e foi exatamente isso que derrubou a primeira versao do robo de
    Transferencias. Dialogo comum de salvar = tem um campo de texto editavel;
    caixa de mensagem (TMessageForm / MessageBox) = nao tem nenhum."""
    try:
        for c in janela.descendants():
            try:
                if (c.class_name() or "").lower() != "edit":
                    continue
                if c.is_visible() and c.is_enabled():
                    return c
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


_BOTOES_AFIRMATIVOS = {"ok", "sim", "yes", "confirmar", "continuar", "fechar"}


def _descrever_e_fechar_dialogo(janela) -> str:
    """Le o texto de uma caixa de mensagem do SGI, fecha e devolve o que ela dizia.

    Serve pros dois casos que aparecem no caminho do "Gerar Arq.": um aviso ANTES
    ("nenhum registro", "confirma a geracao?") e um aviso DEPOIS ("arquivo gerado").
    Qualquer um deles e MODAL: se ficar aberto, o proximo passo do robo (ou a proxima
    execucao) encontra a tela travada. Entao fecha sempre - pelo botao afirmativo se
    houver, senao ESC - e devolve o texto, que e o que me diz o que aconteceu."""
    textos, botao_ok = [], None
    try:
        for c in janela.descendants():
            try:
                classe = (c.class_name() or "").lower()
                txt = (c.window_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not txt:
                continue
            if "static" in classe or "label" in classe:
                textos.append(txt)
            elif "button" in classe or "bitbtn" in classe:
                if txt.replace("&", "").strip().lower() in _BOTOES_AFIRMATIVOS and botao_ok is None:
                    botao_ok = c
    except Exception:  # noqa: BLE001
        pass
    try:
        if botao_ok is not None:
            botao_ok.click()
        else:
            janela.type_keys("{ESC}")
    except Exception:  # noqa: BLE001
        pass
    return " | ".join(textos) or "(sem texto legivel)"


def _achar_dialogo_salvar(app, ignorar: dict | None = None):
    """Procura um dialogo de salvar JA aberto (sobra de uma execucao que morreu no
    meio). Reaproveitar e melhor que clicar "Gerar Arq." de novo: com o modal aberto a
    tela do relatorio esta bloqueada e um segundo clique nao chega nela."""
    for h, (classe, _titulo) in _janelas_de_topo(app).items():
        if ignorar and h in ignorar:
            continue
        try:
            w = app.window(handle=h).wrapper_object()
        except Exception:  # noqa: BLE001
            continue
        if _campo_nome_arquivo(w) is not None:
            return w
    return None


def exportar_arquivo(app, rep, destino: Path, timeout_dialogo: float = 60.0) -> Path:
    """Clica em "Gerar Arq.", salva em `destino` e espera o arquivo aparecer.

    Serve para qualquer relatorio do SGI (so muda o formato que cada tela gera - .xls em
    Compras, .csv em Transferencias). Devolve o caminho realmente usado, que pode
    diferir de `destino` se o arquivo anterior estiver travado.

    O dialogo e achado por ESTRUTURA (janela nova do processo que tem campo de texto) e
    nao por titulo - ver _campo_nome_arquivo. Caixas de mensagem que aparecerem no meio
    do caminho sao lidas e fechadas, e o texto delas entra no erro final: se nao houver
    arquivo no fim, o motivo vem escrito em vez de "timed out"."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists():
        try:
            destino.unlink()  # evita o popup de "sobrescrever?" no Explorer
        except OSError:
            # Arquivo travado por outro processo (Excel aberto nele, antivirus,
            # indexador do Windows) - em 09/10/2026 isso derrubou o dia 22/09 inteiro
            # com "Permission denied". Exportar com outro nome resolve; o arquivo
            # velho fica pra tras, sem atrapalhar (o que importa e o recem-gerado).
            destino = destino.with_name(f"{destino.stem}_{int(time.time())}{destino.suffix}")

    antes = _janelas_de_topo(app)
    avisos: list[str] = []

    dlg = _achar_dialogo_salvar(app)
    if dlg is not None:
        print("  [export] um dialogo de salvar ja estava aberto - reaproveitando")
    else:
        # A tela precisa estar respondendo, senao o clique entra na fila e so acontece
        # depois (ver esperar_janela_responder).
        esperar_janela_responder(rep)
        rep.child_window(title="&Gerar Arq.", class_name="TBitBtn").click()

        fim = time.time() + timeout_dialogo
        while time.time() < fim:
            for h, (classe, titulo) in _janelas_de_topo(app).items():
                if h in antes:
                    continue
                antes[h] = (classe, titulo)  # ja vista, nao reavaliar
                try:
                    w = app.window(handle=h).wrapper_object()
                except Exception:  # noqa: BLE001
                    continue
                if _campo_nome_arquivo(w) is not None:
                    dlg = w
                    break
                texto = _descrever_e_fechar_dialogo(w)
                avisos.append(f"{titulo or classe}: {texto}")
                print(f"  [export] o SGI avisou: {titulo or classe}: {texto}")
            if dlg is not None:
                break
            time.sleep(0.3)

    if dlg is None:
        detalhe = ("; ".join(avisos) if avisos
                   else "nenhuma janela nova apareceu (janelas do SGI agora: "
                        + ", ".join(f"{c}/{t!r}" for c, t in _janelas_de_topo(app).values()) + ")")
        raise RoboIndisponivel(
            f"Cliquei em 'Gerar Arq.' e o dialogo de salvar nao apareceu em "
            f"{timeout_dialogo:.0f}s. {detalhe}")

    campo_nome = _campo_nome_arquivo(dlg)
    campo_nome.set_edit_text(str(destino))
    # Enter no campo confirma o diálogo (Salvar/OK), sem depender do texto exato do
    # botão - diálogo comum do Windows, cuja legenda varia ("Salvar" sem o "&" não
    # bateu; em vez de caçar a grafia certa, usa Enter).
    campo_nome.type_keys("{ENTER}")

    for _ in range(int(timeout_dialogo * 2)):
        try:
            if destino.exists() and destino.stat().st_size > 0:
                # Alguns relatorios avisam "arquivo gerado" DEPOIS de salvar, e esse
                # aviso e modal: fecha antes de devolver, senao o passo seguinte
                # (outro dia, outro relatorio) bate numa tela bloqueada.
                sobra = [h for h in _janelas_de_topo(app) if h not in antes]
                for h in sobra:
                    try:
                        w = app.window(handle=h).wrapper_object()
                    except Exception:  # noqa: BLE001
                        continue
                    if _campo_nome_arquivo(w) is None:
                        print(f"  [export] o SGI avisou: {_descrever_e_fechar_dialogo(w)}")
                return destino
        except OSError:
            # Corrida com o proprio Explorer criando/regravando o arquivo: exists()
            # pode dar True e o stat() seguinte falhar com "arquivo nao encontrado".
            # Isso nao e falha do dia - e so esperar o proximo ciclo.
            pass
        time.sleep(0.5)
    raise RoboIndisponivel(f"Arquivo {destino} nao apareceu apos clicar em Gerar Arq.")


