"""
estoque/db.py - Acesso ao PostgreSQL (SQLAlchemy Core + SQL explícito).

Convenções:
  - `get_engine()` é chamado a cada função (não capturado no import) para os testes
    poderem trocá-lo por monkeypatch.
  - Saldo nunca é gravado: sempre lido de v_saldo_produto (ver schema.sql).
  - Toda escrita em movimentacao_saida passa por `substituir_movimentacao_dia`, que
    é idempotente (upsert + remoção de códigos que sumiram do relatório do dia).
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

try:
    import pandas as pd
except ImportError:  # os robôs do SGI desktop (scripts/sync_compras_sgi.py e
    # scripts/sync_transferencias_sgi.py) rodam em Python 32-bit, onde pandas não tem
    # build pronta (ver estoque/parser_compras_sgi.py). O resto do módulo
    # (painel/GitHub Actions, com pandas instalado) continua normal.
    #
    # CONSEQUÊNCIA PRÁTICA, que já me pegou: função que passe por _df() devolve
    # DataFrame e só funciona lá. Toda função chamada por robô tem que devolver
    # list[dict] - é por isso que existem possiveis_duplicatas_* e
    # ultimas_execucoes_lista() em vez de reaproveitar as versões em DataFrame.
    pd = None
from sqlalchemy import bindparam, create_engine, text

from .util import normalizar_cod, para_decimal, para_int, texto_ou_none

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

TIPOS_AJUSTE = {
    # tipo: (sinal, rótulo)   sinal 0 = livre (correção de contagem)
    "entrada": (1, "Entrada (compra)"),
    "transferencia_entrada": (1, "Transferência recebida"),
    "transferencia_saida": (-1, "Transferência enviada (ex.: Piatã)"),
    "perda": (-1, "Perda / avaria / vencimento"),
    "correcao": (0, "Correção de contagem"),
}

CAMPOS_EDITAVEIS = {
    "descricao", "unidade", "saldo_inicial", "data_saldo_inicial", "preco_custo",
    "preco_venda", "lead_time_dias", "estoque_minimo", "fornecedor", "observacao", "ativo",
}


class ProdutoDuplicado(ValueError):
    pass


class ZerarDiaNaoPermitido(Exception):
    """Relatório vazio para um (loja, dia) que já tinha vendas gravadas."""


# --------------------------------------------------------------------------- engine
def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        try:
            import streamlit as st
            url = st.secrets.get("DATABASE_URL")
        except Exception:
            url = None
    if not url:
        raise RuntimeError(
            "DATABASE_URL não configurada (variável de ambiente, .env ou st.secrets)."
        )
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    # Força o driver psycopg2 (instalado via requirements). Sem isso, a escolha do
    # driver padrão do SQLAlchemy para "postgresql://" varia por ambiente/versão
    # (viu-se "postgresql://" resolver para psycopg v3 no GitHub Actions, não
    # instalado, e para psycopg2 localmente) — explicitar remove a ambiguidade.
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


@lru_cache(maxsize=1)
def get_engine():
    url = _database_url()
    kwargs = {"pool_pre_ping": True, "pool_recycle": 300}
    if url.startswith("postgresql+pg8000://"):
        # pg8000 (robô local, Python 32-bit - ver parser_compras_sgi.py) não entende os
        # parâmetros de query do psycopg2/libpq (sslmode, channel_binding) nem converte
        # sozinho "ssl_context=true" da URL num SSLContext de verdade (gera
        # "'str' object has no attribute 'wrap_socket'"). Monta o SSLContext aqui e tira
        # esses parâmetros da URL - Neon exige TLS, então SEM isso a conexão cai sem TLS
        # (ou nem conecta, dependendo da query string deixada no .env).
        import ssl
        from urllib.parse import urlsplit, urlunsplit

        partes = urlsplit(url)
        url = urlunsplit((partes.scheme, partes.netloc, partes.path, "", partes.fragment))
        kwargs["connect_args"] = {"ssl_context": ssl.create_default_context()}
    return create_engine(url, **kwargs)


def init_schema() -> None:
    """Executa schema.sql inteiro (vários comandos) numa única transação, pela conexão
    DBAPI crua: psycopg2 aceita script multi-comando e, sem parâmetros, não interpreta
    '%'."""
    script = SCHEMA_PATH.read_text(encoding="utf-8")
    raw = get_engine().raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(script)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def _comandos_do_schema(*termos: str) -> list[str]:
    """Comandos do schema.sql que citam qualquer um dos `termos`, um a um, na ordem do
    arquivo (as views vêm depois das tabelas que consultam, e isso importa).

    Existe porque init_schema() manda o arquivo INTEIRO numa tacada, o que o psycopg2
    aceita e o pg8000 não (o robô local usa pg8000 - ver
    requirements-local-robo-compras.txt).

    Os COMENTÁRIOS saem ANTES de dividir por ';', e não depois: um ';' dentro de
    comentário ("Hoje é sempre a matriz; a coluna existe para...") partia o texto no
    meio, e o pedaço de frase que sobrava - já sem o '--' na frente - passava por
    comando. O teste pegou isso. Dividir por ';' só é suficiente porque o schema não
    tem função/trigger com ';' dentro de corpo citado; '--' dentro de string literal
    também não existe aqui."""
    bruto = SCHEMA_PATH.read_text(encoding="utf-8")
    script = "\n".join(l.split("--", 1)[0] for l in bruto.splitlines())
    comandos = []
    for pedaco in script.split(";"):
        corpo = pedaco.strip()
        if corpo and any(t in corpo for t in termos):
            comandos.append(corpo + ";")
    return comandos


def aplicar_schema_por_partes() -> int:
    """Roda o schema.sql INTEIRO, um comando por vez, e devolve quantos rodaram.

    Diferente de init_schema(), que manda o arquivo todo numa tacada só: aqui cada
    comando vai sozinho, o que (a) funciona no pg8000 dos robôs e (b) serve de
    AUTOCURA no painel quando o banco está atrás do código. Isso aconteceu de verdade:
    _preparar_banco() é @st.cache_resource, então num recarregamento de código sem
    reinício de processo o init_schema NÃO roda de novo - o painel subiu com as
    unidades e o banco ainda sem a tabela `unidades`.

    Todos os comandos do schema são idempotentes (IF NOT EXISTS / OR REPLACE / DROP+
    CREATE de view), então repetir é seguro."""
    comandos = _comandos_do_schema("")  # "" casa com todos
    for cmd in comandos:
        with get_engine().begin() as conn:
            conn.execute(text(cmd))
    return len(comandos)


def garantir_schema_robos() -> list[str]:
    """Cria o que os robôs do SGI desktop precisam, se ainda não existir: a tabela
    movimentacao_transferencia (+ índice), a versão de v_saldo_produto que desconta as
    transferências do saldo, e a tabela sync_execucoes do histórico de execuções.

    POR QUE existe: o schema.sql é a fonte da verdade, mas ninguém roda migração neste
    projeto - as tabelas de produção nasceram do painel chamando init_schema(). A
    tabela nova só apareceria lá quando o painel subisse com o código novo, e até lá o
    robô quebrava com 'relation "movimentacao_transferencia" does not exist' DEPOIS de
    ter feito todo o trabalho de exportar (aconteceu em 09/10/2026). Rodar os comandos
    aqui, antes de gravar, resolve de um jeito idempotente: todos são
    IF NOT EXISTS / OR REPLACE.

    Devolve a lista de comandos executados (para o robô dizer o que fez)."""
    # "v_saldo_produto" entra na lista porque as views agora sao DROP+CREATE: sem o
    # termo, o recorte pegaria o CREATE da view por unidade mas nao os DROPs, e o
    # CREATE falharia com "ja existe" ou com dependencia da consolidada.
    comandos = _comandos_do_schema("movimentacao_transferencia", "sync_execucoes",
                                   "v_saldo_produto")
    if not comandos:
        raise RuntimeError("Não achei no schema.sql os comandos dos robôs "
                           "(movimentacao_transferencia / sync_execucoes).")
    for cmd in comandos:
        # Uma transação por comando: no Postgres, erro aborta a transação inteira - se
        # a view falhasse junto com a tabela, a tabela recém-criada também seria
        # desfeita.
        try:
            with get_engine().begin() as conn:
                conn.execute(text(cmd))
        except Exception:  # noqa: BLE001
            # CREATE OR REPLACE VIEW só aceita acrescentar colunas NO FIM. Se a view em
            # produção tiver nascido com outra ordem/nome de colunas, ele recusa. Como
            # nada depende de v_saldo_produto (conferido no schema.sql), recriar do
            # zero é seguro - e é a única saída sem editar o banco na mão.
            if "CREATE OR REPLACE VIEW v_saldo_produto" not in cmd:
                raise
            with get_engine().begin() as conn:
                conn.execute(text("DROP VIEW IF EXISTS v_saldo_produto"))
                conn.execute(text(cmd))
    return [c.split("\n", 1)[0].strip() for c in comandos]


def registrar_execucao_robo(origem: str, periodo_inicio: date | None, periodo_fim: date | None,
                            status: str, dias: int | None = None, linhas: int | None = None,
                            resumo: str | None = None) -> None:
    """Anota, de uma vez, uma execução já terminada de robô em sync_execucoes.

    Usa a MESMA tabela do sync de vendas (iniciar_execucao/finalizar_execucao, que são
    em dois tempos porque aquele roda no GitHub Actions e pode morrer no meio). Aqui o
    robô é local e curto: registrar no fim, numa linha só, evita deixar execução
    'rodando' pendurada quando alguém fecha o terminal. De brinde, o histórico dos robôs
    aparece no painel junto com o das vendas, que já lê esta tabela.

    Nunca derruba o robô: histórico é registro, não parte da sincronização - se o banco
    recusar a anotação, o que foi gravado continua gravado e válido."""
    try:
        with get_engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO sync_execucoes
                    (origem, finalizado_em, status, resumo, periodo_inicio, periodo_fim, dias, linhas)
                VALUES (:o, now(), :st, :r, :ini, :fim, :dias, :linhas)
            """), {"o": origem[:30], "st": status, "r": (resumo or "")[:4000],
                   "ini": periodo_inicio, "fim": periodo_fim, "dias": dias, "linhas": linhas})
    except Exception as e:  # noqa: BLE001
        print(f"  (não deu pra registrar a execução no histórico: {e})")


