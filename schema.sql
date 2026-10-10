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

-- Transferências de SAÍDA para outra loja (hoje só Porteira -> Piatã), sincronizadas
-- do relatório "Relação de Transferências" do SGI desktop (robô local,
-- scripts/sync_transferencias_sgi.py). Diminuem o saldo da Porteira.
--
-- Diferente de movimentacao_entrada_compra, aqui o relatório aceita um PERÍODO e traz a
-- data em cada linha, então o robô sincroniza o período inteiro de uma vez: a gravação
-- apaga o intervalo e regrava (ver db.substituir_transferencias_periodo). A chave
-- (cod, data, destino) mantém a idempotência de qualquer jeito.
CREATE TABLE IF NOT EXISTS movimentacao_transferencia (
    cod_produto            VARCHAR(10)   NOT NULL,
    data                   DATE          NOT NULL,
    destino                VARCHAR(40)   NOT NULL,
    descricao_sgi          TEXT,
    quantidade_transferida NUMERIC(14,3) NOT NULL,
    valor_transferido      NUMERIC(14,2) NOT NULL DEFAULT 0,
    atualizado_em          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (cod_produto, data, destino)
);
CREATE INDEX IF NOT EXISTS ix_mov_transferencia_data ON movimentacao_transferencia (data);

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

-- ============================================================== UNIDADES (multi-loja)
-- Uma UNIDADE DE ESTOQUE é um estoque físico independente. Duas hoje:
--   Barra da Estiva = lojas Porteira + Casa de Adubo. São duas "empresas" no SGI, mas
--                     dividem o MESMO estoque físico - foi assim desde o começo, e é
--                     por isso que o saldo nunca foi separado por loja.
--   Piatã           = a filial. Estoque próprio: o que sai da matriz por transferência
--                     entra aqui.
--
-- NOME DAS COLUNAS: `unidade_estoque`, e não `unidade`, porque `produtos.unidade` já
-- significa unidade de MEDIDA (UN, KG, LT) e aparece ao lado desta na view do saldo.
-- Duas colunas `unidade` com sentidos diferentes no mesmo SELECT é bug silencioso
-- esperando acontecer.
--
-- O que torna isso possível sem reescrever as tabelas de movimento: elas já gravam
-- `loja`. O que faltava era (a) dizer a que unidade cada loja pertence, (b) ter saldo
-- inicial por unidade, e (c) fazer a transferência CREDITAR o destino em vez de só
-- debitar a origem.
CREATE TABLE IF NOT EXISTS unidades (
    nome        VARCHAR(40) PRIMARY KEY,
    -- Rótulo que o SGI usa em movimentacao_transferencia.destino para esta unidade
    -- ('PORTEIRA PIATA'). NULL na matriz, que hoje nunca é destino de transferência.
    destino_sgi VARCHAR(40) UNIQUE,
    ordem       INTEGER NOT NULL DEFAULT 0,  -- ordem de exibição no painel
    -- TRUE na unidade que herda produtos.saldo_inicial quando não há linha própria em
    -- saldo_inicial_unidade. É o que evita MIGRAR os dados de produção: a matriz
    -- segue lendo o saldo inicial de onde sempre leu (a tela de produtos continua
    -- valendo), e só as unidades novas precisam da contagem própria. Sem isto, o
    -- saldo da matriz zeraria no dia em que esta view subisse - os testes de banco
    -- pegaram exatamente isso (saldo 16 virou -9).
    usa_saldo_do_produto BOOLEAN NOT NULL DEFAULT FALSE
);
INSERT INTO unidades (nome, destino_sgi, ordem, usa_saldo_do_produto) VALUES
    ('Barra da Estiva', NULL, 1, TRUE),
    ('Piatã', 'PORTEIRA PIATA', 2, FALSE)
ON CONFLICT (nome) DO NOTHING;
-- Coluna acrescentada depois do primeiro deploy desta secao: ALTER para quem ja criou.
ALTER TABLE unidades ADD COLUMN IF NOT EXISTS usa_saldo_do_produto BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE unidades SET usa_saldo_do_produto = TRUE
 WHERE nome = 'Barra da Estiva' AND usa_saldo_do_produto IS DISTINCT FROM TRUE;

