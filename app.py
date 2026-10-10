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
# Acabamento do CONTEÚDO (a barra lateral tem o seu em estoque/navegacao.py).
# Métricas são a primeira coisa lida na visão geral: ganham borda fina e um filete de
# acento no topo, que é o mesmo vocabulário da faixa do menu. Sem sombra - sombra em
# tudo é o que faz um painel parecer um kit de cards genérico.
st.markdown("""
<style>
[data-testid="stMetric"] {
    background: #FFFFFF;
    border: 1px solid #E6E3DB;
    border-top: 2px solid #C9A227;
    border-radius: 6px;
    padding: 14px 16px 10px;
}
[data-testid="stMetricValue"] { color: #1C211F; font-weight: 600; letter-spacing: -.02em; }
[data-testid="stMetricLabel"] { color: #6B7570; }
h1, h2, h3 { color: #1C211F; letter-spacing: -.01em; }
h1 { font-weight: 600; }
[data-testid="stDataFrame"], [data-testid="stExpander"] { border-radius: 6px; overflow: hidden; }
@media print {
    [data-testid="stSidebar"], [data-testid="stHeader"], [data-testid="stToolbar"],
    button, [data-testid="stDownloadButton"] { display: none !important; }
    [data-testid="stAppViewBlockContainer"] { max-width: 100% !important; }
}
</style>
""", unsafe_allow_html=True)

from estoque import db  # noqa: E402
from estoque.compat import LARG  # noqa: E402
from estoque import navegacao, paginas  # noqa: E402
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


def _aviso_unidade_sem_contagem(unidade: str | None) -> str | None:
    """Texto do aviso quando a unidade não tem contagem física, ou None.

    Unidade sem contagem mostra só o FLUXO (o que entrou menos o que saiu), não o
    estoque: o número é real, mas não é o saldo, e pode ficar negativo. Avisar é
    obrigatório - sem isso alguém compra em cima de um número que não significa o que
    parece. Devolve o texto em vez de desenhar, para a barra lateral decidir onde ele
    entra na hierarquia."""
    if not unidade or unidade == paginas.CONSOLIDADO:
        return None
    try:
        if db.unidade_tem_contagem(unidade):
            return None
    except Exception:  # noqa: BLE001 - banco antigo, antes das unidades
        return None
    return (f"**{unidade}** ainda não tem contagem física. O que aparece é só o "
            "movimento desde que o controle começou — não o estoque real.")


def main() -> None:
    if not _autenticar():
        st.stop()
    try:
        _preparar_banco()
    except Exception as e:  # noqa: BLE001
        st.error("Não consegui conectar ao banco de dados. Confira o secret DATABASE_URL.")
        st.caption(f"Detalhe técnico: {type(e).__name__}")
        st.stop()

    unidades = paginas.unidades_disponiveis()
    pagina = navegacao.render(
        telas=list(PAGINAS),
        unidades=unidades,
        aviso_unidade=_aviso_unidade_sem_contagem(st.session_state.get("unidade_estoque")),
        erro_unidades=st.session_state.get("erro_unidades"),
    )
    PAGINAS[pagina]()


main()
