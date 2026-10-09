"""Diagnostico: por que o saldo nao refletiu uma entrada por compra.

Checa, para um produto (padrao 08477 JOINER), os quatro pontos onde a entrada pode
se perder entre a tabela e o saldo do painel:
  1. a linha existe mesmo em movimentacao_entrada_compra?
  2. a view v_saldo_produto ja tem as colunas de compras (se o banco ficou com a
     versao antiga da view, a entrada nunca entra na conta);
  3. a data da compra e POSTERIOR a data_saldo_inicial do produto? A view so conta
     movimentos depois da contagem fisica - uma compra no mesmo dia ou antes dela
     fica de fora de proposito;
  4. o que a view devolve hoje.

So LE - nao altera nada.

Uso:  py -3.11-32 scripts\\debug_saldo_compras.py [cod_produto]
"""
from __future__ import annotations

import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(RAIZ, ".env"))
except ImportError:
    pass

from sqlalchemy import text  # noqa: E402

from estoque.db import get_engine  # noqa: E402

COD = (sys.argv[1] if len(sys.argv) > 1 else "08477").strip()


def main() -> int:
    with get_engine().connect() as c:
        print(f"=== PRODUTO {COD} ===")
        p = c.execute(text("""SELECT cod_produto, descricao, saldo_inicial,
                                     data_saldo_inicial, ativo
                              FROM produtos WHERE cod_produto = :c"""), {"c": COD}).fetchone()
        if not p:
            print("  NAO esta cadastrado em produtos - nao aparece no painel nem tem saldo.")
            return 1
        print(f"  descricao          : {p[1]}")
        print(f"  saldo_inicial      : {p[2]}")
        print(f"  data_saldo_inicial : {p[3]}   <- a view so conta movimento DEPOIS desta data")
        print(f"  ativo              : {p[4]}")

        print("\n=== ENTRADAS POR COMPRA GRAVADAS ===")
        ents = c.execute(text("""SELECT data, loja, quantidade_entrada, valor_entrada
                                 FROM movimentacao_entrada_compra
                                 WHERE cod_produto = :c ORDER BY data"""), {"c": COD}).fetchall()
        if not ents:
            print("  nenhuma")
        for e in ents:
            conta = "CONTA" if e[0] > p[3] else "IGNORADA (data <= data_saldo_inicial)"
            print(f"  {e[0]}  {e[1]:<10} qtd={e[2]}  valor={e[3]}   -> {conta}")

        print("\n=== AJUSTES MANUAIS ===")
        ajs = c.execute(text("""SELECT id, data, tipo, quantidade, observacao
                                FROM ajustes WHERE cod_produto = :c ORDER BY data"""),
                        {"c": COD}).fetchall()
        if not ajs:
            print("  nenhum")
        for a in ajs:
            print(f"  #{a[0]}  {a[1]}  {a[2]:<22} qtd={a[3]}  {a[4] or ''}")

        print("\n=== SAIDAS (VENDAS) ===")
        sai = c.execute(text("""SELECT COALESCE(SUM(quantidade_saida), 0)
                                FROM movimentacao_saida m
                                WHERE m.cod_produto = :c AND m.data > :d"""),
                        {"c": COD, "d": p[3]}).fetchone()
        print(f"  total depois de {p[3]}: {sai[0]}")

        print("\n=== A VIEW v_saldo_produto TEM AS COLUNAS DE COMPRAS? ===")
        cols = [r[0] for r in c.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'v_saldo_produto' ORDER BY ordinal_position""")).fetchall()]
        tem = "entradas_compras_total" in cols
        print(f"  colunas: {', '.join(cols)}")
        if tem:
            print("  OK - a view conhece as entradas por compra.")
        else:
            print("  *** NAO *** - o banco esta com a VERSAO ANTIGA da view: ela nao soma")
            print("      movimentacao_entrada_compra, entao a compra nunca entra no saldo.")
            print("      Corrigir rodando o init_schema (ver instrucoes).")

        print("\n=== O QUE A VIEW DEVOLVE HOJE ===")
        v = c.execute(text("SELECT * FROM v_saldo_produto WHERE cod_produto = :c"),
                      {"c": COD}).mappings().fetchone()
        for k, val in (v or {}).items():
            print(f"  {k:<30} {val}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
