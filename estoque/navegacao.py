"""
estoque/navegacao.py - A barra lateral do painel: marca, seletor de unidade e menu.

POR QUE UM MÓDULO SÓ PRA ISSO: a navegação cresceu pra 15 telas num `st.radio` corrido,
sem agrupamento nem hierarquia - dava pra ler, mas não dava pra ENCONTRAR. Aqui ela vira
quatro seções nomeadas pelo trabalho que a pessoa está fazendo ("Dia a dia", "Análise"),
não pelo tipo técnico da tela.

Decisões visuais, para quem for mexer depois:

  - A BARRA DE ACENTO no item ativo cita a faixa de classificação do rótulo de
    defensivo: é cor que informa estado, não enfeite. Mesma regra no resto - cor só
    aparece onde significa alguma coisa (unidade ativa, tela ativa, aviso). O resto é
    uma escala de cinza-esverdeado.
  - A UNIDADE vem antes do menu e em botões lado a lado, não num radio empilhado:
    é a escolha que muda o SIGNIFICADO de todo número da tela, então precisa estar
    sempre visível e ser trocável num clique.
  - Rótulo de seção em caixa normal, não em CAIXA ALTA: a sidebar já tem pouca largura
    e versalete come legibilidade sem acrescentar hierarquia que o peso e a cor não
    dêem.
  - O estado ativo usa `type="primary"` do próprio Streamlit em vez de classe CSS
    casada por índice: classe de componente muda de versão pra versão, o `type` não.
"""
from __future__ import annotations

import streamlit as st

CONSOLIDADO = "Consolidado"

# (seção, [(nome da tela em PAGINAS, ícone Material)]). O nome TEM que bater com a
# chave em paginas.PAGINAS - o que sobrar cai em "Outros" automaticamente (ver
# _secoes_completas), pra nenhuma tela sumir do menu quando alguém criar uma nova.
SECOES: list[tuple[str, list[tuple[str, str]]]] = [
    ("Dia a dia", [
        ("Visão geral", ":material/dashboard:"),
        ("Saldo por produto", ":material/inventory_2:"),
        ("Movimentar estoque", ":material/swap_horiz:"),
        ("Entrada por nota fiscal", ":material/receipt_long:"),
    ]),
    ("Análise", [
        ("Indicadores", ":material/insights:"),
        ("Negativados", ":material/warning:"),
        ("Vendas e zerados", ":material/trending_down:"),
        ("Histórico de saídas", ":material/history:"),
        ("Recalcular custo (CMV)", ":material/calculate:"),
    ]),
    ("Entradas do SGI", [
        ("Entradas por compra (SGI)", ":material/local_shipping:"),
        ("Sincronização", ":material/sync:"),
        ("PDFs importados", ":material/picture_as_pdf:"),
        ("Não encontrados", ":material/help:"),
    ]),
    ("Cadastro", [
        ("Produtos", ":material/category:"),
        ("Importar base", ":material/upload_file:"),
    ]),
]

CSS = """
<style>
/* Paleta da barra lateral. Verde-ardósia escuro (serra ao entardecer) com um só
   acento, o amarelo da faixa de rótulo de defensivo. Tokens aqui em cima pra trocar
   a identidade num lugar só.

   SOBRE OS !important: o Streamlit estiliza os componentes com classes geradas
   (emotion) cuja especificidade varia de versão pra versão. Sem eles o alinhamento à
   esquerda não pegava - os itens apareciam centralizados no painel real mesmo passando
   num teste de CSS isolado. Estão só onde a regra PRECISA ganhar, não espalhados. */
:root {
  --nav-bg: #0E1B1A;
  --nav-surface: #1A2B28;
  --nav-line: #223734;
  --nav-text: #E7EEEB;
  --nav-muted: #8AA09B;
  --nav-accent: #C9A227;
}
[data-testid="stSidebar"] {
  background: var(--nav-bg); border-right: 1px solid var(--nav-line);
  min-width: 268px;   /* largura em que as três unidades cabem lado a lado */
}
[data-testid="stSidebar"] > div:first-child { padding-top: 1.1rem; }
/* 15 itens num menu só: sem apertar a pilha, metade fica abaixo da dobra. */
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap: .15rem; }

/* Marca */
.nav-marca { padding: 0 .25rem .8rem; }
.nav-marca b { color: var(--nav-text); font-size: 1.05rem; font-weight: 600; letter-spacing: -.01em; }
.nav-marca span { display: block; color: var(--nav-muted); font-size: .76rem; margin-top: .05rem; }

/* Rótulo de seção. padding em vez de margin, e line-height explícito: com margem o
   texto ficava cortado pelo bloco seguinte quando a pilha apertou. */
.nav-secao {
  color: var(--nav-muted); font-size: .72rem; font-weight: 600;
  line-height: 1.6; padding: .75rem .25rem .15rem; margin: 0;
}

/* Itens do menu */
[data-testid="stSidebar"] .stButton > button {
  width: 100%; display: flex !important; align-items: center;
  justify-content: flex-start !important; gap: .55rem; text-align: left !important;
  border: none; background: transparent; color: var(--nav-text);
  padding: .34rem .5rem; border-radius: 7px; font-weight: 400; font-size: .88rem;
  min-height: 0; line-height: 1.35;
  border-left: 3px solid transparent;  /* reserva o espaço da faixa: nada "pula" ao ativar */
}
/* O rótulo vive num container próprio dentro do <button>; alinhar só o button deixa
   o texto centralizado. */
[data-testid="stSidebar"] .stButton > button * { text-align: left !important; }
[data-testid="stSidebar"] .stButton > button > div { flex: 1 1 auto; min-width: 0; }
[data-testid="stSidebar"] .stButton > button p { margin: 0; }
[data-testid="stSidebar"] .stButton > button > span { flex: 0 0 auto; }
[data-testid="stSidebar"] .stButton > button:hover { background: var(--nav-surface); color: var(--nav-text); }
/* Ativo: a faixa de classificação. */
[data-testid="stSidebar"] .stButton > button[kind="primary"] {
  background: var(--nav-surface); border-left: 3px solid var(--nav-accent);
  color: #FFFFFF; font-weight: 500;
}
[data-testid="stSidebar"] .stButton > button:focus-visible { outline: 2px solid var(--nav-accent); outline-offset: 1px; }

/* Seletor de unidade: as três opções dividem a largura por igual e nunca transbordam.
   min-width:0 é o que permite o item encolher dentro do flex - sem ele o conteúdo
   define o tamanho e o grupo estoura a barra. */
[data-testid="stSidebar"] [data-testid="stSegmentedControl"] { margin-bottom: .1rem; max-width: 100%; }
[data-testid="stSidebar"] [data-testid="stSegmentedControl"] > div {
  flex-wrap: nowrap !important; width: 100%; gap: 2px;
}
[data-testid="stSidebar"] [data-testid="stSegmentedControl"] button {
  flex: 1 1 0; min-width: 0; font-size: .7rem; padding: .2rem .25rem;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-height: 0;
}
[data-testid="stSidebar"] hr { border-color: var(--nav-line); margin: .7rem 0 .2rem; }
</style>
"""


