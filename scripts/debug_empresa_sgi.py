"""Diagnostico TEMPORARIO da selecao de empresa na tela de login do SGI.

NAO loga, NAO digita senha - so abre o dropdown de Empresa e relata, passo a passo, o
que aparece na tela: quantas janelas o dropdown cria, de que classe, posicao, tamanho
e se estao visiveis; quanto tempo a lista demora pra renderizar; e o que muda depois
do clique na linha.

Objetivo: descobrir por que a selecao nao pega no robo (suspeita: o robo clica numa
janela-sombra do Windows em vez da lista real, ou a lista ainda nao renderizou).

Uso: deixe o SGI ABERTO NA TELA DE LOGIN ("Senha...") e rode:
    py -3.11-32 scripts\\debug_empresa_sgi.py

Cole a saida inteira de volta.
"""
from __future__ import annotations

import ctypes
import time

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:  # noqa: BLE001
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass

from pywinauto import Application, Desktop, mouse  # noqa: E402

TITULO_LOGIN = "Senha..."
ALVO = "PORTEIRA AGROCOMERCIAL"
ORDEM = [
    "ALAOR SILVA RIBEIRO",
    "CASA DE ADUBOS(ANTIGA)",
    "PORTEIRA AGROCOMERCIAL",
    "PORTEIRA PIATA",
    "CASA DE ADUBOS CAFE BOM",
]


def descreve(w) -> str:
    try:
        r = w.rectangle()
        return (f"classe={w.class_name()!r} texto={w.window_text()!r} "
                f"rect=({r.left},{r.top},{r.right},{r.bottom}) {r.width()}x{r.height()} "
                f"visivel={w.is_visible()} enabled={w.is_enabled()} hwnd={w.handle}")
    except Exception as e:  # noqa: BLE001
        return f"<nao deu pra ler: {e}>"


def janelas() -> dict:
    out = {}
    try:
        for w in Desktop(backend="win32").windows():
            out[w.handle] = w
    except Exception as e:  # noqa: BLE001
        print(f"  (erro enumerando janelas: {e})")
    return out


def main() -> int:
    # Conecta no processo DONO da tela de login (pode haver mais de um SGI aberto).
    dono = None
    for w in Desktop(backend="win32").windows():
        try:
            if (w.window_text() or "").strip() == TITULO_LOGIN:
                dono = w
                break
        except Exception:  # noqa: BLE001
            continue
    if dono is None:
        print(f"ERRO: nao achei a janela '{TITULO_LOGIN}'. Deixe o SGI aberto NA TELA "
              "DE LOGIN e rode de novo.")
        return 1
    print(f"Tela de login achada: {descreve(dono)}")
    app = Application(backend="win32").connect(process=dono.process_id())
    login = app.window(title=TITULO_LOGIN)

    print("\n== CONTROLES DA TELA DE LOGIN ==")
    filhos = login.children()
    for c in filhos:
        print(f"  {descreve(c)}")

    combos = [c for c in filhos if "combobox" in c.class_name().lower()]
    combos.sort(key=lambda w: w.rectangle().left)
    if len(combos) < 1:
        print("ERRO: nenhum combo encontrado.")
        return 1
    campo = combos[0]
    print(f"\nCombo Empresa (o mais a esquerda): {descreve(campo)}")

    print("\n== PASSO 1: foco na janela de login ==")
    try:
        login.set_focus()
        print("  set_focus OK")
    except Exception as e:  # noqa: BLE001
        print(f"  set_focus falhou: {e}")
    time.sleep(0.3)

    print("\n== PASSO 2: clique na seta do combo ==")
    antes = janelas()
    print(f"  janelas de topo antes: {len(antes)}")
    rect = campo.rectangle()
    coords = (rect.width() - 10, rect.height() // 2)
    print(f"  clicando em coords relativos {coords} (rect do combo: "
          f"{rect.width()}x{rect.height()} em ({rect.left},{rect.top}))")
    campo.click_input(coords=coords)

    print("\n== PASSO 3: o que aparece, instante a instante ==")
    visto = {}
    for i in range(12):  # 12 x 0.3s = 3.6s
        time.sleep(0.3)
        agora = janelas()
        novas = {h: w for h, w in agora.items() if h not in antes}
        for h, w in novas.items():
            if h not in visto:
                visto[h] = w
                print(f"  [{(i + 1) * 0.3:.1f}s] JANELA NOVA: {descreve(w)}")
            else:
                # Re-descreve se mudou de tamanho/visibilidade (lista renderizando)
                pass
        if i in (3, 7, 11) and visto:
            print(f"  [{(i + 1) * 0.3:.1f}s] estado atual das novas:")
            for w in visto.values():
                print(f"      {descreve(w)}")

    if not visto:
        print("  NENHUMA janela nova apareceu - o clique na seta nao abriu o dropdown.")
        return 1

    print(f"\n  Total de janelas novas: {len(visto)}")
    print("  (se for mais de uma, o robo pode estar clicando na sombra em vez da lista)")

    # Escolhe a candidata a lista: TPopupDataList, ou a maior visivel
    candidatas = [w for w in visto.values()
                  if "popup" in (w.class_name() or "").lower()]
    if candidatas:
        print(f"  candidata por classe (contem 'popup'): {descreve(candidatas[0])}")
        popup = candidatas[0]
    else:
        popup = max(visto.values(), key=lambda w: w.rectangle().height())
        print(f"  nenhuma classe com 'popup' - usando a mais alta: {descreve(popup)}")

    print("\n== PASSO 4: clique na linha do alvo ==")
    indice = ORDEM.index(ALVO)
    prect = popup.rectangle()
    altura_linha = prect.height() / len(ORDEM)
    y = int((indice + 0.5) * altura_linha)
    print(f"  popup {prect.width()}x{prect.height()} em ({prect.left},{prect.top}); "
          f"{len(ORDEM)} itens -> {altura_linha:.1f}px por linha")
    x_abs = prect.left + prect.width() // 2
    y_abs = prect.top + y
    print(f"  indice do alvo = {indice} -> alvo na tela ({x_abs}, {y_abs})")
    print("  MOVENDO o mouse ate a linha antes de clicar (o destaque dessa lista "
          "acompanha o hover; clicar sem mover confirma a linha destacada antiga)")
    mouse.move(coords=(x_abs, prect.top + int(altura_linha // 2)))
    time.sleep(0.2)
    mouse.move(coords=(x_abs, y_abs))
    time.sleep(0.4)
    mouse.click(button="left", coords=(x_abs, y_abs))
    time.sleep(0.6)

    print("\n== PASSO 5: estado depois do clique ==")
    for h, w in visto.items():
        try:
            print(f"  janela {h}: visivel={w.is_visible()}")
        except Exception as e:  # noqa: BLE001
            print(f"  janela {h}: sumiu ({e})")
    print(f"  combo agora: {descreve(campo)}")
    print("  (window_text do combo e sempre '' - OLHE A TELA pra ver se mudou pra "
          f"{ALVO})")

    print("\nFim. Diga o que a TELA mostra no campo Empresa agora.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
