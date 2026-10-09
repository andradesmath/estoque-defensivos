# Estoque de Defensivos

Controle de estoque do grupo DEFENSIVOS (Porteira Agrocomercial + Casa de Adubos Café Bom).
Baixa automática a partir do relatório SGI "Totais de Vendas Por Produto" (GRUPOS = DEFENSIVOS),
base = contagem física do fim de 21/09/2026, período de baixa a partir de 22/09/2026.

Repositório **separado** do painel de vendedores. Use repositório **privado** (os artefatos de
debug do Actions contêm dados de venda).

## Como funciona

```
SGI (Playwright, GitHub Actions)  ->  PDF por dia/loja  ->  parser + validação  ->  Postgres  ->  Streamlit
```

Princípio central: **o saldo nunca é um contador armazenado.**

    saldo_atual = saldo_inicial + Σ ajustes − Σ saídas      (só movimentos com data > data_saldo_inicial)

A contagem vale para o **fim** do dia `data_saldo_inicial`; por isso 21/09 fica de fora e 22/09 entra.

### Idempotência (o requisito crítico)

- `movimentacao_saida` tem PK `(cod_produto, data, loja)`.
- Cada sync de um dia **sobrescreve** o dia (UPSERT), apaga códigos que sumiram do relatório
  daquele dia e registra o dia em `sync_dias` — tudo em uma transação.
- Rodar o sync N vezes, com janelas sobrepostas, dá o mesmo saldo.
- Relatório vazio em dia que já tem vendas é recusado (`--permitir-zerar` para forçar).

### Por que um PDF por dia por loja

O PDF do SGI é agregado no período (não traz data por linha). Para ter saída diária, pede-se
o período [dia, dia]. Cada execução refaz uma janela móvel (padrão 3 dias) + dias faltantes
(`sync_dias`). Trade-off: mais requisições ao SGI, em troca de idempotência e correção
retroativa (venda lançada/cancelada com atraso).

### Validações do PDF (`parser_sgi.validar`)

- período do PDF == dia pedido (protege contra o bug conhecido do datepicker);
- Grupos == {DEFENSIVOS};
- soma dos itens == "Total:" impresso (tolerância R$ 0,05; o PDF real tem 1 centavo de diferença);
- nenhum registro descartado pelo parser;
- PDF longo sem itens não é tratado como dia vazio.

Falha em um dia/loja não derruba os outros (isolamento por dia).

## Telas do painel

Visão geral · Saldo por produto · Produtos (adicionar, editar, remover/inativar, parâmetros em
massa) · Movimentar estoque (contagem, entrada, transferência, perda + histórico) · Indicadores
(Giro e ROI, Reposição, Curva ABC, Estoque parado) · Histórico de saídas · Não encontrados
(cadastrar ou ignorar) · Importar base · Sincronização (botão "Sincronizar agora").

### Indicadores (`estoque/kpis.py`, pandas puro)

| Indicador | Fórmula |
|---|---|
| venda média/dia | Σ saídas no período ÷ dias do período |
| cobertura (dias) | saldo ÷ venda média/dia |
| CMV | Σ saídas × preço de custo |
| receita | Σ `valor_saida` do SGI (valor real vendido) |
| lucro bruto | receita − CMV |
| giro | CMV ÷ estoque médio a custo (período e anualizado) |
| dias de giro | dias do período ÷ giro |
| ROI / GMROI | lucro bruto ÷ estoque médio a custo (período e anualizado) |
| ponto de pedido | venda/dia × lead time + estoque mínimo |
| sugestão de compra | max(0, ponto de pedido − saldo) |
| curva ABC | 80% / 95% da receita acumulada |
| capital parado | saldo × custo de itens sem giro/excesso |

Com menos de 15 dias de histórico o painel mostra valores do período, não anualizados
(anualização linear de janela curta distorce). Produto sem preço de custo fica fora dos
indicadores monetários.

## Setup

### 1. Banco

Postgres novo (Neon, Supabase ou outro). Não reutilize o banco dos vendedores.
O schema é criado sozinho no primeiro start (`db.init_schema()`, idempotente).

### 2. GitHub Secrets (Settings > Secrets and variables > Actions)

`DATABASE_URL`, `SGI_URL_LOGIN`, `SGI_LOGIN`, `SGI_SENHA`, `SGI_EMPRESA_PORTEIRA`,
`SGI_EMPRESA_CASA_ADUBO`. Mesmos valores do sistema de vendedores.
Variável opcional (Variables): `SGI_OPERACOES` (padrão `V`).

Cron: 12h e 19h BRT (15:00 e 22:00 UTC). `workflow_dispatch` aceita loja, data, forçar tudo, dry-run.

### 3. Streamlit (Secrets)

`DATABASE_URL`, `GITHUB_TOKEN` (fine-grained, só este repo, permissão *Actions: read and write*),
`GITHUB_REPO` (`dono/repo`), `APP_SENHA` (recomendado: o painel mostra custos e margens).

### 4. Importar a base

```bash
cp .env.example .env   # preencher DATABASE_URL
pip install -r requirements.txt
python scripts/importar_base.py tests/fixtures/base_defensivos_21_09.xlsx --data-contagem 2026-09-21 --validar-apenas
python scripts/importar_base.py tests/fixtures/base_defensivos_21_09.xlsx --data-contagem 2026-09-21
```

Ou pela tela "Importar base". A aba "Removidos" vira lista de códigos ignorados.

### 5. Primeiro teste ao vivo (obrigatório antes do cron)

A navegação no SGI real **não foi testada** (sem acesso ao SGI); os seletores vêm do HTML real
salvo e foram exercitados em navegador offline. Rode na sua máquina:

