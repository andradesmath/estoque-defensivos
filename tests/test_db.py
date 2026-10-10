"""Integração com Postgres real (TEST_DATABASE_URL). Foco: idempotência e regras do saldo."""
from datetime import date
from decimal import Decimal

import pytest

from estoque.db import ProdutoDuplicado, ZerarDiaNaoPermitido

D0 = date(2026, 9, 21)
D22, D23 = date(2026, 9, 22), date(2026, 9, 23)


def _linhas(**qtds):
    return [{"cod_produto": c, "descricao": f"P{c}", "quantidade_saida": Decimal(q), "valor_saida": Decimal(q) * 10}
            for c, q in qtds.items()]


def _saldo(db, cod):
    return float(db.obter_produto(cod)["saldo_atual"])


@pytest.fixture
def base(banco):
    banco.criar_produto("00001", "PRODUTO 1", 100, D0)
    banco.criar_produto("00002", "PRODUTO 2", 50, D0)
    return banco


def test_sync_repetido_nao_desconta_duas_vezes(base):
    for _ in range(5):  # janela sobreposta rodando várias vezes
        base.substituir_movimentacao_dia("Porteira", D22, _linhas(**{"00001": 10}))
    assert _saldo(base, "00001") == 90


def test_upsert_sobrescreve_quantidade_do_dia(base):
    base.substituir_movimentacao_dia("Porteira", D22, _linhas(**{"00001": 10}))
    base.substituir_movimentacao_dia("Porteira", D22, _linhas(**{"00001": 14}))  # correção no SGI
    assert _saldo(base, "00001") == 86


def test_codigo_que_sumiu_do_relatorio_e_removido(base):
    base.substituir_movimentacao_dia("Porteira", D22, _linhas(**{"00001": 10, "00002": 5}))
    r = base.substituir_movimentacao_dia("Porteira", D22, _linhas(**{"00001": 10}))  # venda de 00002 cancelada
    assert r["removidos"] == 1 and _saldo(base, "00002") == 50


def test_lojas_somam_e_nao_se_sobrescrevem(base):
    base.substituir_movimentacao_dia("Porteira", D22, _linhas(**{"00001": 10}))
    base.substituir_movimentacao_dia("Casa de Adubo", D22, _linhas(**{"00001": 3}))
    assert _saldo(base, "00001") == 87
    base.substituir_movimentacao_dia("Porteira", D22, _linhas(**{"00001": 10}))
    assert _saldo(base, "00001") == 87  # reprocessar uma loja não mexe na outra


def test_reprocessar_uma_loja_nao_apaga_a_outra(base):
    base.substituir_movimentacao_dia("Casa de Adubo", D22, _linhas(**{"00001": 3}))
    base.substituir_movimentacao_dia("Porteira", D22, [], permitir_zerar=True)
    assert _saldo(base, "00001") == 97


def test_relatorio_vazio_nao_apaga_dia_com_vendas(base):
    base.substituir_movimentacao_dia("Porteira", D22, _linhas(**{"00001": 10}))
    with pytest.raises(ZerarDiaNaoPermitido):
        base.substituir_movimentacao_dia("Porteira", D22, [])
    assert _saldo(base, "00001") == 90
    base.substituir_movimentacao_dia("Porteira", D22, [], permitir_zerar=True)  # confirmação explícita
    assert _saldo(base, "00001") == 100


def test_dia_vazio_sem_dados_previos_e_registrado(base):
    base.substituir_movimentacao_dia("Porteira", D23, [])
    assert D23 in base.dias_sincronizados("Porteira")


def test_linhas_duplicadas_sao_recusadas(base):
    with pytest.raises(ValueError):
        base.substituir_movimentacao_dia("Porteira", D22, _linhas(**{"00001": 1}) * 2)


def test_venda_na_data_da_contagem_ou_antes_e_ignorada(base):
    base.substituir_movimentacao_dia("Porteira", D0, _linhas(**{"00001": 99}))
    assert _saldo(base, "00001") == 100


def test_transacao_atomica_em_falha(base):
    linhas = _linhas(**{"00001": 10})
    linhas.append({"cod_produto": "00002", "descricao": "x", "quantidade_saida": None, "valor_saida": Decimal(0)})
    with pytest.raises(Exception):
        base.substituir_movimentacao_dia("Porteira", D22, linhas)
    assert _saldo(base, "00001") == 100 and D22 not in base.dias_sincronizados("Porteira")