def _rotulo_unidade(nome: str) -> str:
    """Rótulo curto para o seletor. "Barra da Estiva" inteiro não cabe nas três opções
    lado a lado na largura da barra, e o controle quebrava em duas linhas deixando
    "Consolidado" solto, parecendo outro widget. O nome completo continua no tooltip e
    no aviso de contagem."""
    return "B. Estiva" if nome == "Barra da Estiva" else nome


def _secoes_completas(telas: list[str]) -> list[tuple[str, list[tuple[str, str]]]]:
    """SECOES, mais o que existir em PAGINAS e não estiver mapeado.

    Rede de segurança: quem criar uma tela nova e esquecer de mapear aqui ainda a
    encontra no menu, em vez de ela existir só no código."""
    mapeadas = {nome for _s, itens in SECOES for nome, _i in itens}
    secoes = [(titulo, [(n, i) for n, i in itens if n in telas]) for titulo, itens in SECOES]
    sobrando = [(n, ":material/chevron_right:") for n in telas if n not in mapeadas]
    if sobrando:
        secoes.append(("Outros", sobrando))
    return [(t, itens) for t, itens in secoes if itens]


def render(telas: list[str], unidades: list[str], aviso_unidade: str | None = None,
           erro_unidades: str | None = None) -> str:
    """Desenha a barra lateral e devolve o nome da tela escolhida.

    A tela fica em st.session_state['pagina'] e a unidade em
    st.session_state['unidade_estoque'] - os dois são lidos pelas páginas, então a
    navegação não precisa devolver nada além do nome."""
    st.markdown(CSS, unsafe_allow_html=True)
    if "pagina" not in st.session_state or st.session_state["pagina"] not in telas:
        st.session_state["pagina"] = telas[0]

    with st.sidebar:
        st.markdown(
            '<div class="nav-marca"><b>Porteira</b><span>Estoque de defensivos</span></div>',
            unsafe_allow_html=True)

        if len(unidades) > 1:
            padrao = "Barra da Estiva" if "Barra da Estiva" in unidades else unidades[0]
            if st.session_state.get("unidade_estoque") not in unidades:
                st.session_state["unidade_estoque"] = padrao
            st.segmented_control(
                "Unidade", unidades, key="unidade_estoque",
                selection_mode="single", label_visibility="collapsed",
                format_func=_rotulo_unidade,
                help="Cada unidade tem estoque próprio. 'Consolidado' soma as duas.")
        if erro_unidades:
            st.error("Não consegui ler as unidades; mostrando só o consolidado.", icon=":material/error:")
            st.caption(erro_unidades)
        if aviso_unidade:
            st.warning(aviso_unidade, icon=":material/warning:")

        for titulo, itens in _secoes_completas(telas):
            st.markdown(f'<div class="nav-secao">{titulo}</div>', unsafe_allow_html=True)
            for nome, icone in itens:
                ativo = nome == st.session_state["pagina"]
                if st.button(nome, key=f"nav_{nome}", icon=icone,
                             type="primary" if ativo else "tertiary", width="stretch"):
                    st.session_state["pagina"] = nome
                    st.rerun()

        st.divider()
        if st.button("Atualizar dados", key="nav_atualizar", icon=":material/refresh:",
                     type="tertiary", width="stretch"):
            st.cache_data.clear()
            st.rerun()

    return st.session_state["pagina"]