def ultimas_execucoes_lista(limite: int = 20, origem: str | None = None,
                            prefixo_origem: str | None = None) -> list[dict]:
    """Igual a ultimas_execucoes(), mas devolve list[dict] em vez de DataFrame.

    Existe porque os robôs rodam no Python de 32 bits, onde NÃO há pandas (ver o
    try/except do import no topo deste módulo): qualquer função daqui que passe por
    _df() quebra com ModuleNotFoundError quando chamada pelo robô. Aconteceu de verdade
    em 09/10/2026 com `--historico`. As funções que os robôs usam têm que falar
    list[dict], como possiveis_duplicatas_*.

    `prefixo_origem` ('robo-%') separa as execuções dos robôs do SGI das do sync de
    vendas, que roda 2x por dia pelo GitHub Actions e, sem filtro, ocupa a lista
    inteira: 20 linhas de histórico viram 10 dias só de vendas."""
    cond, params = ["TRUE"], {"l": limite}
    if origem:
        cond.append("origem = :o"); params["o"] = origem
    if prefixo_origem:
        cond.append("origem LIKE :pre"); params["pre"] = prefixo_origem
    with get_engine().begin() as conn:
        linhas = conn.execute(text(f"""
            SELECT id, iniciado_em, finalizado_em, origem, status,
                   periodo_inicio, periodo_fim, dias, linhas, resumo
            FROM sync_execucoes WHERE {' AND '.join(cond)}
            ORDER BY id DESC LIMIT :l"""), params).mappings().all()
    return [dict(l) for l in linhas]


def ultimo_periodo_coberto(origem: str) -> date | None:
    """Maior `periodo_fim` já coberto com sucesso por esse robô, ou None.

    É o MÁXIMO, e não a execução mais recente, de propósito: se alguém rodar hoje com
    --ate numa data antiga para refazer um trecho, a execução recente cobriria menos
    que a anterior, e pegar "a última" faria o robô recomeçar lá atrás na vez seguinte."""
    with get_engine().begin() as conn:
        return conn.execute(text("""SELECT MAX(periodo_fim) FROM sync_execucoes
                                    WHERE origem = :o AND status = 'ok'"""),
                            {"o": origem}).scalar()


def _df(sql: str, **params) -> pd.DataFrame:
    with get_engine().connect() as conn:
        df = pd.read_sql_query(text(sql), conn, params=params)
    for col in df.columns:
        if df[col].dtype == object and df[col].map(lambda v: isinstance(v, Decimal)).any():
            df[col] = df[col].astype(float)
    return df


# ------------------------------------------------------------------------- produtos
def listar_saldos(apenas_ativos: bool = True) -> pd.DataFrame:
    filtro = "WHERE ativo" if apenas_ativos else ""
    return _df(f"SELECT * FROM v_saldo_produto {filtro} ORDER BY descricao")


def obter_produto(cod: str) -> dict | None:
    df = _df("SELECT * FROM v_saldo_produto WHERE cod_produto = :c", c=normalizar_cod(cod))
    return None if df.empty else df.iloc[0].to_dict()


