"""
estoque/navegacao.py - A barra lateral do painel: marca, seletor de unidade e menu.

POR QUE UM MÓDULO SÓ PRA ISSO: a navegação cresceu pra 15 telas num `st.radio` corrido,
sem agrupamento nem hierarquia - dava pra ler, mas não dava pra ENCONTRAR. Aqui ela vira
quatro grupos nomeados pelo trabalho que a pessoa está fazendo ("Dia a dia", "Análise"),
não pelo tipo técnico da tela.

POR QUE RADIO E NÃO BOTÕES, que é o que parece mais moderno: com `st.button` o rótulo
fica CENTRALIZADO dentro do botão, e alinhar à esquerda exige vencer o CSS que o
Streamlit gera com classe própria. Tentei três vezes - inclusive com !important - e no
painel real continuou centralizado; de quebra, o markdown que fazia o título do grupo
acabou coberto pelo botão ativo. O radio alinha à esquerda sozinho, marca o item ativo
sozinho e usa o próprio label como título do grupo. Fica menos "de app", mas funciona em
qualquer versão do Streamlit sem depender de nome de classe gerada - que muda de versão
pra versão. A identidade visual vem da cor, do espaçamento e do agrupamento, que é
justamente o que CSS consegue fazer sem lutar com o componente.

Decisões visuais, para quem for mexer depois:

  - Cor só onde significa estado (unidade ativa, tela ativa, aviso). O resto é uma
    escala de cinza-esverdeado. O acento é o amarelo da faixa de classificação do
    rótulo de defensivo - é cor que informa, não enfeite.
  - A UNIDADE vem antes do menu: é a escolha que muda o SIGNIFICADO de todo número da
    tela, então precisa estar sempre visível e ser trocável num clique.
  - Título de grupo em caixa normal, não em CAIXA ALTA: a barra já tem pouca largura e
    versalete come legibilidade sem acrescentar hierarquia que o peso e a cor não dêem.
"""
from __future__ import annotations

import streamlit as st

CONSOLIDADO = "Consolidado"

# (grupo, [telas]). O nome TEM que bater com a chave em paginas.PAGINAS - o que sobrar
# cai em "Outros" automaticamente (ver _grupos), pra nenhuma tela sumir do menu quando
# alguém criar uma nova.
GRUPOS: list[tuple[str, list[str]]] = [
    ("Dia a dia", ["Visão geral", "Saldo por produto", "Movimentar estoque",
                   "Entrada por nota fiscal"]),
    ("Análise", ["Indicadores", "Negativados", "Vendas e zerados", "Histórico de saídas",
                 "Recalcular custo (CMV)"]),
    ("Entradas do SGI", ["Entradas por compra (SGI)", "Sincronização", "PDFs importados",
                         "Não encontrados"]),
    ("Cadastro", ["Produtos", "Importar base"]),
]

CSS = """
<style>
/* Verde-ardósia escuro (serra ao entardecer) com um só acento, o amarelo da faixa de
   rótulo de defensivo. Tokens aqui em cima pra trocar a identidade num lugar só. */
:root {
  --nav-bg: #0E1B1A;
  --nav-surface: #1A2B28;
  --nav-line: #223734;
  --nav-text: #E7EEEB;
  --nav-muted: #8AA09B;
  --nav-accent: #C9A227;
}
[data-testid="stSidebar"] { background: var(--nav-bg); border-right: 1px solid var(--nav-line); }
[data-testid="stSidebar"] > div:first-child { padding-top: 1.1rem; }

/* Marca */
.nav-marca { padding: 0 .25rem .5rem; }
.nav-marca b { color: var(--nav-text); font-size: 1.05rem; font-weight: 600; letter-spacing: -.01em; }
.nav-marca span { display: block; color: var(--nav-muted); font-size: .76rem; margin-top: .05rem; }

/* Título do grupo: é o label do próprio radio, então não existe bloco solto que possa
   acabar coberto pelo elemento de baixo - foi o que aconteceu na versão com botões. */
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {
  color: var(--nav-muted); font-size: .72rem; font-weight: 600; margin: 0;
}
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] { margin-bottom: .15rem; }

/* Opções: o radio já alinha à esquerda; aqui é só respiro, cor e o destaque do ativo. */
[data-testid="stSidebar"] [role="radiogroup"] { gap: 0; }
[data-testid="stSidebar"] [role="radiogroup"] label {
  padding: .16rem .4rem; border-radius: 6px; border-left: 3px solid transparent;
  color: var(--nav-text);
}
[data-testid="stSidebar"] [role="radiogroup"] label p { font-size: .88rem; }
[data-testid="stSidebar"] [role="radiogroup"] label:hover { background: var(--nav-surface); }
/* Ativo: a faixa de classificação. Onde :has() não existir, o item ativo ainda aparece
   pela bolinha marcada do próprio radio - o menu não deixa de funcionar. */
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) {
  background: var(--nav-surface); border-left-color: var(--nav-accent);
}
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) p { font-weight: 500; }

/* Separação entre grupos: espaço, sem linha. */
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap: .5rem; }
[data-testid="stSidebar"] hr { border-color: var(--nav-line); margin: .5rem 0 .1rem; }
</style>
"""