def test_nao_encontrados_e_ignorados(base):
    base.substituir_movimentacao_dia("Porteira", D22, _linhas(**{"00001": 1, "09999": 4, "08888": 2}))
    ne = base.listar_nao_encontrados().set_index("cod_produto")
    assert set(ne.index) == {"09999", "08888"} and ne.loc["09999", "quantidade_total"] == 4
    base.ignorar_codigo("08888", "ADJUVANTE X", "Não é defensivo")
    assert set(base.listar_nao_encontrados()["cod_produto"]) == {"09999"}
    base.desfazer_ignorar("08888")
    assert set(base.listar_nao_encontrados()["cod_produto"]) == {"09999", "08888"}


def test_cadastrar_produto_depois_conta_vendas_passadas(base):
    base.substituir_movimentacao_dia("Porteira", D22, _linhas(**{"09999": 4}))
    base.criar_produto("09999", "NOVO", 20, D0)
    assert _saldo(base, "09999") == 16
    assert base.listar_nao_encontrados().empty


def test_crud_produto(base):
    with pytest.raises(ProdutoDuplicado):
        base.criar_produto("1", "DUP", 1, D0)  # '1' normaliza para 00001
    base.atualizar_produto("00001", preco_custo="12,50", preco_venda=20, lead_time_dias="7", estoque_minimo=15)
    p = base.obter_produto("00001")
    assert p["preco_custo"] == 12.5 and p["lead_time_dias"] == 7 and p["estoque_minimo"] == 15
    with pytest.raises(ValueError):
        base.atualizar_produto("00001", saldo_atual=1)  # campo calculado não é editável
    base.definir_ativo("00001", False)
    assert "00001" not in set(base.listar_saldos()["cod_produto"])
    assert "00001" in set(base.listar_saldos(apenas_ativos=False)["cod_produto"])
    base.excluir_produto("00002")
    assert base.obter_produto("00002") is None


def test_ajustes_e_sinais(base):
    base.registrar_ajuste("00001", D22, "entrada", 30, custo_unitario=11, atualizar_custo=True)
    base.registrar_ajuste("00001", D22, "transferencia_saida", 8, observacao="Piatã")
    base.registrar_ajuste("00001", D23, "perda", 2)
    assert _saldo(base, "00001") == 100 + 30 - 8 - 2
    assert base.obter_produto("00001")["preco_custo"] == 11
    with pytest.raises(ValueError):
        base.registrar_ajuste("00001", D22, "perda", -1)  # sinal vem do tipo
    with pytest.raises(ValueError):
        base.registrar_ajuste("00001", D0, "entrada", 5)  # data <= contagem seria ignorada
    aj = base.listar_ajustes("00001")
    assert len(aj) == 3 and set(aj["tipo"]) == {"entrada", "transferencia_saida", "perda"}
    base.excluir_ajuste(int(aj.iloc[0]["id"]))
    assert len(base.listar_ajustes("00001")) == 2


def test_definir_saldo_por_contagem_grava_diferenca(base):
    base.substituir_movimentacao_dia("Porteira", D22, _linhas(**{"00001": 10}))
    delta = base.definir_saldo_por_contagem("00001", 85, D23)  # sistema diz 90, contou 85
    assert delta == Decimal(-5) and _saldo(base, "00001") == 85
    assert base.definir_saldo_por_contagem("00001", 85, D23) == 0  # nada a fazer
    assert len(base.listar_ajustes("00001")) == 1


def test_saldo_em_data_passada(base):
    base.substituir_movimentacao_dia("Porteira", D22, _linhas(**{"00001": 10}))
    base.substituir_movimentacao_dia("Porteira", D23, _linhas(**{"00001": 5}))
    assert base.saldo_em("00001", D22) == 90 and base.saldo_em("00001", D23) == 85


def test_importar_modos(banco, base_xlsx_bytes):
    from estoque.importacao import validar_planilha
    r = validar_planilha(base_xlsx_bytes, "b.xlsx")
    assert banco.importar_produtos(r.df, "novos", D0) == {"inseridos": 227, "atualizados": 0, "ignorados": 0}
    assert banco.importar_produtos(r.df, "novos", D0) == {"inseridos": 0, "atualizados": 0, "ignorados": 227}
    banco.atualizar_produto("00004", preco_custo=9.9, saldo_inicial=1)
    df2 = r.df.copy()
    df2.loc[df2["cod_produto"] == "00004", "saldo_inicial"] = Decimal(30)
    out = banco.importar_produtos(df2, "atualizar", D0)
    assert out["atualizados"] == 227 and out["inseridos"] == 0
    p = banco.obter_produto("00004")
    assert p["saldo_inicial"] == 30 and p["preco_custo"] == 9.9  # preço preservado (planilha sem coluna de preço)
    with pytest.raises(ValueError):
        banco.importar_produtos(r.df, "outro", D0)