def _limpar_campos(campos: dict) -> dict:
    out = {}
    for k, v in campos.items():
        if k in ("saldo_inicial", "estoque_minimo"):
            out[k] = para_decimal(v)
        elif k in ("preco_custo", "preco_venda"):
            out[k] = para_decimal(v)
        elif k == "lead_time_dias":
            out[k] = para_int(v)
        elif k in ("descricao", "unidade", "fornecedor", "observacao"):
            out[k] = texto_ou_none(v)
        elif k == "ativo":
            out[k] = bool(v)
        else:
            out[k] = v
    return out


def criar_produto(
    cod_produto, descricao, saldo_inicial, data_saldo_inicial: date, unidade=None,
    preco_custo=None, preco_venda=None, lead_time_dias=None, estoque_minimo=None,
    fornecedor=None, observacao=None,
) -> str:
    cod = normalizar_cod(cod_produto)
    if cod is None:
        raise ValueError("Código inválido: use apenas dígitos (ex.: 00004).")
    desc = texto_ou_none(descricao)
    if not desc:
        raise ValueError("Descrição é obrigatória.")
    saldo = para_decimal(saldo_inicial)
    if saldo is None:
        raise ValueError("Saldo inicial inválido.")
    campos = _limpar_campos({
        "unidade": unidade, "preco_custo": preco_custo, "preco_venda": preco_venda,
        "lead_time_dias": lead_time_dias, "estoque_minimo": estoque_minimo,
        "fornecedor": fornecedor, "observacao": observacao,
    })
    with get_engine().begin() as conn:
        existe = conn.execute(text("SELECT 1 FROM produtos WHERE cod_produto = :c"), {"c": cod}).first()
        if existe:
            raise ProdutoDuplicado(f"Já existe um produto com o código {cod}.")
        conn.execute(text("""
            INSERT INTO produtos (cod_produto, descricao, unidade, saldo_inicial, data_saldo_inicial,
                                  preco_custo, preco_venda, lead_time_dias, estoque_minimo,
                                  fornecedor, observacao)
            VALUES (:cod, :descricao, :unidade, :saldo, :dt, :preco_custo, :preco_venda,
                    :lead_time_dias, :estoque_minimo, :fornecedor, :observacao)
        """), {"cod": cod, "descricao": desc, "saldo": saldo, "dt": data_saldo_inicial, **campos})
    return cod


def atualizar_produto(cod: str, **campos) -> None:
    invalidos = set(campos) - CAMPOS_EDITAVEIS
    if invalidos:
        raise ValueError(f"Campos não editáveis: {sorted(invalidos)}")
    if not campos:
        return
    limpos = _limpar_campos(campos)
    if "descricao" in limpos and not limpos["descricao"]:
        raise ValueError("Descrição não pode ficar vazia.")
    if "saldo_inicial" in limpos and limpos["saldo_inicial"] is None:
        raise ValueError("Saldo inicial não pode ficar vazio.")
    # nomes de coluna vêm do whitelist CAMPOS_EDITAVEIS; valores sempre por parâmetro.
    sets = ", ".join(f"{k} = :{k}" for k in limpos)
    with get_engine().begin() as conn:
        r = conn.execute(
            text(f"UPDATE produtos SET {sets}, atualizado_em = now() WHERE cod_produto = :__cod"),
            {**limpos, "__cod": normalizar_cod(cod)},
        )
        if r.rowcount == 0:
            raise ValueError(f"Produto {cod} não encontrado.")


def atualizar_produtos_em_lote(registros: list[dict]) -> int:
    """Cada registro: {'cod_produto': ..., campo: valor, ...}. Usado pelo editor em
    tabela do painel (preço, mínimo, lead time)."""
    n = 0
    for reg in registros:
        reg = dict(reg)
        cod = reg.pop("cod_produto")
        if reg:
            atualizar_produto(cod, **reg)
            n += 1
    return n


def definir_ativo(cod: str, ativo: bool) -> None:
    atualizar_produto(cod, ativo=ativo)


def excluir_produto(cod: str) -> None:
    """Exclusão definitiva. Apaga ajustes manuais (CASCADE). As vendas do SGI ficam em
    movimentacao_saida e o código reaparece em 'não encontrados'. Prefira inativar."""
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM produtos WHERE cod_produto = :c"), {"c": normalizar_cod(cod)})


def importar_produtos(df: pd.DataFrame, modo: str, data_saldo: date) -> dict:
    """df já validado por importacao.validar_planilha (colunas cod_produto, descricao,
    saldo_inicial e opcionais). modo:
      'novos'     - só insere códigos inexistentes; existentes ficam intactos;
      'atualizar' - insere e, nos existentes, sobrescreve descrição, saldo_inicial e
                    data_saldo_inicial (preços/parâmetros só mudam se vierem preenchidos).
    Retorna {'inseridos', 'atualizados', 'ignorados'}."""
    if modo not in ("novos", "atualizar"):
        raise ValueError("modo deve ser 'novos' ou 'atualizar'.")
    opcionais = ["unidade", "preco_custo", "preco_venda", "lead_time_dias", "estoque_minimo", "fornecedor"]
    linhas = []
    for _, r in df.iterrows():
        linhas.append({
            "cod": normalizar_cod(r["cod_produto"]),
            "descricao": texto_ou_none(r["descricao"]),
            "saldo": para_decimal(r["saldo_inicial"]),
            "dt": data_saldo,
            **{c: (_limpar_campos({c: r[c]})[c] if c in df.columns else None) for c in opcionais},
        })
    if modo == "novos":
        conflito = "ON CONFLICT (cod_produto) DO NOTHING"
    else:
        conflito = """ON CONFLICT (cod_produto) DO UPDATE SET
            descricao = EXCLUDED.descricao,
            saldo_inicial = EXCLUDED.saldo_inicial,
            data_saldo_inicial = EXCLUDED.data_saldo_inicial,
            unidade = COALESCE(EXCLUDED.unidade, produtos.unidade),
            preco_custo = COALESCE(EXCLUDED.preco_custo, produtos.preco_custo),
            preco_venda = COALESCE(EXCLUDED.preco_venda, produtos.preco_venda),
            lead_time_dias = COALESCE(EXCLUDED.lead_time_dias, produtos.lead_time_dias),
            estoque_minimo = COALESCE(EXCLUDED.estoque_minimo, produtos.estoque_minimo),
            fornecedor = COALESCE(EXCLUDED.fornecedor, produtos.fornecedor),
            atualizado_em = now()"""
    sql = text(f"""
        INSERT INTO produtos (cod_produto, descricao, unidade, saldo_inicial, data_saldo_inicial,
                              preco_custo, preco_venda, lead_time_dias, estoque_minimo, fornecedor)
        VALUES (:cod, :descricao, :unidade, :saldo, :dt, :preco_custo, :preco_venda,
                :lead_time_dias, :estoque_minimo, :fornecedor)
        {conflito}
        RETURNING (xmax = 0) AS inserido
    """)
    ins = upd = 0
    with get_engine().begin() as conn:
        for linha in linhas:
            row = conn.execute(sql, linha).first()
            if row is None:
                continue
            if row[0]:
                ins += 1
            else:
                upd += 1
    return {"inseridos": ins, "atualizados": upd, "ignorados": len(linhas) - ins - upd}