def _grupos(telas: list[str]) -> list[tuple[str, list[str]]]:
    """GRUPOS, mais o que existir em PAGINAS e não estiver mapeado.

    Rede de segurança: quem criar uma tela nova e esquecer de mapear aqui ainda a
    encontra no menu, em vez de ela existir só no código."""
    mapeadas = {n for _g, itens in GRUPOS for n in itens}
    grupos = [(titulo, [n for n in itens if n in telas]) for titulo, itens in GRUPOS]
    sobrando = [n for n in telas if n not in mapeadas]
    if sobrando:
        grupos.append(("Outros", sobrando))
    return [(t, itens) for t, itens in grupos if itens]


def _escolher(titulo: str, opcoes: list[str], atual: str, chave_estado: str) -> None:
    """Um radio por grupo, aparecendo marcado só no grupo que contém a escolha atual.

    A `key` inclui a escolha atual DE PROPÓSITO: assim, quando ela muda, os radios são
    recriados e cada um volta a refletir o `index` que passamos. Com key fixa, o
    Streamlit preservaria a seleção antiga de cada grupo e dois apareceriam marcados ao
    mesmo tempo."""
    idx = opcoes.index(atual) if atual in opcoes else None
    escolha = st.radio(titulo, opcoes, index=idx, key=f"{chave_estado}__{titulo}__{atual}")
    if escolha is not None and escolha != atual:
        st.session_state[chave_estado] = escolha
        st.rerun()


def render(telas: list[str], unidades: list[str], aviso_unidade: str | None = None,
           erro_unidades: str | None = None) -> str:
    """Desenha a barra lateral e devolve o nome da tela escolhida.

    A tela fica em st.session_state['pagina'] e a unidade em
    st.session_state['unidade_estoque'] - as páginas leem dali, então a navegação não
    precisa devolver nada além do nome."""
    st.markdown(CSS, unsafe_allow_html=True)
    if "pagina" not in st.session_state or st.session_state["pagina"] not in telas:
        st.session_state["pagina"] = telas[0]

    with st.sidebar:
        st.markdown(
            '<div class="nav-marca"><b>Porteira</b><span>Estoque de defensivos</span></div>',
            unsafe_allow_html=True)

        if len(unidades) > 1:
            if st.session_state.get("unidade_estoque") not in unidades:
                st.session_state["unidade_estoque"] = (
                    "Barra da Estiva" if "Barra da Estiva" in unidades else unidades[0])
            _escolher("Unidade", unidades, st.session_state["unidade_estoque"], "unidade_estoque")
        if erro_unidades:
            st.error("Não consegui ler as unidades; mostrando só o consolidado.",
                     icon=":material/error:")
            st.caption(erro_unidades)
        if aviso_unidade:
            st.warning(aviso_unidade, icon=":material/warning:")

        st.divider()
        for titulo, itens in _grupos(telas):
            _escolher(titulo, itens, st.session_state["pagina"], "pagina")

        st.divider()
        if st.button("Atualizar dados", key="nav_atualizar", icon=":material/refresh:",
                     width="stretch"):
            st.cache_data.clear()
            st.rerun()

    return st.session_state["pagina"]
