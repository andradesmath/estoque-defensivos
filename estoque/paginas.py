"""
estoque/paginas.py - Telas do painel Streamlit. Uma função por tela; app.py só roteia.

Regras de tela que valem em todas:
  - o saldo mostrado vem SEMPRE do banco (v_saldo_produto / kpis), nunca de estado local;
  - toda escrita chama `limpar_cache()` para a próxima leitura já refletir a mudança;
  - erros esperados (validação, duplicado) viram st.error, sem traceback.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from . import db, github_actions, kpis, parser_nfe, relatorios, sync_core
from .compat import LARG
from .importacao import validar_planilha
from .ui_util import ROTULO_SITUACAO, brl, estilo_situacao, num, para_excel, pct, vazio
from .util import hoje_brasil

DATA_CONTAGEM_PADRAO = date(2026, 9, 21)  # contagem física do fim do dia 21/09/2026
DATA_INICIAL_SYNC = date(2026, 9, 22)


# ------------------------------------------------------------------------------ dados
@st.cache_data(ttl=30, show_spinner=False)
def carregar_base() -> dict:
    return {
        "produtos": db.listar_produtos_base(),
        "mov": db.listar_movimentacao_agregada(),
        "aj": db.listar_ajustes_todos(),
    }


def limpar_cache() -> None:
    st.cache_data.clear()


def calcular_indicadores(janela_dias: int = 30, cobertura_alvo: int = 30, excesso_dias: int = 120,
                         dias_sem_giro: int = 30) -> pd.DataFrame:
    b = carregar_base()
    return kpis.indicadores(b["produtos"], b["mov"], b["aj"], hoje_brasil(), janela_dias=janela_dias,
                            cobertura_alvo=cobertura_alvo, excesso_dias=excesso_dias, dias_sem_giro=dias_sem_giro)


def _rotulo_produto(cod: str, mapa_desc: dict) -> str:
    return f"{cod} — {mapa_desc.get(cod, '?')}"


def _seletor_produto(rotulo: str, produtos: pd.DataFrame, key: str, incluir_inativos: bool = True):
    df = produtos if incluir_inativos else produtos[produtos["ativo"]]
    if df.empty:
        st.info("Nenhum produto cadastrado ainda. Importe a base em **Importar base** ou adicione em **Produtos**.")
        return None
    mapa = dict(zip(df["cod_produto"], df["descricao"]))
    return st.selectbox(rotulo, list(mapa), format_func=lambda c: _rotulo_produto(c, mapa), key=key)


def _opt(v):
    """Valor de widget -> None quando 0/vazio em campos opcionais."""
    return None if v in (None, "", 0, 0.0) else v


# --------------------------------------------------------------------------- visão geral
def pagina_visao_geral() -> None:
    st.header("Visão geral do estoque")
    ind = calcular_indicadores()
    if ind.empty:
        st.info("Sem produtos ainda. Comece por **Importar base**.")
        return
    r = kpis.resumo_geral(ind)

    dias = int(r["dias_periodo"])
    curto = dias < 15  # anualizar 3 dias de dados produz números sem sentido (ex.: ROI de 500%)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Valor do estoque (custo)", brl(r["valor_estoque_custo"]))
    c2.metric("Valor a preço de venda", brl(r["valor_estoque_venda"]))
    c3.metric("Lucro potencial", brl(r["lucro_potencial"]))
    rot_parado = "Capital parado" + (f" ({pct(r['pct_capital_parado'])} do estoque)" if not vazio(r["pct_capital_parado"]) else "")
    c4.metric(rot_parado, brl(r["capital_parado"]), help="Custo do estoque de itens 'Sem giro' ou em 'Excesso'.")
    c5, c6, c7, c8 = st.columns(4)
    if curto:
        c5.metric(f"Giro no período ({dias} dia(s))", (num(r["giro_periodo"], 2) + "x") if not vazio(r["giro_periodo"]) else "—",
                  help="Com menos de 15 dias de histórico mostramos o valor do período, sem anualizar.")
        c6.metric("Prazo médio de giro", "—", help="Aparece com 15+ dias de histórico.")
        c7.metric(f"ROI do estoque no período ({dias} dia(s))",
                  pct(r["roi_periodo"] * 100) if not vazio(r["roi_periodo"]) else "—",
                  help="Lucro bruto ÷ estoque médio a custo, no período (GMROI).")
    else:
        c5.metric("Giro anualizado", (num(r["giro_anual"], 1) + "x") if not vazio(r["giro_anual"]) else "—")
        c6.metric("Prazo médio de giro", (num(r["dias_de_giro"], 0) + " dias") if not vazio(r["dias_de_giro"]) else "—")
        c7.metric("ROI do estoque (anualizado)", pct(r["roi_anual"] * 100) if not vazio(r["roi_anual"]) else "—",
                  help="Lucro bruto ÷ estoque médio a custo, anualizado de forma linear (GMROI).")
    c8.metric("Produtos ativos", num(r["n_produtos"]))

    if r["n_sem_custo"]:
        st.warning(f"{r['n_sem_custo']} produto(s) sem preço de custo: ficam fora de valor de estoque, giro e ROI. "
                   "Cadastre em **Produtos > Parâmetros em massa**.")
    if curto and dias > 0:
        st.caption(f"Só {dias} dia(s) de histórico: giro e ROI aparecem sem anualizar e a cobertura oscila. "
                   "Os números se estabilizam com algumas semanas de dados.")

    st.subheader("Alertas")
    a1, a2, a3, a4, a5 = st.columns(5)
    a1.metric("Saldo negativo", r["n_negativos"])
    a2.metric("Zerados", r["n_ruptura"])
    a3.metric("Repor", r["n_repor"])
    a4.metric("Sem giro", r["n_sem_giro"])
    a5.metric("Excesso", r["n_excesso"])
    nao_enc = db.listar_nao_encontrados()
    if not nao_enc.empty:
        st.warning(f"{len(nao_enc)} código(s) vendidos no SGI sem cadastro aqui — veja **Não encontrados**.")
    for sit in ("Negativo", "Ruptura", "Repor"):
        sub = ind[(ind["ativo"]) & (ind["situacao"] == sit)]
        if not sub.empty:
            with st.expander(f"{sit} — {len(sub)} produto(s): {ROTULO_SITUACAO[sit]}"):
                st.dataframe(sub[["cod_produto", "descricao", "saldo_atual", "ponto_pedido", "sugestao_compra"]],
                             hide_index=True, **LARG)

    b = carregar_base()
    ev = kpis.evolucao_valor_estoque(b["produtos"], b["mov"], b["aj"], hoje_brasil())
    if not ev.empty and ev["valor"].sum() > 0:
        st.subheader("Evolução do valor do estoque (a custo atual)")
        fig = px.line(ev, x="data", y="valor", labels={"data": "", "valor": "R$"})
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=280)
        st.plotly_chart(fig, **LARG)


# ----------------------------------------------------------------------- saldo por produto
def pagina_saldo() -> None:
    st.header("Saldo por produto")
    ind = calcular_indicadores()
    if ind.empty:
        st.info("Sem produtos ainda.")
        return
    f1, f2, f3, f4 = st.columns([3, 2, 2, 2])
    busca = f1.text_input("Buscar (código ou nome)")
    situacoes = f2.multiselect("Situação", ["Negativo", "Ruptura", "Repor", "Sem giro", "Excesso", "OK"])
    ativos = f3.selectbox("Cadastro", ["Só ativos", "Só inativos", "Todos"])
    abc = f4.multiselect("Curva ABC", ["A", "B", "C", "—"])
    d = ind.copy()
    if ativos == "Só ativos":
        d = d[d["ativo"]]
    elif ativos == "Só inativos":
        d = d[~d["ativo"]]
    if busca:
        b = busca.strip().lower()
        d = d[d["cod_produto"].str.lower().str.contains(b) | d["descricao"].str.lower().str.contains(b)]
    if situacoes:
        d = d[d["situacao"].isin(situacoes)]
    if abc:
        d = d[d["curva_abc"].isin(abc)]

    cols = ["cod_produto", "descricao", "unidade", "saldo_atual", "situacao", "saidas_qtd", "venda_media_dia",
            "cobertura_dias", "preco_custo", "valor_estoque_custo", "curva_abc"]
    vis = d[cols].sort_values("descricao")
    st.caption(f"{len(vis)} produto(s). Saldo = contagem inicial + entradas/ajustes − saídas do SGI (calculado a cada leitura).")
    st.metric("Total investido em estoque (a custo) — considerando os filtros acima",
             brl(float(vis["valor_estoque_custo"].sum(skipna=True))))
    st.dataframe(
        vis.style.map(estilo_situacao, subset=["situacao"]),
        hide_index=True, **LARG, height=520,
        column_config={
            "cod_produto": "Código", "descricao": "Produto", "unidade": "Un.",
            "saldo_atual": st.column_config.NumberColumn("Saldo", format="%.0f"),
            "situacao": "Situação",
            "saidas_qtd": st.column_config.NumberColumn("Saídas (período)", format="%.0f"),
            "venda_media_dia": st.column_config.NumberColumn("Venda/dia", format="%.2f"),
            "cobertura_dias": st.column_config.NumberColumn("Cobertura (dias)", format="%.0f"),
            "preco_custo": st.column_config.NumberColumn("Custo", format="R$ %.2f"),
            "valor_estoque_custo": st.column_config.NumberColumn("Valor em estoque", format="R$ %.2f"),
            "curva_abc": "ABC",
        },
    )
    c1, c2 = st.columns(2)
    c1.download_button("Baixar Excel", para_excel(vis, "Saldo"), "saldo_defensivos.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_saldo_xlsx")
    c2.download_button("Baixar PDF (para imprimir)", relatorios.gerar_pdf_saldo_estoque(vis),
                       "saldo_e_custo_estoque.pdf", "application/pdf", key="dl_saldo_pdf")


# ---------------------------------------------------------------------------------- produtos
def pagina_produtos() -> None:
    st.header("Produtos")
    tab_add, tab_edit, tab_lote = st.tabs(["Adicionar", "Editar / remover", "Parâmetros em massa"])
    produtos = carregar_base()["produtos"]

    with tab_add:
        _form_adicionar_produto()

    with tab_edit:
        cod = _seletor_produto("Produto", produtos, key="edit_prod")
        if cod:
            _form_editar_produto(cod)

    with tab_lote:
        _editor_em_massa(produtos)


def _form_adicionar_produto(cod_prefill: str = "", desc_prefill: str = "", key: str = "novo") -> None:
    st.caption("A contagem física vale para o FIM da data informada: só vendas do dia seguinte em diante são descontadas.")
    with st.form(f"form_add_{key}", clear_on_submit=True):
        c1, c2, c3 = st.columns([1, 3, 1])
        cod = c1.text_input("Código (SGI)", value=cod_prefill)
        desc = c2.text_input("Descrição", value=desc_prefill)
        un = c3.text_input("Unidade", placeholder="UN, LT, KG")
        c4, c5 = st.columns(2)
        saldo = c4.number_input("Saldo na contagem", value=0.0, step=1.0, format="%.3f")
        dt = c5.date_input("Data da contagem", value=hoje_brasil(), format="DD/MM/YYYY")
        c6, c7, c8, c9 = st.columns(4)
        custo = c6.number_input("Preço de custo (R$)", min_value=0.0, value=0.0, step=0.01, format="%.2f")
        venda = c7.number_input("Preço de venda (R$)", min_value=0.0, value=0.0, step=0.01, format="%.2f")
        lead = c8.number_input("Lead time de reposição (dias)", min_value=0, value=0, step=1)
        minimo = c9.number_input("Estoque mínimo", min_value=0.0, value=0.0, step=1.0)
        forn = st.text_input("Fornecedor")
        obs = st.text_input("Observação")
        ok = st.form_submit_button("Adicionar produto", type="primary")
    if ok:
        try:
            novo = db.criar_produto(cod, desc, saldo, dt, unidade=un, preco_custo=_opt(custo), preco_venda=_opt(venda),
                                    lead_time_dias=_opt(lead), estoque_minimo=_opt(minimo), fornecedor=forn, observacao=obs)
            limpar_cache()
            st.success(f"Produto {novo} adicionado.")
        except (ValueError, db.ProdutoDuplicado) as e:
            st.error(str(e))


def _form_editar_produto(cod: str) -> None:
    p = db.obter_produto(cod)
    if p is None:
        st.error("Produto não encontrado.")
        return
    st.metric("Saldo atual", num(p["saldo_atual"], 0))
    st.caption("Para corrigir o saldo com trilha de auditoria, use **Movimentar estoque > Contagem**. "
               "Editar a contagem inicial abaixo reescreve a base do cálculo.")
    with st.form(f"form_edit_{cod}"):
        c1, c2 = st.columns([3, 1])
        desc = c1.text_input("Descrição", value=p["descricao"])
        un = c2.text_input("Unidade", value=p["unidade"] or "")
        c3, c4 = st.columns(2)
        saldo_ini = c3.number_input("Saldo na contagem inicial", value=float(p["saldo_inicial"]), step=1.0, format="%.3f")
        dt = c4.date_input("Data da contagem inicial", value=p["data_saldo_inicial"], format="DD/MM/YYYY")
        c5, c6, c7, c8 = st.columns(4)
        custo = c5.number_input("Preço de custo (R$)", min_value=0.0, value=0.0 if vazio(p["preco_custo"]) else float(p["preco_custo"]), step=0.01, format="%.2f")
        venda = c6.number_input("Preço de venda (R$)", min_value=0.0, value=0.0 if vazio(p["preco_venda"]) else float(p["preco_venda"]), step=0.01, format="%.2f")
        lead = c7.number_input("Lead time (dias)", min_value=0, value=0 if vazio(p["lead_time_dias"]) else int(p["lead_time_dias"]), step=1)
        minimo = c8.number_input("Estoque mínimo", min_value=0.0, value=0.0 if vazio(p["estoque_minimo"]) else float(p["estoque_minimo"]), step=1.0)
        forn = st.text_input("Fornecedor", value=p["fornecedor"] or "")
        obs = st.text_input("Observação", value=p["observacao"] or "")
        salvar = st.form_submit_button("Salvar alterações", type="primary")
    if salvar:
        try:
            db.atualizar_produto(cod, descricao=desc, unidade=un, saldo_inicial=saldo_ini, data_saldo_inicial=dt,
                                 preco_custo=_opt(custo), preco_venda=_opt(venda), lead_time_dias=_opt(lead),
                                 estoque_minimo=_opt(minimo), fornecedor=forn, observacao=obs)
            limpar_cache()
            st.success("Alterações salvas.")
            st.rerun()
        except ValueError as e:
            st.error(str(e))

    st.divider()
    b1, b2 = st.columns(2)
    with b1:
        if p["ativo"]:
            if st.button("Inativar produto", help="Some das listas, mantém histórico e vendas.", key=f"inativar_{cod}"):
                db.definir_ativo(cod, False); limpar_cache(); st.rerun()
        else:
            if st.button("Reativar produto", key=f"reativar_{cod}"):
                db.definir_ativo(cod, True); limpar_cache(); st.rerun()
    with b2:
        with st.expander("Excluir definitivamente"):
            st.warning("Apaga o produto e seus ajustes manuais. As vendas do SGI permanecem e o código volta a "
                       "aparecer em 'Não encontrados'. Prefira inativar.")
            confirma = st.text_input(f"Digite {cod} para confirmar", key=f"conf_del_{cod}")
            if st.button("Excluir", disabled=confirma.strip() != cod, key=f"del_{cod}"):
                db.excluir_produto(cod); limpar_cache(); st.rerun()


def _editor_em_massa(produtos: pd.DataFrame) -> None:
    st.caption("Preencha custo, preço de venda, lead time e mínimo direto na tabela e salve. "
               "Campo apagado vira 'não informado'. Sem custo, o produto fica fora de valor de estoque, giro e ROI.")
    if produtos.empty:
        st.info("Sem produtos.")
        return
    f1, f2 = st.columns([3, 2])
    busca = f1.text_input("Filtrar", key="lote_busca")
    so_sem_custo = f2.checkbox("Só sem custo", key="lote_sem_custo")
    cols = ["cod_produto", "descricao", "preco_custo", "preco_venda", "lead_time_dias", "estoque_minimo", "fornecedor"]
    base = produtos[produtos["ativo"]][cols].copy()
    for c in ("preco_custo", "preco_venda", "lead_time_dias", "estoque_minimo"):
        base[c] = pd.to_numeric(base[c], errors="coerce")
    if busca:
        b = busca.strip().lower()
        base = base[base["cod_produto"].str.lower().str.contains(b) | base["descricao"].str.lower().str.contains(b)]
    if so_sem_custo:
        base = base[base["preco_custo"].isna()]
    editado = st.data_editor(
        base, hide_index=True, **LARG, height=480, key="editor_params",
        disabled=["cod_produto", "descricao"],
        column_config={
            "cod_produto": "Código", "descricao": "Produto",
            "preco_custo": st.column_config.NumberColumn("Custo (R$)", min_value=0.0, format="%.2f"),
            "preco_venda": st.column_config.NumberColumn("Venda (R$)", min_value=0.0, format="%.2f"),
            "lead_time_dias": st.column_config.NumberColumn("Lead time (dias)", min_value=0, step=1, format="%d"),
            "estoque_minimo": st.column_config.NumberColumn("Mínimo", min_value=0.0, format="%.0f"),
            "fornecedor": "Fornecedor",
        },
    )
    alteracoes = []
    for idx in editado.index:
        mudou = {}
        for c in ("preco_custo", "preco_venda", "lead_time_dias", "estoque_minimo", "fornecedor"):
            novo, velho = editado.at[idx, c], base.at[idx, c]
            if vazio(novo) and vazio(velho):
                continue
            if vazio(novo) != vazio(velho) or novo != velho:
                mudou[c] = None if vazio(novo) else novo
        if mudou:
            alteracoes.append({"cod_produto": editado.at[idx, "cod_produto"], **mudou})
    st.caption(f"{len(alteracoes)} linha(s) alterada(s) pendente(s).")
    if st.button("Salvar alterações", type="primary", disabled=not alteracoes, key="salvar_lote"):
        try:
            n = db.atualizar_produtos_em_lote(alteracoes)
            limpar_cache()
            st.success(f"{n} produto(s) atualizado(s).")
            st.rerun()
        except ValueError as e:
            st.error(str(e))


# ------------------------------------------------------------------------ movimentar estoque
def pagina_movimentar() -> None:
    st.header("Movimentar estoque")
    produtos = carregar_base()["produtos"]
    tab_reg, tab_hist = st.tabs(["Registrar", "Histórico de ajustes"])

    with tab_reg:
        cod = _seletor_produto("Produto", produtos[produtos["ativo"]], key="mov_prod", incluir_inativos=True)
        if cod:
            p = db.obter_produto(cod)
            st.metric("Saldo atual no sistema", num(p["saldo_atual"], 0))
            tipos = {k: v[1] for k, v in db.TIPOS_AJUSTE.items()}
            chaves = ["correcao"] + [k for k in tipos if k != "correcao"]
            tipo = st.radio("Tipo", chaves, format_func=lambda k: {"correcao": "Contagem (informar o saldo contado)"}.get(k, tipos[k]),
                            horizontal=True, key="mov_tipo")
            min_dt = p["data_saldo_inicial"] + timedelta(days=1)
            with st.form("form_mov", clear_on_submit=True):
                dt = st.date_input("Data", value=max(hoje_brasil(), min_dt), min_value=min_dt, max_value=hoje_brasil(),
                                   format="DD/MM/YYYY")
                if tipo == "correcao":
                    q = st.number_input("Saldo contado (o que existe de fato)", value=float(p["saldo_atual"]), step=1.0, format="%.3f")
                    custo, atualizar = 0.0, False
                else:
                    q = st.number_input("Quantidade (sempre positiva)", min_value=0.0, value=0.0, step=1.0, format="%.3f")
                    custo, atualizar = 0.0, False
                    if tipo == "entrada":
                        custo = st.number_input("Custo unitário desta compra (R$)", min_value=0.0, value=0.0, step=0.01, format="%.2f")
                        atualizar = st.checkbox("Atualizar o preço de custo do produto com este valor")
                obs = st.text_input("Observação (nota, destino da transferência, motivo…)")
                ok = st.form_submit_button("Registrar", type="primary")
            if ok:
                try:
                    if tipo == "correcao":
                        delta = db.definir_saldo_por_contagem(cod, q, dt, obs)
                        st.success("Saldo já confere; nada registrado." if delta == 0 else f"Ajuste de {num(float(delta), 0)} registrado.")
                    else:
                        db.registrar_ajuste(cod, dt, tipo, q, custo_unitario=_opt(custo), observacao=obs, atualizar_custo=atualizar)
                        st.success("Movimento registrado.")
                    limpar_cache()
                except ValueError as e:
                    st.error(str(e))

    with tab_hist:
        aj = db.listar_ajustes()
        if aj.empty:
            st.info("Nenhum ajuste manual registrado.")
        else:
            aj["tipo"] = aj["tipo"].map(lambda t: db.TIPOS_AJUSTE[t][1])
            st.dataframe(aj.drop(columns=["criado_em"]), hide_index=True, **LARG,
                         column_config={"quantidade": st.column_config.NumberColumn("Qtd (±)", format="%.0f"),
                                        "custo_unitario": st.column_config.NumberColumn("Custo unit.", format="R$ %.2f")})
            with st.expander("Excluir um ajuste lançado por engano"):
                aid = st.number_input("ID do ajuste", min_value=1, step=1, key="del_aj_id")
                if st.button("Excluir ajuste", key="del_aj"):
                    db.excluir_ajuste(int(aid)); limpar_cache(); st.rerun()


# --------------------------------------------------------------------- entrada por nota fiscal
def pagina_entrada_nfe() -> None:
    st.header("Entrada por nota fiscal (PDF)")
    st.caption("Envie o PDF da nota (DANFE) do fornecedor. O sistema tenta reconhecer os itens "
               "automaticamente — confira e corrija antes de confirmar; nada é gravado sem sua revisão. "
               "Cada item confirmado soma a quantidade ao saldo do produto escolhido e atualiza o custo "
               "(mesma regra de 'Movimentar estoque' → Entrada).")
    arq = st.file_uploader("PDF da nota fiscal", type=["pdf"], key="nfe_upload")
    if arq is None:
        return

    chave = (arq.name, arq.size)
    if st.session_state.get("nfe_chave") != chave:
        with st.spinner("Lendo a nota..."):
            nfe = parser_nfe.extrair_danfe(arq.getvalue())
        produtos_df = carregar_base()["produtos"]
        mapa_desc_todos = dict(zip(produtos_df["cod_produto"], produtos_df["descricao"]))
        mapa_forn = db.buscar_mapa_fornecedor(nfe.cnpj_emitente) if nfe.cnpj_emitente else {}
        linhas = [{
            "incluir": True,
            "cod_fornecedor": it.cod_fornecedor,
            "descricao_nf": it.descricao,
            "quantidade": it.quantidade,
            "valor_unitario": it.valor_unitario,
            "produto": _rotulo_produto(mapa_forn[it.cod_fornecedor], mapa_desc_todos)
                       if it.cod_fornecedor in mapa_forn else None,
        } for it in nfe.itens]
        st.session_state["nfe_chave"] = chave
        st.session_state["nfe_extraida"] = nfe
        st.session_state["nfe_linhas"] = pd.DataFrame(
            linhas, columns=["incluir", "cod_fornecedor", "descricao_nf", "quantidade", "valor_unitario", "produto"])

    nfe = st.session_state["nfe_extraida"]
    info = [t for t in [
        f"**Fornecedor:** {nfe.nome_emitente}" if nfe.nome_emitente else None,
        f"**Nota nº** {nfe.numero}" if nfe.numero else None,
        f"**Emissão:** {nfe.data_emissao}" if nfe.data_emissao else None,
    ] if t]
    if info:
        st.caption(" · ".join(info))
    if nfe.aviso:
        st.warning(nfe.aviso)
    if not nfe.cnpj_emitente:
        st.info("Não encontrei o CNPJ do fornecedor nesta nota — a associação código-do-fornecedor → "
                "produto não será lembrada para a próxima nota dele.")

    ativos = carregar_base()["produtos"]
    ativos = ativos[ativos["ativo"]]
    mapa_desc = dict(zip(ativos["cod_produto"], ativos["descricao"]))
    opcoes = [_rotulo_produto(c, mapa_desc) for c in sorted(mapa_desc)]

    dt_entrada = st.date_input("Data de entrada no estoque", value=hoje_brasil(), format="DD/MM/YYYY", key="nfe_data")
    if st.session_state["nfe_linhas"].empty:
        st.session_state["nfe_linhas"] = pd.DataFrame(
            columns=["incluir", "cod_fornecedor", "descricao_nf", "quantidade", "valor_unitario", "produto"])
    editado = st.data_editor(
        st.session_state["nfe_linhas"], key="nfe_editor", hide_index=True, **LARG, num_rows="dynamic",
        column_config={
            "incluir": st.column_config.CheckboxColumn("Gravar?", default=True),
            "cod_fornecedor": "Código (fornecedor)",
            "descricao_nf": "Descrição na nota",
            "quantidade": st.column_config.NumberColumn("Quantidade", format="%.3f", min_value=0.0),
            "valor_unitario": st.column_config.NumberColumn("Valor unit. (R$)", format="%.4f", min_value=0.0),
            "produto": st.column_config.SelectboxColumn("Produto no sistema", options=opcoes),
        },
    )
    st.session_state["nfe_linhas"] = editado

    if st.button("Confirmar entrada", type="primary", key="nfe_confirmar"):
        erros, gravados = [], 0
        for _, r in editado.iterrows():
            if not r["incluir"]:
                continue
            rotulo_produto, qtd_nf = r["produto"], r["quantidade"]
            if not rotulo_produto:
                erros.append(f"'{r['descricao_nf'] or r['cod_fornecedor']}': sem produto selecionado — não gravado.")
                continue
            cod = str(rotulo_produto).split(" — ")[0].strip()
            try:
                qtd = float(qtd_nf or 0)
                if qtd <= 0:
                    raise ValueError("quantidade deve ser maior que zero")
                db.registrar_ajuste(
                    cod, dt_entrada, "entrada", qtd, custo_unitario=_opt(float(r["valor_unitario"] or 0)),
                    observacao=f"NF {nfe.numero or '?'} — {nfe.nome_emitente or 'fornecedor não identificado'}",
                    atualizar_custo=True,
                )
                if nfe.cnpj_emitente and r["cod_fornecedor"]:
                    db.salvar_mapa_fornecedor(nfe.cnpj_emitente, str(r["cod_fornecedor"]), cod, r["descricao_nf"])
                gravados += 1
            except ValueError as e:
                erros.append(f"{cod}: {e}")
        limpar_cache()
        if gravados:
            st.success(f"{gravados} item(ns) somado(s) ao estoque.")
            del st.session_state["nfe_chave"]  # se subir a mesma nota de novo, relê do zero
        for e in erros:
            st.error(e)


# ------------------------------------------------------------------------------ indicadores
def pagina_indicadores() -> None:
    st.header("Indicadores de estoque")
    with st.expander("Parâmetros de análise", expanded=False):
        p1, p2, p3, p4 = st.columns(4)
        janela = p1.selectbox("Período de análise", [7, 15, 30, 60, 90, 180, 365], index=2, format_func=lambda d: f"últimos {d} dias")
        cob_alvo = p2.number_input("Cobertura-alvo p/ compra (dias)", min_value=1, value=30)
        excesso = p3.number_input("Excesso acima de (dias de cobertura)", min_value=1, value=120)
        sem_giro = p4.number_input("Sem giro após (dias sem venda)", min_value=1, value=30)
    ind = calcular_indicadores(janela, int(cob_alvo), int(excesso), int(sem_giro))
    ind = ind[ind["ativo"]] if not ind.empty else ind
    if ind.empty:
        st.info("Sem produtos ativos.")
        return
    r = kpis.resumo_geral(ind)
    st.caption(f"Base de cálculo: {int(r['dias_periodo'])} dia(s) de histórico dentro do período escolhido. "
               "Custo = o cadastrado hoje; receita = valor faturado no SGI.")
    if r["n_sem_custo"]:
        st.warning(f"{r['n_sem_custo']} produto(s) sem custo ficam de fora de giro, ROI e valor de estoque.")

    t1, t2, t3, t4 = st.tabs(["Giro e ROI", "Reposição", "Curva ABC", "Estoque parado"])

    with t1:
        cols = ["cod_produto", "descricao", "saldo_atual", "saidas_qtd", "receita", "cmv", "lucro_bruto",
                "margem_realizada_pct", "estoque_medio_valor", "giro_anual", "dias_de_giro", "roi_anual", "curva_abc"]
        d = ind[cols].sort_values("lucro_bruto", ascending=False, na_position="last")
        st.dataframe(d, hide_index=True, **LARG, height=520, column_config={
            "cod_produto": "Código", "descricao": "Produto",
            "saldo_atual": st.column_config.NumberColumn("Saldo", format="%.0f"),
            "saidas_qtd": st.column_config.NumberColumn("Vendido", format="%.0f"),
            "receita": st.column_config.NumberColumn("Receita", format="R$ %.2f"),
            "cmv": st.column_config.NumberColumn("CMV", format="R$ %.2f"),
            "lucro_bruto": st.column_config.NumberColumn("Lucro bruto", format="R$ %.2f"),
            "margem_realizada_pct": st.column_config.NumberColumn("Margem %", format="%.1f"),
            "estoque_medio_valor": st.column_config.NumberColumn("Estoque médio (R$)", format="R$ %.2f"),
            "giro_anual": st.column_config.NumberColumn("Giro anual (x)", format="%.1f"),
            "dias_de_giro": st.column_config.NumberColumn("Dias de giro", format="%.0f"),
            "roi_anual": st.column_config.NumberColumn("ROI anual", format="%.2f"),
            "curva_abc": "ABC",
        })
        st.download_button("Baixar Excel", para_excel(d, "Giro e ROI"), "indicadores_defensivos.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_ind")
        with st.expander("Como cada número é calculado"):
            st.markdown(
                "- **Giro anual** = (CMV ÷ estoque médio a custo) × 365 ÷ dias do período.\n"
                "- **Dias de giro** = dias do período ÷ giro do período (quanto tempo o capital fica parado).\n"
                "- **ROI anual (GMROI)** = (lucro bruto ÷ estoque médio a custo) × 365 ÷ dias — anualização linear.\n"
                "- **Cobertura** = saldo atual ÷ venda média diária.\n"
                "- Estoque médio = média dos saldos de fim de dia no período.")

    with t2:
        rep = ind[ind["situacao"].isin(["Negativo", "Ruptura", "Repor"]) | (ind["sugestao_compra"] > 0)]
        rep = rep[["cod_produto", "descricao", "situacao", "saldo_atual", "venda_media_dia", "lead_time_dias",
                   "ponto_pedido", "sugestao_compra"]].sort_values("sugestao_compra", ascending=False, na_position="last")
        st.caption("Sugestão = venda média/dia × (lead time + cobertura-alvo) − saldo, arredondada para cima. "
                   "Exige lead time cadastrado e vendas no período.")
        if rep.empty:
            st.success("Nenhum item pede reposição agora.")
        else:
            st.dataframe(rep.style.map(estilo_situacao, subset=["situacao"]), hide_index=True, **LARG,
                         column_config={"venda_media_dia": st.column_config.NumberColumn("Venda/dia", format="%.2f"),
                                        "ponto_pedido": st.column_config.NumberColumn("Ponto de pedido", format="%.1f"),
                                        "sugestao_compra": st.column_config.NumberColumn("Comprar (sugestão)", format="%.0f"),
                                        "saldo_atual": st.column_config.NumberColumn("Saldo", format="%.0f")})

    with t3:
        vend = ind[ind["receita"] > 0].sort_values("receita", ascending=False)
        if vend.empty:
            st.info("Sem vendas no período.")
        else:
            cont = ind["curva_abc"].value_counts().reindex(["A", "B", "C", "—"], fill_value=0)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Classe A (≈80% da receita)", int(cont["A"]))
            m2.metric("Classe B (≈15%)", int(cont["B"]))
            m3.metric("Classe C (≈5%)", int(cont["C"]))
            m4.metric("Sem venda", int(cont["—"]))
            top = vend.head(25).copy()
            top["acum"] = vend["receita"].cumsum().head(25) / vend["receita"].sum() * 100
            fig = go.Figure()
            fig.add_bar(x=top["descricao"], y=top["receita"], name="Receita (R$)")
            fig.add_scatter(x=top["descricao"], y=top["acum"], name="% acumulado", yaxis="y2", mode="lines+markers")
            fig.update_layout(yaxis2=dict(overlaying="y", side="right", range=[0, 100], title="%"),
                              margin=dict(l=0, r=0, t=10, b=0), height=380, legend=dict(orientation="h"))
            st.plotly_chart(fig, **LARG)
            st.caption("Classe pela receita do período: A até 80% acumulado, B até 95%, C o restante.")

    with t4:
        par = ind[ind["situacao"].isin(["Sem giro", "Excesso"])]
        par = par[["cod_produto", "descricao", "situacao", "saldo_atual", "cobertura_dias", "preco_custo",
                   "valor_estoque_custo"]].sort_values("valor_estoque_custo", ascending=False, na_position="last")
        if par.empty:
            st.success("Nenhum produto parado ou em excesso com os parâmetros atuais.")
        else:
            st.metric("Capital parado (a custo)", brl(float(par["valor_estoque_custo"].sum(skipna=True))))
            st.dataframe(par.style.map(estilo_situacao, subset=["situacao"]), hide_index=True, **LARG,
                         column_config={"cobertura_dias": st.column_config.NumberColumn("Cobertura (dias)", format="%.0f"),
                                        "preco_custo": st.column_config.NumberColumn("Custo", format="R$ %.2f"),
                                        "valor_estoque_custo": st.column_config.NumberColumn("Valor parado", format="R$ %.2f"),
                                        "saldo_atual": st.column_config.NumberColumn("Saldo", format="%.0f")})


# ------------------------------------------------------------------------ histórico de saídas
def pagina_historico() -> None:
    st.header("Histórico de saídas (vendas do SGI)")
    produtos = carregar_base()["produtos"]
    mapa = dict(zip(produtos["cod_produto"], produtos["descricao"])) if not produtos.empty else {}
    f1, f2, f3 = st.columns([3, 2, 2])
    cods = ["(todos)"] + sorted(mapa)
    cod = f1.selectbox("Produto", cods, format_func=lambda c: c if c == "(todos)" else _rotulo_produto(c, mapa))
    ini = f2.date_input("De", value=DATA_INICIAL_SYNC, format="DD/MM/YYYY")
    fim = f3.date_input("Até", value=hoje_brasil(), format="DD/MM/YYYY")
    mov = db.listar_movimentacao(ini=ini, fim=fim, cod=None if cod == "(todos)" else cod)
    if mov.empty:
        st.info("Nenhuma saída no filtro.")
        return
    mov["produto"] = mov["cod_produto"].map(mapa).fillna(mov["descricao_sgi"])
    lojas = sorted(mov["loja"].unique())
    sel = st.multiselect("Loja", lojas, default=lojas)
    mov = mov[mov["loja"].isin(sel)]
    m1, m2 = st.columns(2)
    m1.metric("Unidades vendidas", num(float(mov["quantidade_saida"].sum()), 0))
    m2.metric("Valor vendido", brl(float(mov["valor_saida"].sum())))
    diario = mov.groupby(["data", "loja"], as_index=False)["quantidade_saida"].sum()
    fig = px.bar(diario, x="data", y="quantidade_saida", color="loja", barmode="stack",
                 labels={"data": "", "quantidade_saida": "Unidades"})
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=280)
    st.plotly_chart(fig, **LARG)
    tabela = mov[["data", "loja", "cod_produto", "produto", "quantidade_saida", "valor_saida"]]
    st.dataframe(tabela, hide_index=True, **LARG, height=380, column_config={
        "quantidade_saida": st.column_config.NumberColumn("Qtd", format="%.0f"),
        "valor_saida": st.column_config.NumberColumn("Valor", format="R$ %.2f")})
    st.download_button("Baixar Excel", para_excel(tabela, "Saidas"), "saidas_defensivos.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_saidas")


# --------------------------------------------------------------------- vendas por loja / zerados
def pagina_vendas_zerados() -> None:
    st.header("Vendas por loja e produtos zerados")
    st.caption("Total vendido por loja no período e, abaixo, cada produto com saldo zerado ou "
               "negativo — com o dia e a loja de cada venda que o zerou.")
    f1, f2 = st.columns(2)
    ini = f1.date_input("De", value=DATA_INICIAL_SYNC, format="DD/MM/YYYY", key="vz_ini")
    fim = f2.date_input("Até", value=hoje_brasil(), format="DD/MM/YYYY", key="vz_fim")
    mov = db.listar_movimentacao(ini=ini, fim=fim)
    if mov.empty:
        st.info("Nenhuma venda sincronizada no período.")
        return

    st.subheader("Total vendido por loja")
    resumo = mov.groupby("loja", as_index=False)[["quantidade_saida", "valor_saida"]].sum()
    cols = st.columns(len(resumo))
    for col, (_, row) in zip(cols, resumo.iterrows()):
        col.metric(row["loja"], f"{num(row['quantidade_saida'], 0)} un.", brl(row["valor_saida"]))

    diario = mov.groupby(["data", "loja"], as_index=False)["quantidade_saida"].sum()
    fig = px.bar(diario, x="data", y="quantidade_saida", color="loja", barmode="group",
                 labels={"data": "", "quantidade_saida": "Unidades"})
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=280)
    st.plotly_chart(fig, **LARG)

    st.subheader("Produtos com saldo zerado ou negativo")
    saldos = db.listar_saldos(apenas_ativos=False)
    zerados = saldos[saldos["saldo_atual"] <= 0].sort_values("saldo_atual")
    if zerados.empty:
        st.success("Nenhum produto com saldo zerado ou negativo agora.")
        return
    tabela = zerados[["cod_produto", "descricao", "saldo_inicial", "saldo_atual", "saidas_total", "valor_saidas_total"]]
    st.dataframe(tabela, hide_index=True, **LARG, column_config={
        "cod_produto": "Código", "descricao": "Produto",
        "saldo_inicial": st.column_config.NumberColumn("Saldo na contagem", format="%.0f"),
        "saldo_atual": st.column_config.NumberColumn("Saldo atual", format="%.0f"),
        "saidas_total": st.column_config.NumberColumn("Vendido (total)", format="%.0f"),
        "valor_saidas_total": st.column_config.NumberColumn("Valor vendido", format="R$ %.2f"),
    })
    c_dl1, c_dl2 = st.columns(2)
    c_dl1.download_button("Baixar Excel", para_excel(tabela, "Zerados"), "produtos_zerados.xlsx",
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_zerados")
    c_dl2.download_button(
        "Baixar PDF (para imprimir)",
        relatorios.gerar_pdf_vendas_zerados(ini, fim, resumo, zerados, mov),
        f"vendas_zerados_{ini:%Y%m%d}_{fim:%Y%m%d}.pdf", "application/pdf", key="dl_zerados_pdf",
    )

    st.caption("Detalhe por produto — dia, loja e quantidade de cada venda no período filtrado acima:")
    for _, p in zerados.iterrows():
        det = mov[mov["cod_produto"] == p["cod_produto"]].sort_values("data")
        rotulo = f"{p['cod_produto']} — {p['descricao']} (saldo atual: {num(p['saldo_atual'], 0)})"
        with st.expander(rotulo):
            if det.empty:
                st.caption("Sem vendas no período filtrado (o saldo já estava assim antes do período, ou veio de ajuste).")
            else:
                st.dataframe(det[["data", "loja", "quantidade_saida", "valor_saida"]], hide_index=True, **LARG,
                             column_config={
                                 "data": "Dia", "loja": "Loja",
                                 "quantidade_saida": st.column_config.NumberColumn("Qtd vendida", format="%.0f"),
                                 "valor_saida": st.column_config.NumberColumn("Valor", format="R$ %.2f"),
                             })


# --------------------------------------------------------------------------- negativados
def pagina_negativados() -> None:
    st.header("Produtos negativados")
    st.caption("Só produtos com saldo atual **negativo** (diferente de zerados: saldo = 0 não entra aqui). "
               "Por loja: em quantos dias teve venda desse produto, quantas unidades saíram no total e em "
               "quais dias — geralmente sinal de erro de lançamento, transferência não registrada ou venda "
               "sem saldo suficiente.")
    f1, f2 = st.columns(2)
    ini = f1.date_input("De", value=DATA_INICIAL_SYNC, format="DD/MM/YYYY", key="ng_ini")
    fim = f2.date_input("Até", value=hoje_brasil(), format="DD/MM/YYYY", key="ng_fim")

    saldos = db.listar_saldos(apenas_ativos=False)
    negativados = saldos[saldos["saldo_atual"] < 0].sort_values("saldo_atual")
    if negativados.empty:
        st.success("Nenhum produto com saldo negativo agora.")
        return

    mov = db.listar_movimentacao(ini=ini, fim=fim)
    mov = mov[mov["cod_produto"].isin(negativados["cod_produto"])]

    st.subheader(f"{len(negativados)} produto(s) negativado(s) agora")
    m1, m2 = st.columns(2)
    m1.metric("Unidades vendidas no período", num(float(mov["quantidade_saida"].sum()), 0) if not mov.empty else "0")
    m2.metric("Valor vendido no período", brl(float(mov["valor_saida"].sum())) if not mov.empty else brl(0))

    colunas_resumo = ["cod_produto", "descricao", "loja", "ocorrencias", "quantidade_saida", "valor_saida"]
    if mov.empty:
        st.info("Nenhuma venda desses produtos no período filtrado (o saldo já estava negativo antes do "
                "período, ou veio de um ajuste manual).")
        resumo = pd.DataFrame(columns=colunas_resumo)
    else:
        mapa_desc = dict(zip(negativados["cod_produto"], negativados["descricao"]))
        resumo = (mov.groupby(["cod_produto", "loja"], as_index=False)
                     .agg(ocorrencias=("data", "nunique"), quantidade_saida=("quantidade_saida", "sum"),
                          valor_saida=("valor_saida", "sum")))
        resumo["descricao"] = resumo["cod_produto"].map(mapa_desc)
        resumo = resumo[colunas_resumo].sort_values("quantidade_saida", ascending=False)

        st.subheader("Resumo por produto e loja")
        st.dataframe(resumo, hide_index=True, **LARG, column_config={
            "cod_produto": "Código", "descricao": "Produto", "loja": "Loja",
            "ocorrencias": st.column_config.NumberColumn("Nº de dias com venda", format="%.0f"),
            "quantidade_saida": st.column_config.NumberColumn("Unidades vendidas", format="%.0f"),
            "valor_saida": st.column_config.NumberColumn("Valor vendido", format="R$ %.2f"),
        })
        c1, c2 = st.columns(2)
        c1.download_button("Baixar Excel", para_excel(resumo, "Negativados"), "produtos_negativados.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_neg_xlsx")
        c2.download_button(
            "Baixar PDF (para imprimir)",
            relatorios.gerar_pdf_negativados(ini, fim, negativados, resumo, mov),
            f"produtos_negativados_{ini:%Y%m%d}_{fim:%Y%m%d}.pdf", "application/pdf", key="dl_neg_pdf",
        )

    st.caption("Detalhe por produto — loja, nº de dias com venda e as datas de cada venda no período filtrado acima:")
    for _, p in negativados.iterrows():
        det = mov[mov["cod_produto"] == p["cod_produto"]]
        rotulo = f"{p['cod_produto']} — {p['descricao']} (saldo atual: {num(p['saldo_atual'], 0)})"
        with st.expander(rotulo):
            if det.empty:
                st.caption("Sem vendas no período filtrado.")
                continue
            for loja, g in det.groupby("loja"):
                dias = sorted(g["data"].unique())
                dias_fmt = ", ".join(d.strftime("%d/%m") for d in dias)
                st.markdown(f"**{loja}** — {len(dias)} dia(s) com venda, "
                           f"{num(float(g['quantidade_saida'].sum()), 0)} un., {brl(float(g['valor_saida'].sum()))}")
                st.caption(f"Dias: {dias_fmt}")


# ------------------------------------------------------------------------- não encontrados
def pagina_nao_encontrados() -> None:
    st.header("Vendidos no SGI sem cadastro aqui")
    st.caption("Códigos que apareceram no relatório de DEFENSIVOS e não estão na base. Nada é descartado: as vendas ficam "
               "guardadas e passam a contar sozinhas se você cadastrar o produto (com a contagem da data certa).")
    ne = db.listar_nao_encontrados()
    if ne.empty:
        st.success("Nenhum código pendente.")
    else:
        st.dataframe(ne, hide_index=True, **LARG, column_config={
            "descricao_sgi": "Descrição no SGI", "quantidade_total": st.column_config.NumberColumn("Qtd vendida", format="%.0f"),
            "valor_total": st.column_config.NumberColumn("Valor", format="R$ %.2f"),
            "primeira_venda": "1ª venda", "ultima_venda": "Última venda", "lojas": "Lojas"})
        mapa = dict(zip(ne["cod_produto"], ne["descricao_sgi"]))
        cod = st.selectbox("Tratar o código", list(mapa), format_func=lambda c: _rotulo_produto(c, mapa), key="ne_cod")
        a1, a2 = st.columns(2)
        with a1:
            with st.expander("Adicionar à base", expanded=True):
                _form_adicionar_produto(cod_prefill=cod, desc_prefill=mapa[cod], key=f"ne_{cod}")
        with a2:
            with st.expander("Ignorar (não é defensivo / fora do controle)", expanded=True):
                motivo = st.text_input("Motivo", placeholder="Adjuvante, fertilizante…", key=f"ne_motivo_{cod}")
                if st.button("Ignorar este código", key=f"ne_ign_{cod}"):
                    db.ignorar_codigo(cod, mapa[cod], motivo); limpar_cache(); st.rerun()
    ig = db.listar_ignorados()
    if not ig.empty:
        with st.expander(f"Códigos ignorados ({len(ig)})"):
            st.dataframe(ig.drop(columns=["criado_em"]), hide_index=True, **LARG)
            volta = st.selectbox("Voltar a monitorar", ig["cod_produto"], key="ne_volta")
            if st.button("Remover da lista de ignorados", key="ne_volta_btn"):
                db.desfazer_ignorar(volta); limpar_cache(); st.rerun()


# ---------------------------------------------------------------------------- importar base
def pagina_importar() -> None:
    st.header("Importar base de produtos")
    st.caption("Excel (.xlsx) ou CSV com as colunas **Código**, **Produto** e **Quantidade** "
               "(opcionais: Unidade, Preço de custo, Preço de venda, Lead time, Estoque mínimo, Fornecedor). "
               "Se houver uma aba 'Removidos…', ela alimenta a lista de ignorados.")
    arq = st.file_uploader("Planilha", type=["xlsx", "csv"], key="upload_base")
    if arq is None:
        return
    res = validar_planilha(arq.getvalue(), arq.name)
    if res.aba:
        st.caption(f"Aba lida: **{res.aba}**")
    for e in res.erros:
        st.error(e)
    for a in res.avisos:
        st.warning(a)
    if not res.ok:
        return
    st.success(f"{len(res.df)} produto(s) válidos.")
    st.dataframe(res.df.head(20), hide_index=True, **LARG)

    c1, c2 = st.columns(2)
    dt = c1.date_input("Data da contagem (fim do dia)", value=DATA_CONTAGEM_PADRAO, format="DD/MM/YYYY",
                       help="Vendas até esta data já estão refletidas na contagem e NÃO são descontadas.")
    modo = c2.radio("Se o código já existir", ["Manter o existente", "Atualizar descrição e saldo"], key="imp_modo")
    if modo != "Manter o existente":
        st.warning("Atualizar reescreve o saldo inicial e a data da contagem dos produtos existentes.")
    reg_ign = False
    if not res.ignorados.empty:
        reg_ign = st.checkbox(f"Registrar {len(res.ignorados)} código(s) da aba de removidos como ignorados", value=True)
    if st.button("Importar", type="primary", key="btn_importar"):
        try:
            out = db.importar_produtos(res.df, "novos" if modo == "Manter o existente" else "atualizar", dt)
            if reg_ign:
                for _, r in res.ignorados.iterrows():
                    db.ignorar_codigo(r["cod_produto"], r["descricao"], r["motivo"])
            limpar_cache()
            st.success(f"Inseridos: {out['inseridos']} · Atualizados: {out['atualizados']} · Mantidos: {out['ignorados']}"
                       + (f" · Ignorados registrados: {len(res.ignorados)}" if reg_ign else ""))
        except Exception as e:  # noqa: BLE001
            st.error(f"Falha na importação: {e}")


# ---------------------------------------------------------------------------- pdfs importados
def pagina_pdfs() -> None:
    st.header("PDFs importados do SGI")
    st.caption("Um PDF por loja e dia, salvo exatamente como baixado do SGI no momento da sincronização "
               "(guardado para sempre — passa a valer a partir desta atualização; sincronizações anteriores "
               "não têm PDF salvo).")
    pdfs = db.listar_pdfs()
    if pdfs.empty:
        st.info("Nenhum PDF salvo ainda. Sincronize (de novo, se necessário) para os dias que quiser guardar.")
        return
    f1, f2 = st.columns(2)
    lojas = sorted(pdfs["loja"].unique())
    sel_lojas = f1.multiselect("Loja", lojas, default=lojas, key="pdf_lojas")
    ini = f2.date_input("A partir de", value=DATA_INICIAL_SYNC, format="DD/MM/YYYY", key="pdf_ini")
    d = pdfs[pdfs["loja"].isin(sel_lojas) & (pdfs["data"] >= ini)].sort_values(
        ["data", "loja"], ascending=[False, True])
    st.caption(f"{len(d)} PDF(s) · {d['tamanho'].sum() / 1024:.0f} KB no total.")
    st.dataframe(d, hide_index=True, **LARG, height=380, column_config={
        "loja": "Loja", "data": "Dia",
        "tamanho": st.column_config.NumberColumn("Tamanho (bytes)", format="%d"),
        "criado_em": "Guardado em",
    })
    if d.empty:
        return

    st.subheader("Baixar um PDF")
    opcoes = list(zip(d["loja"], d["data"]))
    escolha = st.selectbox("Loja e dia", opcoes, format_func=lambda o: f"{o[0]} — {o[1]:%d/%m/%Y}", key="pdf_escolha")
    if escolha:
        loja_sel, dia_sel = escolha
        pdf_bytes = db.obter_pdf(loja_sel, dia_sel)
        if pdf_bytes:
            st.download_button(
                "Baixar PDF", pdf_bytes, file_name=f"{loja_sel.replace(' ', '_')}_{dia_sel:%Y%m%d}.pdf",
                mime="application/pdf", key=f"dl_pdf_{loja_sel}_{dia_sel}")
        else:
            st.warning("PDF não encontrado (pode ter sido removido).")


# ---------------------------------------------------------------------------- sincronização
def pagina_sincronizacao() -> None:
    st.header("Sincronização com o SGI")
    hoje = hoje_brasil()
    sd = db.listar_sync_dias(limite=400)

    st.subheader("Situação por loja")
    cols = st.columns(len(sync_core.LOJAS))
    for col, loja in zip(cols, sync_core.LOJAS):
        feitos = db.dias_sincronizados(loja)
        pend = sync_core.dias_a_sincronizar(DATA_INICIAL_SYNC, hoje, feitos, janela=0)
        ultimo = max(feitos) if feitos else None
        col.metric(loja, f"até {ultimo:%d/%m}" if ultimo else "nunca sincronizou")
        col.caption(f"⚠ {len(pend)} dia(s) pendente(s)" if pend else "Em dia")

    st.subheader("Sincronizar agora")
    st.caption("Dispara o workflow no GitHub Actions (leva 2–5 min) e acompanha até terminar. "
               "Além disso o sistema roda sozinho às 12h e 19h.")
    s1, s2, s3 = st.columns(3)
    loja = s1.selectbox("Loja", ["(todas)"] + list(sync_core.LOJAS))
    dia = s2.date_input("Só este dia (opcional)", value=None, format="DD/MM/YYYY")
    forcar = s3.checkbox("Rebuscar tudo desde 22/09", help="Ignora o histórico de dias já sincronizados.")
    if st.button("Sincronizar agora", type="primary", key="btn_sync"):
        try:
            disparado = github_actions.disparar_sincronizacao(
                None if loja == "(todas)" else loja, dia.strftime("%d/%m/%Y") if dia else None, forcar)
            with st.status("Sincronizando…", expanded=True) as status:
                ok, url = github_actions.aguardar_conclusao(disparado, callback_status=lambda t: status.write(t))
                if ok is True:
                    status.update(label="Sincronização concluída.", state="complete")
                elif ok is False:
                    status.update(label="A sincronização terminou com erro.", state="error")
                    st.error(f"Veja os logs e os arquivos de depuração: {url}")
                else:
                    status.update(label="Ainda rodando; acompanhe no GitHub.", state="running")
                    st.info(f"Não deu tempo de confirmar. Acompanhe: {url}")
            limpar_cache()
        except github_actions.SincronizacaoIndisponivel as e:
            st.error(str(e))

    st.subheader("Dias sincronizados")
    if sd.empty:
        st.info("Nenhum dia sincronizado ainda.")
    else:
        pv = sd.pivot_table(index="data", columns="loja", values="valor_total", aggfunc="sum").sort_index(ascending=False)
        st.dataframe(pv, **LARG, column_config={
            c: st.column_config.NumberColumn(c, format="R$ %.2f") for c in pv.columns})
    ex = db.ultimas_execucoes(10)
    if not ex.empty:
        with st.expander("Últimas execuções"):
            st.dataframe(ex, hide_index=True, **LARG)


PAGINAS = {
    "Visão geral": pagina_visao_geral,
    "Saldo por produto": pagina_saldo,
    "Produtos": pagina_produtos,
    "Movimentar estoque": pagina_movimentar,
    "Entrada por nota fiscal": pagina_entrada_nfe,
    "Indicadores": pagina_indicadores,
    "Histórico de saídas": pagina_historico,
    "Vendas e zerados": pagina_vendas_zerados,
    "Negativados": pagina_negativados,
    "Não encontrados": pagina_nao_encontrados,
    "Importar base": pagina_importar,
    "PDFs importados": pagina_pdfs,
    "Sincronização": pagina_sincronizacao,
}