# ------------------------------------------------------------------------- ajustes
def saldo_em(cod: str, dia: date) -> Decimal:
    """Saldo ao FIM de `dia` (mesma regra da view, cortada em `dia`)."""
    df = _df("""
        SELECT p.saldo_inicial
               + COALESCE((SELECT SUM(quantidade) FROM ajustes a
                           WHERE a.cod_produto = p.cod_produto
                             AND a.data > p.data_saldo_inicial AND a.data <= :d), 0)
               - COALESCE((SELECT SUM(quantidade_saida) FROM movimentacao_saida m
                           WHERE m.cod_produto = p.cod_produto
                             AND m.data > p.data_saldo_inicial AND m.data <= :d), 0) AS saldo
        FROM produtos p WHERE p.cod_produto = :c
    """, c=normalizar_cod(cod), d=dia)
    if df.empty:
        raise ValueError(f"Produto {cod} não encontrado.")
    return Decimal(str(df.iloc[0]["saldo"]))


UNIDADE_PADRAO = "Barra da Estiva"


def listar_unidades() -> list[dict]:
    """As unidades de estoque, na ordem de exibição. list[dict] e não DataFrame: isto é
    chamado também pelos robôs, que rodam sem pandas (ver o import no topo)."""
    with get_engine().begin() as conn:
        linhas = conn.execute(text("""
            SELECT nome, destino_sgi, ordem, usa_saldo_do_produto
            FROM unidades ORDER BY ordem, nome""")).mappings().all()
    return [dict(l) for l in linhas]


def saldo_por_unidade(unidade: str | None = None, so_ativos: bool = True) -> pd.DataFrame:
    """Saldo de cada produto numa unidade (ou de todas, se unidade=None).

    O consolidado NÃO sai daqui: é v_saldo_produto, que já soma as unidades e tem as
    mesmas colunas de sempre - foi feito assim para o painel atual não precisar mudar
    quando as unidades entraram."""
    cond, params = ["TRUE"], {}
    if unidade:
        cond.append("unidade_estoque = :u"); params["u"] = unidade
    if so_ativos:
        cond.append("ativo")
    return _df(f"""SELECT * FROM v_saldo_produto_unidade
                   WHERE {' AND '.join(cond)}
                   ORDER BY descricao, unidade_estoque""", **params)


def produtos_para_match() -> dict:
    """{cod_produto: descricao} dos produtos ATIVOS, para casar descrição de planilha
    com o cadastro (ver estoque/match_produto.py).

    dict e não DataFrame porque quem usa isto é script de linha de comando, que roda
    no Python 32-bit sem pandas - a mesma regra de possiveis_duplicatas_* e
    ultimas_execucoes_lista."""
    with get_engine().begin() as conn:
        linhas = conn.execute(text(
            "SELECT cod_produto, descricao FROM produtos WHERE ativo ORDER BY descricao")).all()
    return {c: d for c, d in linhas}


def unidade_tem_contagem(unidade: str) -> bool:
    """A unidade já tem contagem física registrada?

    A matriz responde TRUE mesmo sem linhas em saldo_inicial_unidade, porque herda
    produtos.saldo_inicial (usa_saldo_do_produto). Sem essa distinção o painel avisaria
    "sem contagem" justamente na unidade que tem a contagem mais antiga do sistema."""
    with get_engine().begin() as conn:
        return bool(conn.execute(text("""
            SELECT EXISTS (SELECT 1 FROM unidades
                            WHERE nome = :u AND usa_saldo_do_produto)
                OR EXISTS (SELECT 1 FROM saldo_inicial_unidade WHERE unidade_estoque = :u)
        """), {"u": unidade}).scalar())


def definir_saldo_inicial_unidade(cod: str, unidade: str, saldo, dia: date) -> None:
    """Grava a contagem física que serve de marco zero daquela unidade.

    Para a matriz isto é opcional: sem linha aqui, ela herda produtos.saldo_inicial
    (unidades.usa_saldo_do_produto) - foi o que evitou migrar os dados de produção."""
    with get_engine().begin() as conn:
        conn.execute(text("""
            INSERT INTO saldo_inicial_unidade
                (cod_produto, unidade_estoque, saldo_inicial, data_saldo_inicial)
            VALUES (:c, :u, :s, :d)
            ON CONFLICT (cod_produto, unidade_estoque) DO UPDATE SET
                saldo_inicial = EXCLUDED.saldo_inicial,
                data_saldo_inicial = EXCLUDED.data_saldo_inicial,
                atualizado_em = now()
        """), {"c": normalizar_cod(cod), "u": unidade, "s": para_decimal(saldo), "d": dia})


def registrar_ajuste(
    cod: str, dia: date, tipo: str, quantidade, custo_unitario=None, observacao=None,
    atualizar_custo: bool = False, unidade_estoque: str = UNIDADE_PADRAO,
) -> int:
    """`quantidade` entra POSITIVA para entrada/transferência/perda (o sinal vem do
    tipo). Para 'correcao' é o delta assinado. Recusa datas <= data_saldo_inicial: o
    saldo ignora esses movimentos (a contagem já os incorpora).

    `unidade_estoque` é a unidade cujo estoque o ajuste move - uma perda em Piatã não
    pode descontar da matriz. O default mantém o comportamento de antes das unidades,
    quando todo ajuste era da matriz."""
    if tipo not in TIPOS_AJUSTE:
        raise ValueError(f"Tipo inválido: {tipo}")
    cod = normalizar_cod(cod)
    q = para_decimal(quantidade)
    if q is None or q == 0:
        raise ValueError("Quantidade deve ser diferente de zero.")
    sinal = TIPOS_AJUSTE[tipo][0]
    if sinal != 0:
        if q < 0:
            raise ValueError("Informe a quantidade positiva; o sinal é definido pelo tipo.")
        q = q * sinal
    prod = obter_produto(cod)
    if prod is None:
        raise ValueError(f"Produto {cod} não encontrado.")
    if dia <= prod["data_saldo_inicial"]:
        raise ValueError(
            f"Data {dia:%d/%m/%Y} é anterior ou igual à data da contagem inicial "
            f"({prod['data_saldo_inicial']:%d/%m/%Y}); o movimento seria ignorado no saldo."
        )
    custo = para_decimal(custo_unitario)
    with get_engine().begin() as conn:
        novo_id = conn.execute(text("""
            INSERT INTO ajustes
                (cod_produto, data, tipo, quantidade, custo_unitario, observacao, unidade_estoque)
            VALUES (:c, :d, :t, :q, :cu, :o, :u) RETURNING id
        """), {"c": cod, "d": dia, "t": tipo, "q": q, "cu": custo,
               "o": texto_ou_none(observacao), "u": unidade_estoque}).scalar_one()
        if atualizar_custo and custo is not None and tipo == "entrada":
            conn.execute(text("UPDATE produtos SET preco_custo = :cu, atualizado_em = now() WHERE cod_produto = :c"),
                         {"cu": custo, "c": cod})
    return int(novo_id)


