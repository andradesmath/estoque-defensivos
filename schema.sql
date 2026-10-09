-- schema.sql - Controle de estoque de DEFENSIVOS (PostgreSQL >= 13).
-- Idempotente: pode rodar a cada start (db.init_schema()). Não usar '%' neste arquivo
-- (o driver psycopg2 trataria como parâmetro).
--
-- Princípio central: o saldo NUNCA é um contador armazenado. É sempre calculado:
--   saldo_atual = saldo_inicial + SUM(ajustes) - SUM(saidas)
-- considerando só movimentos posteriores a data_saldo_inicial (a contagem física vale
-- para o FIM daquele dia). Como movimentacao_saida tem chave (cod, data, loja) e cada
-- sincronização SOBRESCREVE o dia, rodar o sync N vezes com janelas sobrepostas não
-- desconta a mesma venda duas vezes.

CREATE TABLE IF NOT EXISTS produtos (
    cod_produto         VARCHAR(10)   PRIMARY KEY,
    descricao           TEXT          NOT NULL,
    unidade             VARCHAR(20),
    saldo_inicial       NUMERIC(14,3) NOT NULL DEFAULT 0,
    data_saldo_inicial  DATE          NOT NULL,
    preco_custo         NUMERIC(14,4),
    preco_venda         NUMERIC(14,4),
    lead_time_dias      INTEGER,
    estoque_minimo      NUMERIC(14,3),
    fornecedor          TEXT,
    observacao          TEXT,
    ativo               BOOLEAN       NOT NULL DEFAULT TRUE,
    data_importacao     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    atualizado_em       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT ck_produtos_lead_time CHECK (lead_time_dias IS NULL OR lead_time_dias >= 0),
    CONSTRAINT ck_produtos_precos CHECK (
        (preco_custo IS NULL OR preco_custo >= 0) AND (preco_venda IS NULL OR preco_venda >= 0)
    )
);

-- Saídas por venda vindas do SGI. Guarda TODOS os códigos do relatório (inclusive os
-- que ainda não estão em `produtos`): se o produto for cadastrado depois, as vendas
-- passadas passam a contar sozinhas. Sem FK de propósito.
CREATE TABLE IF NOT EXISTS movimentacao_saida (
    cod_produto       VARCHAR(10)   NOT NULL,
    data              DATE          NOT NULL,
    loja              VARCHAR(40)   NOT NULL,
    descricao_sgi     TEXT,
    quantidade_saida  NUMERIC(14,3) NOT NULL,
    valor_saida       NUMERIC(14,2) NOT NULL DEFAULT 0,
    atualizado_em     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (cod_produto, data, loja)
);
CREATE INDEX IF NOT EXISTS ix_mov_saida_data ON movimentacao_saida (data);

-- Entradas por COMPRA, sincronizadas automaticamente do relatório "Relação de Custo de
-- Compras" do SGI (robô local, estoque-defensivos/scripts/sync_compras_sgi.py). Mesmo
-- padrão idempotente de movimentacao_saida: chave (cod, data, loja), upsert por dia —
-- rodar o sync várias vezes com janelas sobrepostas não soma a mesma compra duas vezes.
CREATE TABLE IF NOT EXISTS movimentacao_entrada_compra (
    cod_produto        VARCHAR(10)   NOT NULL,
    data               DATE          NOT NULL,
    loja               VARCHAR(40)   NOT NULL,
    descricao_sgi      TEXT,
    quantidade_entrada NUMERIC(14,3) NOT NULL,
    valor_entrada      NUMERIC(14,2) NOT NULL DEFAULT 0,
    atualizado_em      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (cod_produto, data, loja)
);
CREATE INDEX IF NOT EXISTS ix_mov_entrada_compra_data ON movimentacao_entrada_compra (data);

-- Movimentos manuais. quantidade é ASSINADA: positivo aumenta o estoque.
CREATE TABLE IF NOT EXISTS ajustes (
    id              BIGSERIAL     PRIMARY KEY,
    cod_produto     VARCHAR(10)   NOT NULL REFERENCES produtos (cod_produto) ON DELETE CASCADE,
    data            DATE          NOT NULL,
    tipo            VARCHAR(30)   NOT NULL,
    quantidade      NUMERIC(14,3) NOT NULL,
    custo_unitario  NUMERIC(14,4),
    observacao      TEXT,
    criado_em       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT ck_ajustes_tipo CHECK (tipo IN (
        'entrada', 'transferencia_entrada', 'transferencia_saida', 'perda', 'correcao'
    )),
    CONSTRAINT ck_ajustes_qtd CHECK (quantidade <> 0)
);
CREATE INDEX IF NOT EXISTS ix_ajustes_cod ON ajustes (cod_produto, data);