-- De que unidade é cada loja dos movimentos (movimentacao_saida.loja,
-- movimentacao_entrada_compra.loja). Tabela, e não constante no código, porque o saldo
-- é calculado na VIEW: o mapa precisa existir dentro do banco.
CREATE TABLE IF NOT EXISTS unidades_loja (
    loja            VARCHAR(40) PRIMARY KEY,
    unidade_estoque VARCHAR(40) NOT NULL REFERENCES unidades (nome) ON UPDATE CASCADE
);
INSERT INTO unidades_loja (loja, unidade_estoque) VALUES
    ('Porteira', 'Barra da Estiva'),
    ('Casa de Adubo', 'Barra da Estiva'),
    ('Piatã', 'Piatã')
ON CONFLICT (loja) DO NOTHING;

-- Saldo inicial POR UNIDADE: a contagem física que serve de marco zero de cada
-- estoque. Barra da Estiva contou em 22/09/2026; Piatã tem contagem própria, em outra
-- data - e é por isso que a data também é por unidade, não uma só do sistema.
--
-- Substitui produtos.saldo_inicial / produtos.data_saldo_inicial, que valiam quando
-- havia um estoque só. Aquelas colunas continuam na tabela e são MIGRADAS para cá como
-- a linha de 'Barra da Estiva' (ver db.garantir_schema_unidades), mas quem manda no
-- saldo passa a ser esta tabela.
CREATE TABLE IF NOT EXISTS saldo_inicial_unidade (
    cod_produto        VARCHAR(10)   NOT NULL REFERENCES produtos (cod_produto) ON DELETE CASCADE,
    unidade_estoque    VARCHAR(40)   NOT NULL REFERENCES unidades (nome) ON UPDATE CASCADE,
    saldo_inicial      NUMERIC(14,3) NOT NULL DEFAULT 0,
    data_saldo_inicial DATE          NOT NULL,
    atualizado_em      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (cod_produto, unidade_estoque)
);

-- Ajuste manual pertence a uma unidade (uma perda em Piatã não pode descontar da
-- matriz). DEFAULT cobre os lançamentos que já existiam, todos de Barra da Estiva.
ALTER TABLE ajustes
    ADD COLUMN IF NOT EXISTS unidade_estoque VARCHAR(40) NOT NULL DEFAULT 'Barra da Estiva';

-- De onde a transferência saiu. Hoje é sempre a matriz; a coluna existe para o dia em
-- que houver Piatã -> matriz, e para a view não ter a origem escrita na pedra.
-- Fora da PK de propósito: a chave (cod, data, destino) já garante a idempotência do
-- robô, e duas origens para o mesmo destino no mesmo dia não existem na operação.
ALTER TABLE movimentacao_transferencia
    ADD COLUMN IF NOT EXISTS origem VARCHAR(40) NOT NULL DEFAULT 'Barra da Estiva';

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
-- Colunas acrescentadas depois, para os robôs do SGI desktop (compras e
-- transferências), em ALTER e não dentro do CREATE acima: a tabela já existe em
-- produção, e `CREATE TABLE IF NOT EXISTS` não altera tabela existente - mudar só o
-- CREATE deixaria o banco antigo sem as colunas e os robôs quebrando no INSERT.
--
-- `periodo_fim` não é só informativo: é o ponto de partida do robô de TRANSFERÊNCIAS.
-- Ele grava por período (apaga o intervalo e regrava), então a próxima execução começa
-- no maior `periodo_fim` com status 'ok' menos a janela de dias recentes. Antes disso o
-- critério era a data da última transferência gravada, que não avançava em semana
-- parada e fazia a consulta no SGI crescer sem motivo. Compras não usa isto para
-- decidir o que buscar - lá sync_dias_compras é dia a dia, mais preciso -, só registra.
ALTER TABLE sync_execucoes ADD COLUMN IF NOT EXISTS periodo_inicio DATE;
ALTER TABLE sync_execucoes ADD COLUMN IF NOT EXISTS periodo_fim    DATE;
ALTER TABLE sync_execucoes ADD COLUMN IF NOT EXISTS dias           INTEGER;
ALTER TABLE sync_execucoes ADD COLUMN IF NOT EXISTS linhas         INTEGER;
CREATE INDEX IF NOT EXISTS ix_sync_execucoes_origem
    ON sync_execucoes (origem, iniciado_em DESC);

