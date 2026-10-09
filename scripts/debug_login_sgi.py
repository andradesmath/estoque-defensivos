"""Diagnostico TEMPORARIO (nao faz parte do robo) - imprime a arvore completa de
controles da tela de login do SGI ("Senha..."), pra descobrir a classe real do combo
Empresa (CB_GETCOUNT voltou vazio - provavelmente nao e um ComboBox Win32 padrao, e sim
um controle customizado/skinado).

Uso: deixe o SGI parado na tela de login (abra manualmente clicando no atalho, ou deixe
o robo chegar ate la) e rode:
    py -3.11-32 scripts\\debug_login_sgi.py

Cola a saida inteira de volta.
"""
from pywinauto import Application

app = Application(backend="win32").connect(title="Senha...", timeout=15)
dlg = app.window(title="Senha...")
dlg.print_control_identifiers()
