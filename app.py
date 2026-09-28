"""
app.py - Painel de controle de estoque de DEFENSIVOS (Streamlit).

    streamlit run app.py

Configuração em variável de ambiente, .env ou st.secrets: DATABASE_URL (obrigatória),
GITHUB_TOKEN + GITHUB_REPO (botão "Sincronizar agora") e APP_SENHA (opcional; se definida,
o painel pede a senha — recomendado, pois mostra custos e margens).
"""
import hmac
import os

import streamlit as st

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

st.set_page_config(page_title="Estoque de Defensivos", page_icon="📦", layout="wide")

from estoque import db  # noqa: E402
from estoque.compat import LARG  # noqa: E402
from estoque.paginas import PAGINAS  # noqa: E402


def _senha_configurada() -> str | None:
    s = os.environ.get("APP_SENHA")
    if s:
        return s
    try:
        return st.secrets.get("APP_SENHA")
    except Exception:  # noqa: BLE001
        return None


def _autenticar() -> bool:
    senha = _senha_configurada()
    if not senha:
        return True
    if st.session_state.get("autenticado"):
        return True
    st.title("Estoque de Defensivos")
    tentativa = st.text_input("Senha", type="password")
    if tentativa:
        if hmac.compare_digest(tentativa.encode(), senha.encode()):
            st.session_state["autenticado"] = True
            st.rerun()
        else:
            st.error("Senha incorreta.")
    return False


@st.cache_resource(show_spinner=False)
def _preparar_banco() -> bool:
    db.init_schema()  # idempotente: cria tabelas/views se ainda não existirem
    return True


def main() -> None:
    if not _autenticar():
        st.stop()
    try:
        _preparar_banco()
    except Exception as e:  # noqa: BLE001
        st.error("Não consegui conectar ao banco de dados. Confira o secret DATABASE_URL.")
        st.caption(f"Detalhe técnico: {type(e).__name__}")
        st.stop()

    with st.sidebar:
        st.title("📦 Defensivos")
        pagina = st.radio("Tela", list(PAGINAS), label_visibility="collapsed", key="pagina")
        st.divider()
        if st.button("Atualizar dados", **LARG):
            st.cache_data.clear()
            st.rerun()
    PAGINAS[pagina]()


main()