-- Saldo POR UNIDADE DE ESTOQUE. É a view-base: v_saldo_produto (o consolidado, logo
-- abaixo) é a soma desta.
--
-- Regras, todas as três deliberadas:
--   1. Só conta movimento POSTERIOR à data_saldo_inicial DAQUELA unidade. Cada unidade
--      contou o estoque num dia diferente, então o corte é por unidade - usar uma data
--      global faria o estoque de Piatã contar vendas anteriores à própria contagem.
--   2. A transferência DEBITA a origem e CREDITA o destino. Antes ela só debitava,
--      porque Piatã não existia como estoque aqui: o produto simplesmente sumia.
--   3. O produto aparece em TODA unidade cadastrada, mesmo sem saldo inicial lá
--      (CROSS JOIN) - senão um item transferido para Piatã antes de ser contado lá não
--      teria linha nenhuma, e a transferência sumiria do consolidado.
-- DROP + CREATE, e não CREATE OR REPLACE: o REPLACE recusa qualquer mudança de TIPO
-- de coluna, e basta um MAX(varchar) virar text para ele falhar. Quando isso acontece
-- dentro do init_schema, a view fica com a definição ANTIGA sem ninguém perceber -
-- aconteceu no teste de upgrade desta versão, e a consolidada teria continuado sem
-- Piatã. A consolidada é dropada primeiro porque depende da por-unidade. Nada mais
-- depende das duas (conferido), então recriar é seguro.
DROP VIEW IF EXISTS v_saldo_produto;
DROP VIEW IF EXISTS v_saldo_produto_unidade;
CREATE VIEW v_saldo_produto_unidade AS
SELECT
    p.cod_produto,
    u.nome                                                      AS unidade_estoque,
    p.descricao,
    p.unidade,                       -- unidade de MEDIDA (UN, KG) - ver nota no schema
    COALESCE(si.saldo_inicial,
             CASE WHEN u.usa_saldo_do_produto THEN p.saldo_inicial ELSE 0 END)
                                                                AS saldo_inicial,
    COALESCE(si.data_saldo_inicial, p.data_saldo_inicial)       AS data_saldo_inicial,
    COALESCE(a.total, 0)                                        AS ajustes_total,
    COALESCE(s.qtd, 0)                                          AS saidas_total,
    COALESCE(s.valor, 0)                                        AS valor_saidas_total,
    COALESCE(ec.qtd, 0)                                         AS entradas_compras_total,
    COALESCE(ec.valor, 0)                                       AS valor_entradas_compras_total,
    COALESCE(ts.qtd, 0)                                         AS transferencias_saida_total,
    COALESCE(ts.valor, 0)                                       AS valor_transferencias_saida_total,
    COALESCE(te.qtd, 0)                                         AS transferencias_entrada_total,
    COALESCE(te.valor, 0)                                       AS valor_transferencias_entrada_total,
    COALESCE(si.saldo_inicial,
             CASE WHEN u.usa_saldo_do_produto THEN p.saldo_inicial ELSE 0 END)
        + COALESCE(a.total, 0) - COALESCE(s.qtd, 0)
        + COALESCE(ec.qtd, 0) - COALESCE(ts.qtd, 0) + COALESCE(te.qtd, 0)
                                                                AS saldo_atual,
    p.preco_custo,
    p.preco_venda,
    p.lead_time_dias,
    p.estoque_minimo,
    p.fornecedor,
    p.observacao,
    p.ativo