def definir_saldo_por_contagem(cod: str, saldo_contado, dia: date, observacao=None) -> Decimal:
    """'Editar o número': o usuário informa o que contou; o sistema grava a diferença
    como ajuste 'correcao' (mantém trilha de auditoria). Devolve o delta (0 = nada a
    fazer, nenhum registro criado)."""
    contado = para_decimal(saldo_contado)
    if contado is None:
        raise ValueError("Saldo contado inválido.")
    delta = contado - saldo_em(cod, dia)
    if delta == 0:
        return Decimal(0)
    registrar_ajuste(cod, dia, "correcao", delta, observacao=observacao or "Contagem física")
    return delta


def listar_ajustes(cod: str | None = None, limite: int = 500) -> pd.DataFrame:
    if cod:
        return _df("""SELECT a.id, a.data, a.cod_produto, p.descricao, a.tipo, a.quantidade,
                             a.custo_unitario, a.observacao, a.criado_em
                      FROM ajustes a JOIN produtos p USING (cod_produto)
                      WHERE a.cod_produto = :c ORDER BY a.data DESC, a.id DESC LIMIT :l""",
                   c=normalizar_cod(cod), l=limite)
    return _df("""SELECT a.id, a.data, a.cod_produto, p.descricao, a.tipo, a.quantidade,
                         a.custo_unitario, a.observacao, a.criado_em
                  FROM ajustes a JOIN produtos p USING (cod_produto)
                  ORDER BY a.data DESC, a.id DESC LIMIT :l""", l=limite)


def excluir_ajuste(ajuste_id: int) -> None:
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM ajustes WHERE id = :i"), {"i": int(ajuste_id)})


# ------------------------------------------------------------------------ movimentação
def substituir_movimentacao_dia(
    loja: str, dia: date, linhas: list[dict], permitir_zerar: bool = False,
) -> dict:
    """Idempotente. Em UMA transação: upsert de cada (cod, dia, loja) SOBRESCREVENDO a
    quantidade (nunca soma), remove os códigos daquele (loja, dia) que não vieram no
    relatório (venda cancelada depois) e registra o dia em sync_dias.
    `linhas`: [{'cod_produto','descricao','quantidade_saida','valor_saida'}] com um
    item por código (ver parser_sgi.agregar_por_codigo).
    Se `linhas` vier vazio e o dia já tinha vendas, recusa (ZerarDiaNaoPermitido) a
    menos que permitir_zerar=True — um PDF vazio por falha transitória do SGI não pode
    apagar um dia inteiro."""
    cods = [l["cod_produto"] for l in linhas]
    if len(set(cods)) != len(cods):
        raise ValueError("linhas com código repetido — agregue antes de gravar.")
    with get_engine().begin() as conn:
        if not linhas and not permitir_zerar:
            existentes = conn.execute(
                text("SELECT COUNT(*) FROM movimentacao_saida WHERE loja = :l AND data = :d"),
                {"l": loja, "d": dia},
            ).scalar_one()
            if existentes:
                raise ZerarDiaNaoPermitido(
                    f"Relatório vazio para {loja} em {dia:%d/%m/%Y}, mas o dia já tem "
                    f"{existentes} produto(s) gravado(s). Nada foi alterado."
                )
        if linhas:
            conn.execute(text("""
                INSERT INTO movimentacao_saida (cod_produto, data, loja, descricao_sgi, quantidade_saida, valor_saida)
                VALUES (:cod, :d, :l, :desc, :q, :v)
                ON CONFLICT (cod_produto, data, loja) DO UPDATE SET
                    descricao_sgi = EXCLUDED.descricao_sgi,
                    quantidade_saida = EXCLUDED.quantidade_saida,
                    valor_saida = EXCLUDED.valor_saida,
                    atualizado_em = now()
            """), [{"cod": l["cod_produto"], "d": dia, "l": loja, "desc": l.get("descricao"),
                    "q": l["quantidade_saida"], "v": l["valor_saida"]} for l in linhas])
        if cods:
            r = conn.execute(
                text("DELETE FROM movimentacao_saida WHERE loja = :l AND data = :d AND cod_produto NOT IN :cods")
                .bindparams(bindparam("cods", expanding=True)),
                {"l": loja, "d": dia, "cods": cods},
            )
        else:
            r = conn.execute(text("DELETE FROM movimentacao_saida WHERE loja = :l AND data = :d"),
                             {"l": loja, "d": dia})
        removidos = r.rowcount
        qtd_total = sum((l["quantidade_saida"] for l in linhas), Decimal(0))
        valor_total = sum((l["valor_saida"] for l in linhas), Decimal(0))
        conn.execute(text("""
            INSERT INTO sync_dias (loja, data, n_produtos, qtd_total, valor_total)
            VALUES (:l, :d, :n, :q, :v)
            ON CONFLICT (loja, data) DO UPDATE SET
                sincronizado_em = now(), n_produtos = EXCLUDED.n_produtos,
                qtd_total = EXCLUDED.qtd_total, valor_total = EXCLUDED.valor_total
        """), {"l": loja, "d": dia, "n": len(linhas), "q": qtd_total, "v": valor_total})
    return {"gravados": len(linhas), "removidos": removidos}


