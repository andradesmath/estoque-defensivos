"""
scripts/checar_unidades.py - Diz se o banco já tem a estrutura de UNIDADES e, se não
tiver, aplica. Resolve a pergunta "por que o painel só mostra Consolidado?" sem
depender do Streamlit.

POR QUE existe: o painel aplica o schema sozinho (db.aplicar_schema_por_partes), mas
isso só acontece quando o processo dele roda o código novo - e o Streamlit Cloud às
vezes recarrega o arquivo sem reiniciar o processo, deixando painel novo com banco
velho. Daqui a checagem é direta e não depende de nada disso.

Uso (Python 32-bit, mesma máquina do robô, na pasta do projeto):
    py -3.11-32 scripts\\checar_unidades.py            # só confere e mostra
    py -3.11-32 scripts\\checar_unidades.py --aplicar  # confere e CRIA o que faltar
"""
from __future__ import annotations

import argparse
import os
import sys

RAIZ_PROJETO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_PROJETO)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(RAIZ_PROJETO, ".env"))
except ImportError:
    pass

from sqlalchemy import text  # noqa: E402

from estoque import db  # noqa: E402

OBJETOS = [
    ("tabela", "unidades"),
    ("tabela", "unidades_loja"),
    ("tabela", "saldo_inicial_unidade"),
    ("view", "v_saldo_produto_unidade"),
    ("view", "v_saldo_produto"),
]


def _existe(conn, nome: str) -> bool:
    return conn.execute(text("SELECT to_regclass(:n)"), {"n": nome}).scalar() is not None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Confere (e cria) a estrutura de unidades no banco.")
    ap.add_argument("--aplicar", action="store_true",
                    help="Aplica o schema.sql comando a comando para criar o que faltar.")
    args = ap.parse_args(argv)

    with db.get_engine().begin() as conn:
        faltando = [n for _t, n in OBJETOS if not _existe(conn, n)]
        for tipo, nome in OBJETOS:
            print(f"  {tipo:7} {nome:28} {'OK' if nome not in faltando else 'NAO EXISTE'}")
        tem_coluna = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
             WHERE table_name = 'ajustes' AND column_name = 'unidade_estoque'""")).scalar()
        print(f"  coluna  ajustes.unidade_estoque      {'OK' if tem_coluna else 'NAO EXISTE'}")

    if not faltando and tem_coluna:
        with db.get_engine().begin() as conn:
            linhas = conn.execute(text(
                "SELECT nome, COALESCE(destino_sgi, '-') FROM unidades ORDER BY ordem")).all()
        print("\nUnidades cadastradas:")
        for nome, destino in linhas:
            print(f"  - {nome}  (destino no SGI: {destino})")
        print("\nO banco esta completo. Se o painel ainda mostra so 'Consolidado', o "
              "processo do Streamlit esta com o codigo/cache antigo: use 'Reboot app'.")
        return 0

    print(f"\nFaltando: {', '.join(faltando) or 'coluna ajustes.unidade_estoque'}")
    if not args.aplicar:
        print("Rode de novo com --aplicar para criar. Todos os comandos sao idempotentes.")
        return 1

    n = db.aplicar_schema_por_partes()
    print(f"Schema aplicado ({n} comando(s)).")
    with db.get_engine().begin() as conn:
        ainda = [nome for _t, nome in OBJETOS if not _existe(conn, nome)]
    if ainda:
        print(f"ERRO: mesmo depois de aplicar, falta: {', '.join(ainda)}")
        return 1
    print("Pronto. Recarregue o painel (e, se precisar, use 'Reboot app').")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