def test_schema_idempotente(banco):
    banco.init_schema(); banco.init_schema()


def test_salvar_e_obter_pdf(banco):
    conteudo = b"%PDF-1.4 conteudo de teste"
    banco.salvar_pdf("Porteira", D22, conteudo)
    assert banco.obter_pdf("Porteira", D22) == conteudo
    assert banco.obter_pdf("Casa de Adubo", D22) is None  # loja diferente, nada salvo

    lst = banco.listar_pdfs()
    assert len(lst) == 1
    assert lst.iloc[0]["loja"] == "Porteira" and lst.iloc[0]["tamanho"] == len(conteudo)

    # sobrescreve no mesmo (loja, dia) em vez de duplicar
    novo = b"%PDF-1.4 versao atualizada, mais bytes"
    banco.salvar_pdf("Porteira", D22, novo)
    assert banco.obter_pdf("Porteira", D22) == novo
    assert len(banco.listar_pdfs()) == 1


def test_mapa_fornecedor_produto(base):
    cnpj = "14.644.182/0004-90"
    assert base.buscar_mapa_fornecedor(cnpj) == {}
    base.salvar_mapa_fornecedor(cnpj, "0139857", "00001", "APPROVE - 12X1")
    assert base.buscar_mapa_fornecedor(cnpj) == {"0139857": "00001"}
    # reassociar o mesmo código do fornecedor a outro produto atualiza, não duplica
    base.salvar_mapa_fornecedor(cnpj, "0139857", "00002", "APPROVE - 12X1")
    assert base.buscar_mapa_fornecedor(cnpj) == {"0139857": "00002"}
    # CNPJ diferente não enxerga o mapa de outro fornecedor
    assert base.buscar_mapa_fornecedor("99.999.999/0001-99") == {}


def test_comandos_do_schema_separa_so_o_que_importa():
    """garantir_schema_transferencias() depende deste recorte: o robô local usa pg8000,
    que não aceita o schema.sql inteiro numa tacada (psycopg2 aceita). Se o recorte
    pegar comando a mais, a execução do robô mexe em tabela que não é dela; a menos, a
    gravação quebra com 'relation does not exist' depois de todo o trabalho feito."""
    from estoque.db import _comandos_do_schema

    cmds = _comandos_do_schema("movimentacao_transferencia", "sync_execucoes")
    assert cmds[0].startswith("CREATE TABLE IF NOT EXISTS movimentacao_transferencia")
    # A view precisa vir DEPOIS da tabela que ela consulta.
    assert cmds[-1].startswith("CREATE OR REPLACE VIEW v_saldo_produto")
    assert all(c.rstrip().endswith(";") for c in cmds)
    # Nenhum comando pode ser só comentário solto.
    assert not any(c.lstrip().startswith("--") for c in cmds)
    # Todo comando precisa ser idempotente: o robô roda isto em TODA execução.
    assert all(("IF NOT EXISTS" in c) or c.startswith("CREATE OR REPLACE") for c in cmds)
    # As colunas novas de sync_execucoes entram por ALTER, não no CREATE: a tabela já
    # existe em produção e CREATE TABLE IF NOT EXISTS não altera tabela existente.
    alters = [c for c in cmds if c.startswith("ALTER TABLE sync_execucoes")]
    assert {"periodo_inicio", "periodo_fim", "dias", "linhas"} == {
        c.split("IF NOT EXISTS")[1].split()[0] for c in alters}


def test_uma_so_definicao_de_cada_tabela_no_schema():
    """Pegou um bug real: eu criei uma segunda tabela sync_execucoes sem ver que ela já
    existia (com outras colunas), e em produção o CREATE IF NOT EXISTS seria um no-op
    silencioso - o INSERT do robô é que quebraria, com a tabela 'existindo'."""
    import re
    from pathlib import Path

    from estoque.db import SCHEMA_PATH

    nomes = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)",
                       Path(SCHEMA_PATH).read_text(encoding="utf-8"))
    repetidas = {n for n in nomes if nomes.count(n) > 1}
    assert not repetidas, f"tabelas definidas mais de uma vez no schema.sql: {repetidas}"
