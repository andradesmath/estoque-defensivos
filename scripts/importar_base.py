"""
scripts/importar_base.py - Importa a planilha da base de produtos pela linha de comando
(mesma validação e mesmas regras do painel: Importar base).

    python scripts/importar_base.py tests/fixtures/base_defensivos_21_09.xlsx --data-contagem 2026-09-21
    python scripts/importar_base.py planilha.xlsx --data-contagem 2026-09-21 --modo atualizar
    python scripts/importar_base.py planilha.xlsx --data-contagem 2026-09-21 --validar-apenas

Por padrão registra também os códigos da aba "Removidos…" como ignorados
(--sem-ignorados desliga). Use --validar-apenas para ver erros/avisos sem tocar no banco.
"""
import argparse
import os
import sys
from datetime import datetime

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(RAIZ, ".env"))
except ImportError:
    pass

from estoque.importacao import validar_planilha  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("arquivo")
    ap.add_argument("--data-contagem", required=True, help="AAAA-MM-DD: fim do dia da contagem física.")
    ap.add_argument("--modo", choices=["novos", "atualizar"], default="novos")
    ap.add_argument("--sem-ignorados", action="store_true")
    ap.add_argument("--validar-apenas", action="store_true")
    args = ap.parse_args(argv)

    data = datetime.strptime(args.data_contagem, "%Y-%m-%d").date()
    with open(args.arquivo, "rb") as f:
        res = validar_planilha(f.read(), os.path.basename(args.arquivo))

    print(f"Aba lida: {res.aba or '(csv)'} · {len(res.df)} produto(s) válido(s) · {len(res.ignorados)} ignorado(s) na planilha")
    for e in res.erros:
        print("ERRO :", e)
    for a in res.avisos:
        print("AVISO:", a)
    if not res.ok:
        return 1
    if args.validar_apenas:
        print("Validação OK (nada gravado).")
        return 0

    from estoque import db
    db.init_schema()
    out = db.importar_produtos(res.df, args.modo, data)
    print(f"Inseridos: {out['inseridos']} · Atualizados: {out['atualizados']} · Mantidos: {out['ignorados']}")
    if not args.sem_ignorados:
        for _, r in res.ignorados.iterrows():
            db.ignorar_codigo(r["cod_produto"], r["descricao"], r["motivo"])
        print(f"Ignorados registrados: {len(res.ignorados)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
