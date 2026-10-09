"""Diagnostico da tela "Relação de Transferências" do SGI desktop.

Imprime a arvore de controles da janela (classe, texto, posicao) pra eu saber
exatamente o que automatizar: o combo "Transferido para", os dois campos de data, o
checkbox Grupo e os botoes Buscar / Gerar Arq.

So LE - nao clica em nada, nao gera arquivo.

Uso: deixe a janela "Relação de Transferências" ABERTA no SGI e rode:
    py -3.11-32 scripts\\debug_transferencias_sgi.py
"""
from __future__ import annotations

import ctypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:  # noqa: BLE001
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass

from pywinauto import Application, Desktop  # noqa: E402

TITULO_PRINCIPAL = "SGI - Sistema de Ger"
TITULO_JANELA = "Relação de Transferências"


def descreve(w, nivel: int = 0) -> str:
    pad = "  " * nivel
    try:
        r = w.rectangle()
        return (f"{pad}classe={w.class_name()!r} texto={w.window_text()!r} "
                f"rect=({r.left},{r.top},{r.right},{r.bottom}) {r.width()}x{r.height()}")
    except Exception as e:  # noqa: BLE001
        return f"{pad}<nao deu pra ler: {e}>"


def main() -> int:
    dono = None
    for w in Desktop(backend="win32").windows():
        try:
            if (w.window_text() or "").startswith(TITULO_PRINCIPAL):
                dono = w
                break
        except Exception:  # noqa: BLE001
            continue
    if dono is None:
        print(f"ERRO: nao achei a janela principal do SGI ('{TITULO_PRINCIPAL}...').")
        return 1

    app = Application(backend="win32").connect(process=dono.process_id())
    main_w = app.window(title_re=TITULO_PRINCIPAL + ".*")

    rel = main_w.child_window(title=TITULO_JANELA)
    if not rel.exists():
        print(f"ERRO: a janela '{TITULO_JANELA}' nao esta aberta. Abra em")
        print("      Relatorios > Relatorio de Transferencias e rode de novo.")
        print("\nJanelas filhas vistas na principal:")
        for c in main_w.children():
            print("  " + descreve(c))
        return 1

    print(f"Janela achada: {descreve(rel)}")
    print(f"  class_name da janela: {rel.class_name()!r}   <- usar como CLASSE_RELATORIO")

    print("\n=== CONTROLES (1o nivel) ===")
    for c in rel.children():
        print(descreve(c))

    print("\n=== TODOS OS DESCENDENTES ===")
    for c in rel.descendants():
        print(descreve(c, 1))

    print("\n=== RESUMO DO QUE INTERESSA ===")
    for alvo, rotulo in [("combobox", "combos (Transferido para / Grupo)"),
                         ("datetimepicker", "campos de data"),
                         ("bitbtn", "botoes"),
                         ("checkbox", "checkbox Grupo"),
                         ("grid", "grade de resultados"),
                         ("dbgrid", "grade de resultados")]:
        achados = [c for c in rel.descendants() if alvo in (c.class_name() or "").lower()]
        if achados:
            print(f"\n-- {rotulo} ({alvo}):")
            for c in achados:
                print(descreve(c, 1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