# ------------------------------------------------------------ movimentação de entrada (compras SGI)
def substituir_movimentacao_entrada_dia(
    loja: str, dia: date, linhas: list[dict], permitir_zerar: bool = False,
) -> dict:
    """Espelho de substituir_movimentacao_dia, para ENTRADAS por compra sincronizadas do
    relatório 'Relação de Custo de Compras' do SGI (scripts/sync_compras_sgi.py).
    Idempotente: upsert por (cod, dia, loja), nunca soma a mesma compra duas vezes
    mesmo rodando com janelas sobrepostas. `linhas`:
    [{'cod_produto','descricao','quantidade_entrada','valor_entrada'}]."""
    cods = [l["cod_produto"] for l in linhas]
    if len(set(cods)) != len(cods):
        raise ValueError("linhas com código repetido — agregue antes de gravar.")
    with get_engine().begin() as conn:
        if not linhas and not permitir_zerar:
            existentes = conn.execute(
                text("SELECT COUNT(*) FROM movimentacao_entrada_compra WHERE loja = :l AND data = :d"),
                {"l": loja, "d": dia},
            ).scalar_one()
            if existentes:
                raise ZerarDiaNaoPermitido(
                    f"Relatório de compras vazio para {loja} em {dia:%d/%m/%Y}, mas o dia já tem "
                    f"{existentes} produto(s) gravado(s). Nada foi alterado."
                )
        if linhas:
            conn.execute(text("""
                INSERT INTO movimentacao_entrada_compra
                    (cod_produto, data, loja, descricao_sgi, quantidade_entrada, valor_entrada)
                VALUES (:cod, :d, :l, :desc, :q, :v)
                ON CONFLICT (cod_produto, data, loja) DO UPDATE SET
                    descricao_sgi = EXCLUDED.descricao_sgi,
                    quantidade_entrada = EXCLUDED.quantidade_entrada,
                    valor_entrada = EXCLUDED.valor_entrada,
                    atualizado_em = now()
            """), [{"cod": l["cod_produto"], "d": dia, "l": loja, "desc": l.get("descricao"),
                    "q": l["quantidade_entrada"], "v": l["valor_entrada"]} for l in linhas])
        if cods:
            r = conn.execute(
                text("DELETE FROM movimentacao_entrada_compra WHERE loja = :l AND data = :d AND cod_produto NOT IN :cods")
                .bindparams(bindparam("cods", expanding=True)),
                {"l": loja, "d": dia, "cods": cods},
            )
        else:
            r = conn.execute(text("DELETE FROM movimentacao_entrada_compra WHERE loja = :l AND data = :d"),
                             {"l": loja, "d": dia})
        removidos = r.rowcount
        # para_decimal (não Decimal() direto): linhas vem de parser_compras_sgi, que
        # devolve float (via xlrd) - Decimal(0) + float dá TypeError.
        qtd_total = sum((para_decimal(l["quantidade_entrada"]) for l in linhas), Decimal(0))
        valor_total = sum((para_decimal(l["valor_entrada"]) for l in linhas), Decimal(0))
        conn.execute(text("""
            INSERT INTO sync_dias_compras (loja, data, n_produtos, qtd_total, valor_total)
            VALUES (:l, :d, :n, :q, :v)
            ON CONFLICT (loja, data) DO UPDATE SET
                sincronizado_em = now(), n_produtos = EXCLUDED.n_produtos,
                qtd_total = EXCLUDED.qtd_total, valor_total = EXCLUDED.valor_total
        """), {"l": loja, "d": dia, "n": len(linhas), "q": qtd_total, "v": valor_total})
    return {"gravados": len(linhas), "removidos": removidos}


def dias_sincronizados_compras(loja: str) -> set[date]:
    # Sem pandas de propósito (ver import no topo do arquivo): esta função roda tanto
    # no painel (com pandas) quanto no robô local scripts/sync_compras_sgi.py (sem).
    with get_engine().connect() as conn:
        linhas = conn.execute(
            text("SELECT data FROM sync_dias_compras WHERE loja = :l"), {"l": loja}
        ).fetchall()
    return {r[0] for r in linhas}


def substituir_transferencias_periodo(destino: str, inicio: date, fim: date,
                                      por_dia: dict) -> dict:
    """Grava as transferências de SAÍDA (Porteira -> `destino`) do período, do robô
    scripts/sync_transferencias_sgi.py.

    Por período, e não por dia como as compras, porque o relatório "Relação de
    Transferências" aceita um intervalo e traz a data em cada linha: uma exportação só
    cobre tudo. A gravação APAGA o intervalo e regrava - assim um dia que deixou de ter
    transferência (lançamento estornado no SGI) some do nosso lado também, o que o
    upsert por dia sozinho não faria.

    `por_dia`: {date: [{'cod_produto','descricao','quantidade_transferida',
    'valor_transferido'}]} - a saída de parser_transferencias_sgi.agrupar_por_dia."""
    for dia, linhas in por_dia.items():
        cods = [l["cod_produto"] for l in linhas]
        if len(set(cods)) != len(cods):
            raise ValueError(f"linhas com código repetido em {dia:%d/%m/%Y} — agregue antes de gravar.")

    registros = [
        {"cod": l["cod_produto"], "d": dia, "dest": destino, "desc": l.get("descricao"),
         "q": l["quantidade_transferida"], "v": l["valor_transferido"]}
        for dia, linhas in por_dia.items() for l in linhas
    ]
    with get_engine().begin() as conn:
        r = conn.execute(text("""DELETE FROM movimentacao_transferencia
                                 WHERE destino = :dest AND data BETWEEN :ini AND :fim"""),
                         {"dest": destino, "ini": inicio, "fim": fim})
        removidos = r.rowcount
        if registros:
            conn.execute(text("""
                INSERT INTO movimentacao_transferencia
                    (cod_produto, data, destino, descricao_sgi, quantidade_transferida, valor_transferido)
                VALUES (:cod, :d, :dest, :desc, :q, :v)
                ON CONFLICT (cod_produto, data, destino) DO UPDATE SET
                    descricao_sgi = EXCLUDED.descricao_sgi,
                    quantidade_transferida = EXCLUDED.quantidade_transferida,
                    valor_transferido = EXCLUDED.valor_transferido,
                    atualizado_em = now()
            """), registros)
    return {"gravados": len(registros), "removidos": removidos, "dias": len(por_dia)}


def listar_transferencias(ini: date | None = None, fim: date | None = None,
                          cod: str | None = None, unidade_origem: str | None = None,
                          unidade_destino: str | None = None) -> pd.DataFrame:
    """A MESMA transferência é saída de uma unidade e entrada na outra - qual das duas
    coisas ela é depende de quem pergunta. `unidade_origem` traz o que saiu dali;
    `unidade_destino`, o que chegou ali (casando com unidades.destino_sgi, o rótulo que
    o SGI usa). Sem filtro, traz tudo, que é o que o consolidado quer."""
    cond, params = ["TRUE"], {}
    if unidade_origem:
        cond.append("origem = :uo"); params["uo"] = unidade_origem
    if unidade_destino:
        cond.append("destino = (SELECT destino_sgi FROM unidades WHERE nome = :ud)")
        params["ud"] = unidade_destino
    if ini:
        cond.append("data >= :ini"); params["ini"] = ini
    if fim:
        cond.append("data <= :fim"); params["fim"] = fim
    if cod:
        cond.append("cod_produto = :cod"); params["cod"] = normalizar_cod(cod)
    return _df(f"""SELECT cod_produto, data, destino, descricao_sgi,
                          quantidade_transferida, valor_transferido
                   FROM movimentacao_transferencia WHERE {' AND '.join(cond)}
                   ORDER BY data DESC, cod_produto""", **params)


def ultima_data_transferencia(destino: str) -> date | None:
    """Data da transferência mais recente já gravada para esse destino, ou None.

    É o que dispensa uma tabela de controle para o robô de transferências (as compras
    têm sync_dias_compras). Ele regrava por PERÍODO, então basta começar na última data
    conhecida e ir até hoje: tudo que veio depois dela entra, inclusive os dias em que o
    notebook ficou desligado. Dias sem transferência nenhuma não movem esta data - e
    isso é inofensivo, porque regravar um dia vazio é um no-op (apaga nada, insere
    nada)."""
    with get_engine().begin() as conn:
        r = conn.execute(text("""SELECT MAX(data) FROM movimentacao_transferencia
                                 WHERE destino = :dest"""), {"dest": destino}).scalar()
    return r


