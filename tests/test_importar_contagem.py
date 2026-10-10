"""Desempate de APRESENTAÇÃO na importação de contagem física.

Regra confirmada pelo usuário em 10/10/2026: nome sem especificação é o de litro. O
desempate mora no importador e não em match_produto de propósito - na entrada por nota
fiscal o conservadorismo é o certo, porque quem digita está na frente da tela.
"""
import importlib.util
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "importar_contagem_unidade", RAIZ / "scripts" / "importar_contagem_unidade.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

CAD = {
    "01714": "ENGEO PLENO 1 LT", "01715": "ENGEO PLENO 250 ML",
    "06908": "VERDADERO 600 WG 1 KG", "09416": "VERDADERO 600 WG 5KG",
    "09606": "DISPARO ULTRA S  1L", "09607": "DISPARO ULTRA S  5L",
    "09098": "VERDICT MAX 1 LT", "07524": "VERDICT MAX 5 LT",
    "02991": "TORDON  10 LT", "02976": "TORDON  20 LT", "02867": "TORDON LT",
}


def _d(desc):
    return mod._desempatar_por_apresentacao(desc, CAD, 0.80)


def test_prefere_litro_e_nao_o_mililitro():
    """"ENGEO PLENO" é o de 1 litro, não o de 250 ml - mesmo o de 250 ml sendo o menor."""
    assert _d("ENGEO PLENO") == "01714"


def test_prefere_a_embalagem_unitaria():
    assert _d("VERDADERO") == "06908"     # 1 KG, não 5 KG
    assert _d("DISPARO") == "09606"       # 1 L, não 5 L


def test_tolera_erro_pequeno_de_grafia():
    """"VERDICT MAXX" na planilha, "VERDICT MAX" no cadastro: um X a mais."""
    assert _d("VERDICT MAXX") == "09098"


def test_nao_casa_produto_diferente_com_nome_parecido():
    """TORDON ULTRA-S não é TORDON. A primeira versão da regra casou os dois e teria
    posto 2 unidades no produto errado - este teste existe por causa disso."""
    assert _d("TORDON ULTRA-S") is None


def test_nao_age_quando_a_planilha_diz_o_tamanho():
    """Com tamanho na descrição, não há ambiguidade de embalagem a resolver: se não
    casou, o motivo é outro e inventar aqui seria esconder o problema."""
    assert _d("VERDADERO 5 KG") is None