-- Códigos que o SGI classifica como DEFENSIVOS mas que não entram no controle
-- (adjuvantes, fertilizantes etc.). Somem da lista de "não encontrados".
CREATE TABLE IF NOT EXISTS produtos_ignorados (
    cod_produto  VARCHAR(10) PRIMARY KEY,
    descricao    TEXT,
    motivo       TEXT,
    criado_em    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Um registro por (loja, dia) já sincronizado com sucesso: base para descobrir dias
-- faltantes e para auditar o que o relatório dizia (totais impressos no PDF).
CREATE TABLE IF NOT EXISTS sync_dias (
    loja             VARCHAR(40)   NOT NULL,
    data             DATE          NOT NULL,
    sincronizado_em  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    n_produtos       INTEGER       NOT NULL,
    qtd_total        NUMERIC(14,3) NOT NULL,
    valor_total      NUMERIC(14,2) NOT NULL,
    PRIMARY KEY (loja, data)
);

-- Mesmo controle de "dias já vistos" de sync_dias, mas para o robô de Compras (janela
-- móvel): sem isso, um dia sem nenhuma compra nunca entraria aqui e seria rebuscado
-- para sempre (em vez de só nos últimos N dias da janela).
CREATE TABLE IF NOT EXISTS sync_dias_compras (
    loja             VARCHAR(40)   NOT NULL,
    data             DATE          NOT NULL,
    sincronizado_em  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    n_produtos       INTEGER       NOT NULL,
    qtd_total        NUMERIC(14,3) NOT NULL,
    valor_total      NUMERIC(14,2) NOT NULL,
    PRIMARY KEY (loja, data)
);

-- PDF original baixado do SGI, um por (loja, dia), guardado para conferência/auditoria.
-- Reaproveita o mesmo padrão de sync_dias: sobrescreve se o dia for sincronizado de novo.
CREATE TABLE IF NOT EXISTS sync_pdfs (
    loja        VARCHAR(40)   NOT NULL,
    data        DATE          NOT NULL,
    pdf         BYTEA         NOT NULL,
    tamanho     INTEGER       NOT NULL,
    criado_em   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (loja, data)
);

-- Associa o código do produto usado pelo fornecedor (nas notas fiscais dele) ao código
-- interno do produto, por CNPJ do emitente. Preenchida ao confirmar a 1ª entrada por nota
-- de cada fornecedor; pré-preenche a tela sozinha nas próximas notas do mesmo emitente.
CREATE TABLE IF NOT EXISTS mapa_fornecedor_produto (
    cnpj_emitente        VARCHAR(20)  NOT NULL,
    cod_fornecedor       VARCHAR(40)  NOT NULL,
    cod_produto          VARCHAR(10)  NOT NULL REFERENCES produtos(cod_produto),
    descricao_fornecedor TEXT,
    atualizado_em        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (cnpj_emitente, cod_fornecedor)
);

CREATE TABLE IF NOT EXISTS sync_execucoes (
    id             BIGSERIAL   PRIMARY KEY,
    iniciado_em    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalizado_em  TIMESTAMPTZ,
    origem         VARCHAR(30),
    status         VARCHAR(20) NOT NULL DEFAULT 'rodando',
    resumo         TEXT
);

CREATE OR REPLACE VIEW v_saldo_produto AS
SELECT
    p.cod_produto,
    p.descricao,
    p.unidade,
    p.saldo_inicial,
    p.data_saldo_inicial,
    COALESCE(a.total, 0)                                        AS ajustes_total,
    COALESCE(s.qtd, 0)                                          AS saidas_total,
    COALESCE(s.valor, 0)                                        AS valor_saidas_total,
    p.saldo_inicial + COALESCE(a.total, 0) - COALESCE(s.qtd, 0)
        + COALESCE(ec.qtd, 0)                                   AS saldo_atual,
    p.preco_custo,
    p.preco_venda,
    p.lead_time_dias,
    p.estoque_minimo,
    p.fornecedor,
    p.observacao,
    p.ativo,
    -- Novas (ao final de propósito: CREATE OR REPLACE VIEW do Postgres só aceita
    -- colunas novas no fim da lista - inserir no meio quebra com InvalidTableDefinition
    -- porque desloca a posição das colunas já existentes em produção).
    COALESCE(ec.qtd, 0)                                         AS entradas_compras_total,
    COALESCE(ec.valor, 0)                                       AS valor_entradas_compras_total
FROM produtos p
LEFT JOIN (
    SELECT m.cod_produto,
           SUM(m.quantidade_saida) AS qtd,
           SUM(m.valor_saida)      AS valor
    FROM movimentacao_saida m
    JOIN produtos p2 ON p2.cod_produto = m.cod_produto
    WHERE m.data > p2.data_saldo_inicial
    GROUP BY m.cod_produto
) s ON s.cod_produto = p.cod_produto
LEFT JOIN (
    SELECT m.cod_produto,
           SUM(m.quantidade_entrada) AS qtd,
           SUM(m.valor_entrada)      AS valor
    FROM movimentacao_entrada_compra m
    JOIN produtos p4 ON p4.cod_produto = m.cod_produto
    WHERE m.data > p4.data_saldo_inicial
    GROUP BY m.cod_produto
) ec ON ec.cod_produto = p.cod_produto
LEFT JOIN (
    SELECT j.cod_produto, SUM(j.quantidade) AS total
    FROM ajustes j
    JOIN produtos p3 ON p3.cod_produto = j.cod_produto
    WHERE j.data > p3.data_saldo_inicial
    GROUP BY j.cod_produto
) a ON a.cod_produto = p.cod_produto;

-- Vendidos no SGI sem cadastro em `produtos` e fora da lista de ignorados.
CREATE OR REPLACE VIEW v_nao_encontrados AS
SELECT
    m.cod_produto,
    MAX(m.descricao_sgi)           AS descricao_sgi,
    SUM(m.quantidade_saida)        AS quantidade_total,
    SUM(m.valor_saida)             AS valor_total,
    MIN(m.data)                    AS primeira_venda,
    MAX(m.data)                    AS ultima_venda,
    STRING_AGG(DISTINCT m.loja, ', ') AS lojas
FROM movimentacao_saida m
LEFT JOIN produtos p            ON p.cod_produto  = m.cod_produto
LEFT JOIN produtos_ignorados ig ON ig.cod_produto = m.cod_produto
WHERE p.cod_produto IS NULL AND ig.cod_produto IS NULL
GROUP BY m.cod_produto;
