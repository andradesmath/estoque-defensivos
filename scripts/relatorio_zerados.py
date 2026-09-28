"""
scripts/relatorio_zerados.py - Vendas por loja e detalhe dos produtos com saldo <= 0.

    python scripts/relatorio_zerados.py
"""
import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(RAIZ, ".env"))
except ImportError:
    pass

from estoque import db  # noqa: E402


def main() -> None:
    print("=== Vendas por loja (todo o período sincronizado) ===")
    mov = db.listar_movimentacao()
    if mov.empty:
        print("Nenhuma venda sincronizada ainda.")
        return
    resumo = mov.groupby("loja")[["quantidade_saida", "valor_saida"]].sum()
    for loja, row in resumo.iterrows():
        print(f"{loja}: {row['quantidade_saida']:.0f} un. · R$ {row['valor_saida']:.2f}")

    print("\n=== Produtos com saldo atual <= 0 ===")
    saldos = db.listar_saldos(apenas_ativos=False)
    zerados = saldos[saldos["saldo_atual"] <= 0].sort_values("saldo_atual")
    if zerados.empty:
        print("Nenhum produto zerado ou negativo.")
        return
    for _, p in zerados.iterrows():
        print(f"\n{p['cod_produto']} - {p['descricao']} | saldo atual: {p['saldo_atual']:.0f} "
              f"| saldo inicial (21/09): {p['saldo_inicial']:.0f} | vendido total: {p['saidas_total']:.0f} un.")
        detalhe = mov[mov["cod_produto"] == p["cod_produto"]].sort_values("data")
        for _, d in detalhe.iterrows():
            print(f"    {d['data']} | {d['loja']} | vendeu {d['quantidade_saida']:.0f} un. "
                  f"| R$ {d['valor_saida']:.2f}")

    print(f"\nTotal de produtos com saldo <= 0: {len(zerados)}")


if __name__ == "__main__":
    main()