def possiveis_duplicatas_transferencia(dia: date, codigos: list[str]) -> list[dict]:
    """Ajustes manuais de SAÍDA (quantidade negativa) que já existem para esses produtos
    nesse dia - candidatos a descontar em dobro quando o robô gravar a mesma
    transferência.

    Espelha possiveis_duplicatas_compra, mas olhando o outro sinal: transferência tira
    do estoque, e hoje essas saídas são lançadas à mão no painel (confirmado pelo
    usuário em 09/10/2026), então ao ligar o robô os lançamentos manuais dos mesmos
    dias precisam sair."""
    if not codigos:
        return []
    with get_engine().connect() as conn:
        linhas = conn.execute(
            text("""SELECT a.id, a.cod_produto, p.descricao, a.quantidade, a.tipo, a.observacao
                    FROM ajustes a
                    JOIN produtos p ON p.cod_produto = a.cod_produto
                    WHERE a.data = :d AND a.quantidade < 0
                      AND a.cod_produto = ANY(:cods)
                    ORDER BY a.cod_produto"""),
            {"d": dia, "cods": list(codigos)},
        ).fetchall()
    return [
        {"id": r[0], "cod_produto": r[1], "descricao": r[2],
         "quantidade": r[3], "tipo": r[4], "observacao": r[5]}
        for r in linhas
    ]


def possiveis_duplicatas_compra(dia: date, codigos: list[str]) -> list[dict]:
    """Entradas manuais (ajustes de quantidade POSITIVA) que ja existem para esses
    produtos nesse dia - ou seja, candidatas a contar em dobro quando o robo de compras
    gravar a mesma entrada.

    So olha produtos CADASTRADOS (os ajustes tem FK pra produtos), que sao justamente
    os monitorados - compra de coleira/racao nao entra no controle e nao gera ajuste.
    Em 02/10/2026 esse caso aconteceu de verdade: o JOINER tinha um ajuste manual
    'ENTRADA NF' de +12 que teria somado em cima da compra importada.

    Sem pandas de proposito: roda no robo local, que nao tem pandas (Python 32-bit)."""
    if not codigos:
        return []
    with get_engine().connect() as conn:
        linhas = conn.execute(
            text("""SELECT a.id, a.cod_produto, p.descricao, a.quantidade, a.tipo, a.observacao
                    FROM ajustes a
                    JOIN produtos p ON p.cod_produto = a.cod_produto
                    WHERE a.data = :d AND a.quantidade > 0
                      AND a.cod_produto = ANY(:cods)
                    ORDER BY a.cod_produto"""),
            {"d": dia, "cods": list(codigos)},
        ).fetchall()
    return [
        {"id": r[0], "cod_produto": r[1], "descricao": r[2],
         "quantidade": r[3], "tipo": r[4], "observacao": r[5]}
        for r in linhas
    ]


def listar_sync_dias_compras(limite: int = 200) -> pd.DataFrame:
    return _df("""SELECT loja, data, n_produtos, qtd_total, valor_total, sincronizado_em
                  FROM sync_dias_compras ORDER BY data DESC, loja LIMIT :l""", l=limite)


def listar_movimentacao_entrada_compra(ini: date | None = None, fim: date | None = None,
                                       cod: str | None = None,
                                       unidade: str | None = None) -> pd.DataFrame:
    cond, params = ["TRUE"], {}
    if unidade:
        cond.append("loja IN (SELECT loja FROM unidades_loja WHERE unidade_estoque = :u)")
        params["u"] = unidade
    if ini:
        cond.append("data >= :ini"); params["ini"] = ini
    if fim:
        cond.append("data <= :fim"); params["fim"] = fim
    if cod:
        cond.append("cod_produto = :cod"); params["cod"] = normalizar_cod(cod)
    return _df(f"""SELECT cod_produto, data, loja, descricao_sgi, quantidade_entrada, valor_entrada
                  FROM movimentacao_entrada_compra WHERE {' AND '.join(cond)}
                  ORDER BY data DESC, cod_produto""", **params)


def produtos_defensivos_faltantes(lista_esperada: list[tuple[str, str]]) -> pd.DataFrame:
    """Confere a lista fixa de códigos esperados (rol de defensivos) contra o cadastro:
    devolve os que estão na lista mas NÃO existem (ou estão inativos) em `produtos`."""
    if not lista_esperada:
        return pd.DataFrame(columns=["cod_produto", "descricao_esperada"])
    cods = [normalizar_cod(c) for c, _ in lista_esperada]
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT cod_produto FROM produtos WHERE cod_produto IN :cods AND ativo")
            .bindparams(bindparam("cods", expanding=True)),
            {"cods": cods},
        ).fetchall()
    existentes = {r[0] for r in rows}
    faltando = [(c, d) for c, d in lista_esperada if normalizar_cod(c) not in existentes]
    return pd.DataFrame(faltando, columns=["cod_produto", "descricao_esperada"])


def listar_movimentacao(ini: date | None = None, fim: date | None = None, cod: str | None = None) -> pd.DataFrame:
    """Uma linha por (cod, data, loja)."""
    cond, params = ["TRUE"], {}
    if ini:
        cond.append("data >= :ini"); params["ini"] = ini
    if fim:
        cond.append("data <= :fim"); params["fim"] = fim
    if cod:
        cond.append("cod_produto = :cod"); params["cod"] = normalizar_cod(cod)
    return _df(f"""SELECT cod_produto, data, loja, descricao_sgi, quantidade_saida, valor_saida
                   FROM movimentacao_saida WHERE {' AND '.join(cond)} ORDER BY data DESC, cod_produto""", **params)


def listar_movimentacao_agregada(unidade: str | None = None) -> pd.DataFrame:
    """Soma das lojas por (cod, data) — entrada dos indicadores.

    Com `unidade`, soma só as lojas daquela unidade de estoque: o giro de Piatã não
    pode incluir as vendas de Barra da Estiva. Sem ela, soma tudo (o consolidado)."""
    filtro = ""
    params = {}
    if unidade:
        filtro = ("JOIN unidades_loja ul ON ul.loja = m.loja AND ul.unidade_estoque = :u")
        params["u"] = unidade
    return _df(f"""SELECT m.cod_produto, m.data, SUM(m.quantidade_saida) AS quantidade_saida,
                          SUM(m.valor_saida) AS valor_saida
                   FROM movimentacao_saida m {filtro}
                   GROUP BY m.cod_produto, m.data""", **params)