```bash
pip install -r requirements-sync.txt && playwright install chromium
python scripts/sync_sgi.py --loja Porteira --data 22/09/2026 --dry-run --headed
```

`--dry-run` baixa e valida sem gravar. Se passar nas duas lojas, rode sem `--dry-run` e depois
ative o cron. Em falha, o workflow sobe screenshot/HTML/PDF como artefato (7 dias).

Flags: `--loja`, `--data DD/MM/AAAA`, `--desde`, `--janela N`, `--forcar-tudo`,
`--permitir-zerar`, `--dry-run`, `--headed`.

### 6. Robô de COMPRAS (SGI desktop) e o agendamento ao ligar o notebook

As **entradas por compra** só existem no SGI **desktop** (não no portal web), e o banco do SGI
não aceita conexão direta — então a única automação possível é dirigir a interface do próprio
`SGI.exe`, nesta máquina, com `scripts/sync_compras_sgi.py`. Ele abre o SGI, loga, escolhe a
empresa **PORTEIRA AGROCOMERCIAL**, abre *Relatórios > Compras > Relação de Custo de Compras*,
percorre dia a dia, exporta o `.xls` e grava em `movimentacao_entrada_compra` (upsert por
`cod_produto + data + loja`, então rodar de novo não soma duas vezes).

Requisitos: **Python 32 bits** (mesma arquitetura do SGI.exe) e
`py -3.11-32 -m pip install -r requirements-local-robo-compras.txt`. No `.env` desta máquina:
`SGI_SENHA_DESKTOP` (a senha do desktop é diferente da do portal web, que fica em `SGI_SENHA`)
e `DATABASE_URL=postgresql+pg8000://...`.

```bash
py -3.11-32 scripts\sync_compras_sgi.py --dry-run    # mostra o que entraria, sem gravar
py -3.11-32 scripts\sync_compras_sgi.py              # grava
```

**Enquanto roda, não use o computador**: a automação depende de cliques e teclas reais, e o
robô põe a janela do SGI à frente (desfaz isso ao terminar). Se algo passar na frente, ele
**não clica** — para com erro, em vez de clicar no programa errado.

**Trava de empresa**: antes de buscar qualquer dado, o robô lê a faixa *"Licenciado para ..."*
do rodapé do SGI e só segue se for a empresa esperada; se não for, desloga e tenta de novo
com outro jeito de escolher a empresa. Isso existe porque uma execução chegou a rodar 18 dias
inteiros logada na empresa errada (vinha tudo vazio).

**Aviso de contagem em dobro**: o saldo soma ajustes manuais **e** entradas por compra. Se um
produto monitorado já tiver ajuste de entrada lançado à mão no mesmo dia, o robô imprime
`!! ATENCAO` com o número do ajuste — confira antes de gravar.

#### Agendamento (gatilho "ao fazer logon")

`scripts/sync_compras_sgi_logon.bat` é o que a tarefa chama: fixa a pasta do projeto e guarda
a saída em `logs/compras_AAAA-MM-DD.log`. Não leva data: o robô consulta `sync_dias_compras`
e busca sozinho os dias que faltam — **os dias em que o notebook ficou desligado entram no
próximo logon**.

Criar a tarefa (PowerShell **como administrador**, uma vez só):

```powershell
schtasks /create /tn "Estoque - Compras SGI (logon)" ^
  /tr "C:\Users\Admin\Documents\estoque-defensivos\estoque-defensivos\scripts\sync_compras_sgi_logon.bat" ^
  /sc onlogon /delay 0002:00 /rl highest /f
```

`/delay 0002:00` espera 2 minutos após o logon (dá tempo de a rede subir). Para conferir,
rodar na hora ou remover:

```powershell
schtasks /query /tn "Estoque - Compras SGI (logon)" /v /fo list
schtasks /run   /tn "Estoque - Compras SGI (logon)"
schtasks /delete /tn "Estoque - Compras SGI (logon)" /f
```

A tarefa dispara em **todo** logon; como o robô só busca o que falta, nos logons seguintes do
mesmo dia ele não tem o que fazer e sai rápido.

## Testes

```bash
pip install -r requirements-dev.txt
(cd tests/fixtures && npm install)        # jQuery/Bootstrap/datepicker reais, p/ teste de navegador
playwright install chromium
export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/estoque_test   # nome precisa conter "test"
pytest -q
```

A fixture recria o schema `public` do banco de teste: nunca aponte para banco real.
Sem `TEST_DATABASE_URL`, os testes de banco são pulados. 78 testes cobrem parser (PDF real),
importação, KPIs (valores calculados à mão), idempotência/UPSERT/zerar em Postgres real,
orquestração do sync, navegação Playwright offline e o app (`streamlit.testing`).

## Decisões e limites

- **Operações = só venda (V).** Bonificação/troca (B/T) também movem estoque físico. Decisão sua:
  ajuste `SGI_OPERACOES` (ex.: `V,B`) depois de confirmar como o SGI reporta.
- **Entradas e transferências (Piatã) são manuais** (tela Movimentar estoque). Sem isso o saldo
  diverge. Achado com dados reais: 3 produtos ficariam negativos (09490, 07014, 03939), todos com
  contagem 0 e venda posterior — compra não registrada ou contagem errada.
- **Estoque único** para as duas lojas (não há saldo por loja).
- **Lead time** (`lead_time_dias`) é informado por produto; "tempo de giro" é calculado.
- Anualização linear; estoque médio a partir do saldo diário reconstruído.
- Sem coluna de data por linha no PDF: correção retroativa só via re-sync da janela.

## Extensões possíveis

- Importar NFs de entrada do SGI (elimina lançamento manual de compras).
- Pedido/recebimento de transferência com controle solicitado × efetivo.
- Saldo por loja; lotes e validade.
- Alerta por e-mail/WhatsApp de ruptura.