FROM produtos p
CROSS JOIN unidades u
LEFT JOIN saldo_inicial_unidade si
       ON si.cod_produto = p.cod_produto AND si.unidade_estoque = u.nome
-- Corte por unidade: COALESCE cobre produto ainda sem contagem naquela unidade.
LEFT JOIN LATERAL (
    SELECT COALESCE(si.data_saldo_inicial, p.data_saldo_inicial) AS d
) corte ON TRUE
LEFT JOIN LATERAL (
    SELECT SUM(m.quantidade_saida) AS qtd, SUM(m.valor_saida) AS valor
    FROM movimentacao_saida m
    JOIN unidades_loja ul ON ul.loja = m.loja
    WHERE m.cod_produto = p.cod_produto AND ul.unidade_estoque = u.nome
      AND m.data > corte.d
) s ON TRUE
LEFT JOIN LATERAL (
    SELECT SUM(m.quantidade_entrada) AS qtd, SUM(m.valor_entrada) AS valor
    FROM movimentacao_entrada_compra m
    JOIN unidades_loja ul ON ul.loja = m.loja
    WHERE m.cod_produto = p.cod_produto AND ul.unidade_estoque = u.nome
      AND m.data > corte.d
) ec ON TRUE
LEFT JOIN LATERAL (
    SELECT SUM(m.quantidade_transferida) AS qtd, SUM(m.valor_transferido) AS valor
    FROM movimentacao_transferencia m
    WHERE m.cod_produto = p.cod_produto AND m.origem = u.nome
      AND m.data > corte.d
) ts ON TRUE
LEFT JOIN LATERAL (
    SELECT SUM(m.quantidade_transferida) AS qtd, SUM(m.valor_transferido) AS valor
    FROM movimentacao_transferencia m
    WHERE m.cod_produto = p.cod_produto AND m.destino = u.destino_sgi
      AND m.data > corte.d
) te ON TRUE
LEFT JOIN LATERAL (
    SELECT SUM(j.quantidade) AS total
    FROM ajustes j
    WHERE j.cod_produto = p.cod_produto AND j.unidade_estoque = u.nome
      AND j.data > corte.d
) a ON TRUE;

-- Consolidado das unidades, com as MESMAS colunas de antes: é o que o painel já
-- consome. Antes era "o estoque" (só existia um); agora é a soma das unidades.
-- transferencias_total continua sendo só a SAÍDA, para não mudar o sentido da coluna
-- em quem já a lê; no consolidado as duas pontas se anulam no saldo de qualquer jeito.
CREATE VIEW v_saldo_produto AS
SELECT
    v.cod_produto,
    MAX(v.descricao)                        AS descricao,
    MAX(v.unidade)::VARCHAR(20)             AS unidade,
    SUM(v.saldo_inicial)                    AS saldo_inicial,
    MIN(v.data_saldo_inicial)               AS data_saldo_inicial,
    SUM(v.ajustes_total)                    AS ajustes_total,
    SUM(v.saidas_total)                     AS saidas_total,
    SUM(v.valor_saidas_total)               AS valor_saidas_total,
    SUM(v.saldo_atual)                      AS saldo_atual,
    MAX(v.preco_custo)                      AS preco_custo,
    MAX(v.preco_venda)                      AS preco_venda,
    MAX(v.lead_time_dias)                   AS lead_time_dias,
    MAX(v.estoque_minimo)                   AS estoque_minimo,
    MAX(v.fornecedor)                       AS fornecedor,
    MAX(v.observacao)                       AS observacao,
    BOOL_OR(v.ativo)                        AS ativo,
    SUM(v.entradas_compras_total)           AS entradas_compras_total,
    SUM(v.valor_entradas_compras_total)     AS valor_entradas_compras_total,
    SUM(v.transferencias_saida_total)       AS transferencias_total,
    SUM(v.valor_transferencias_saida_total) AS valor_transferencias_total
FROM v_saldo_produto_unidade v
GROUP BY v.cod_produto;

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
