"""estoque/compat.py - Diferenças entre versões do Streamlit.

`use_container_width=True` foi depreciado em favor de `width="stretch"` (Streamlit >= 1.50).
Usar o argumento novo em versão antiga quebra; o antigo em versão futura pode ser removido.
`LARG` escolhe o certo pela versão instalada."""
import streamlit as st


def _versao() -> tuple[int, int]:
    try:
        a, b = st.__version__.split(".")[:2]
        return int(a), int(b)
    except Exception:  # noqa: BLE001
        return (0, 0)


LARG = {"width": "stretch"} if _versao() >= (1, 50) else {"use_container_width": True}