def listar_ajustes_todos(unidade: str | None = None) -> pd.DataFrame:
    cond, params = ["TRUE"], {}
    if unidade:
        cond.append("unidade_estoque = :u"); params["u"] = unidade
    return _df(f"SELECT cod_produto, data, tipo, quantidade FROM ajustes "
               f"WHERE {' AND '.join(cond)}", **params)


def listar_produtos_base(unidade: str | None = None) -> pd.DataFrame:
    """Cadastro + saldo inicial. Com `unidade`, o saldo inicial e a data vêm da
    contagem DAQUELA unidade (v_saldo_produto_unidade); sem ela, do cadastro, que é o
    comportamento de sempre.

    A data importa tanto quanto o número: é o corte do que já está dentro da contagem,
    e cada unidade contou num dia diferente."""
    if unidade:
        return _df("""SELECT cod_produto, descricao, unidade, saldo_inicial, data_saldo_inicial,
                             preco_custo, preco_venda, lead_time_dias, estoque_minimo,
                             fornecedor, observacao, ativo
                      FROM v_saldo_produto_unidade WHERE unidade_estoque = :u
                      ORDER BY descricao""", u=unidade)
    return _df("""SELECT cod_produto, descricao, unidade, saldo_inicial, data_saldo_inicial,
                         preco_custo, preco_venda, lead_time_dias, estoque_minimo, fornecedor,
                         observacao, ativo
                  FROM produtos ORDER BY descricao""")


# -------------------------------------------------------------- não encontrados / ignorados
def listar_nao_encontrados() -> pd.DataFrame:
    return _df("SELECT * FROM v_nao_encontrados ORDER BY valor_total DESC")


def ignorar_codigo(cod: str, descricao: str | None = None, motivo: str | None = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(text("""
            INSERT INTO produtos_ignorados (cod_produto, descricao, motivo) VALUES (:c, :d, :m)
            ON CONFLICT (cod_produto) DO UPDATE SET descricao = EXCLUDED.descricao, motivo = EXCLUDED.motivo
        """), {"c": normalizar_cod(cod), "d": texto_ou_none(descricao), "m": texto_ou_none(motivo)})


def desfazer_ignorar(cod: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM produtos_ignorados WHERE cod_produto = :c"), {"c": normalizar_cod(cod)})


def listar_ignorados() -> pd.DataFrame:
    return _df("SELECT cod_produto, descricao, motivo, criado_em FROM produtos_ignorados ORDER BY cod_produto")


# ------------------------------------------------------------------------------ sync
def dias_sincronizados(loja: str) -> set[date]:
    df = _df("SELECT data FROM sync_dias WHERE loja = :l", l=loja)
    return set(df["data"]) if not df.empty else set()


def listar_sync_dias(limite: int = 200) -> pd.DataFrame:
    return _df("""SELECT loja, data, n_produtos, qtd_total, valor_total, sincronizado_em
                  FROM sync_dias ORDER BY data DESC, loja LIMIT :l""", l=limite)


def iniciar_execucao(origem: str) -> int:
    with get_engine().begin() as conn:
        return int(conn.execute(
            text("INSERT INTO sync_execucoes (origem) VALUES (:o) RETURNING id"), {"o": origem}
        ).scalar_one())


def finalizar_execucao(exec_id: int, status: str, resumo: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(text("""UPDATE sync_execucoes SET finalizado_em = now(), status = :s, resumo = :r
                             WHERE id = :i"""), {"s": status, "r": resumo[:4000], "i": exec_id})


def ultimas_execucoes(limite: int = 10, origem: str | None = None) -> pd.DataFrame:
    """Histórico de execuções: o sync de vendas e os robôs do SGI desktop, na mesma
    tabela. `periodo_inicio/periodo_fim/dias/linhas` só vêm preenchidos nas execuções
    dos robôs (ver registrar_execucao_robo)."""
    cond, params = ["TRUE"], {"l": limite}
    if origem:
        cond.append("origem = :o"); params["o"] = origem
    return _df(f"""SELECT id, iniciado_em, finalizado_em, origem, status,
                          periodo_inicio, periodo_fim, dias, linhas, resumo
                   FROM sync_execucoes WHERE {' AND '.join(cond)}
                   ORDER BY id DESC LIMIT :l""", **params)


# -------------------------------------------------------------------------------- pdfs
def salvar_pdf(loja: str, dia: date, pdf_bytes: bytes) -> None:
    """Guarda (sobrescrevendo) o PDF original de um (loja, dia). Chamado pelo sync
    logo após o download, antes/depois de gravar as saídas — falha aqui não deve
    interromper a gravação das vendas (ver scripts/sync_sgi.py)."""
    with get_engine().begin() as conn:
        conn.execute(text("""
            INSERT INTO sync_pdfs (loja, data, pdf, tamanho)
            VALUES (:l, :d, :p, :t)
            ON CONFLICT (loja, data) DO UPDATE SET
                pdf = EXCLUDED.pdf, tamanho = EXCLUDED.tamanho, criado_em = now()
        """), {"l": loja, "d": dia, "p": pdf_bytes, "t": len(pdf_bytes)})


def listar_pdfs() -> pd.DataFrame:
    """Só metadados (sem os bytes) — leve para listar/filtrar na tela."""
    return _df("SELECT loja, data, tamanho, criado_em FROM sync_pdfs ORDER BY data DESC, loja")


def obter_pdf(loja: str, dia: date) -> bytes | None:
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT pdf FROM sync_pdfs WHERE loja = :l AND data = :d"), {"l": loja, "d": dia}
        ).first()
    return bytes(row[0]) if row else None


# ------------------------------------------------------------- entrada por nota fiscal
def buscar_mapa_fornecedor(cnpj_emitente: str) -> dict:
    """cod_fornecedor -> cod_produto já associados para este CNPJ, para pré-preencher a
    tela de entrada por nota na próxima vez que vier uma nota do mesmo fornecedor."""
    if not cnpj_emitente:
        return {}
    df = _df("SELECT cod_fornecedor, cod_produto FROM mapa_fornecedor_produto WHERE cnpj_emitente = :c",
             c=cnpj_emitente)
    return dict(zip(df["cod_fornecedor"], df["cod_produto"]))


def salvar_mapa_fornecedor(cnpj_emitente: str, cod_fornecedor: str, cod_produto: str,
                           descricao_fornecedor: str | None = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(text("""
            INSERT INTO mapa_fornecedor_produto (cnpj_emitente, cod_fornecedor, cod_produto, descricao_fornecedor)
            VALUES (:cn, :cf, :cp, :d)
            ON CONFLICT (cnpj_emitente, cod_fornecedor) DO UPDATE SET
                cod_produto = EXCLUDED.cod_produto, descricao_fornecedor = EXCLUDED.descricao_fornecedor,
                atualizado_em = now()
        """), {"cn": cnpj_emitente, "cf": cod_fornecedor, "cp": normalizar_cod(cod_produto),
               "d": descricao_fornecedor})
