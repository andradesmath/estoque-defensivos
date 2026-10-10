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

# Acabamento visual leve sobre o tema (.streamlit/config.toml): cards com sombra sutil,
# métricas destacadas e uma folha de impressão (esconde menu/botões ao "Imprimir" do navegador).
st.markdown("""
<style>
[data-testid="stMetric"] {
    background: #FFFFFF;
    border: 1px solid #E3E8EC;
    border-radius: 10px;
    padding: 14px 16px 10px;
    box-shadow: 0 1px 3px rgba(16, 42, 62, 0.08);
}
[data-testid="stMetricValue"] { color: #1B4B66; }
h1, h2, h3 { color: #16374B; }
[data-testid="stSidebar"] h1 { color: #F2F5F7 !important; font-size: 1.3rem; }
[data-testid="stDataFrame"], [data-testid="stExpander"] {
    border-radius: 8px;
    overflow: hidden;
}
@media print {
    [data-testid="stSidebar"], [data-testid="stHeader"], [data-testid="stToolbar"],
    button, [data-testid="stDownloadButton"] { display: none !important; }
    [data-testid="stAppViewBlockContainer"] { max-width: 100% !important; }
}
</style>
""", unsafe_allow_html=True)

from estoque import db  # noqa: E402
from estoque.compat import LARG  # noqa: E402
from estoque import paginas  # noqa: E402
from estoque import paginas  # noqa: E402
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


def _avisar_unidade_sem_contagem(unidade: str | None) -> None:
    """Unidade sem contagem física mostra só o FLUXO (o que entrou menos o que saiu),
    não o estoque. O número é real, mas não é o saldo - e pode ficar negativo. Avisar
    é obrigatório: sem isso alguém compra em cima de um número que não significa o que
    parece."""
    if not unidade or unidade == paginas.CONSOLIDADO:
        return
    try:
        if db.unidade_tem_contagem(unidade):
            return
    except Exception:  # noqa: BLE001 - banco antigo, antes das unidades
        return
    st.warning(f"**{unidade}** ainda não tem contagem física. O que aparece é só o "
               "movimento desde que o controle começou — não o estoque real.", icon="⚠️")


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
        # Seletor de UNIDADE antes do de tela, e global: o saldo é o mesmo assunto em
        # todas as telas, e ver uma em Piatã e outra na matriz seria a receita para ler
        # o número errado. "Consolidado" soma as unidades - é o que o painel sempre fez
        # quando só havia um estoque.
        unidades = paginas.unidades_disponiveis()
        padrao = unidades.index("Barra da Estiva") if "Barra da Estiva" in unidades else 0
        st.radio("Unidade", unidades, index=padrao, key="unidade_estoque")
        _avisar_unidade_sem_contagem(st.session_state.get("unidade_estoque"))
        st.divider()
        pagina = st.radio("Tela", list(PAGINAS), label_visibility="collapsed", key="pagina")
        st.divider()
        if st.button("Atualizar dados", **LARG):
            st.cache_data.clear()
            st.rerun()
    PAGINAS[pagina]()


main()
