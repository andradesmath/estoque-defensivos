"""
scripts/importar_contagem_unidade.py - Importa uma CONTAGEM FÍSICA como saldo inicial
de uma unidade de estoque (ver a seção UNIDADES no schema.sql).

É assim que uma unidade nova entra no controle: antes da contagem, o painel mostra só o
fluxo (o que entrou menos o que saiu) e avisa que aquilo não é o estoque. Depois dela, o
número contado vira o marco zero e tudo que se movimentar DEPOIS da data é somado em
cima - movimento anterior é ignorado, porque a contagem já o incorpora.

O CSV tem `descricao;quantidade` (a terceira coluna, `pagina`, é só rastreabilidade do
documento de origem). A descrição é casada com o cadastro por estoque/match_produto.py,
o mesmo matcher usado na entrada por nota fiscal - produto que não casar com confiança
é LISTADO e não entra, nunca chutado.

Uso:
    py -3.11-32 scripts\\importar_contagem_unidade.py dados\\contagem_piata_2026-10-10.csv ^
        --unidade "Piatã" --data 10/10/2026                 # confere e mostra, nao grava
    py -3.11-32 scripts\\importar_contagem_unidade.py ... --gravar
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

RAIZ_PROJETO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_PROJETO)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(RAIZ_PROJETO, ".env"))
except ImportError:
    pass

from estoque import db, match_produto  # noqa: E402

# Abaixo disto o palpite do matcher não é confiável o bastante para virar saldo: a linha
# vai para a lista de "não casou" e a pessoa decide. Mesmo patamar usado na entrada por
# nota fiscal.
SCORE_MINIMO = 0.80

# Unidades "base" de venda, na ordem de preferência quando o nome não diz a
# apresentação. Confirmado pelo usuário em 10/10/2026: "o que não tiver especificação
# de litro, é de litro". ML e G ficam de fora de propósito - "ENGEO PLENO" é o de
# 1 litro, não o de 250 ml, mesmo o de 250 ml sendo o "menor".
UNIDADES_BASE = ("L", "KG")

# Quanto o nome do CADASTRO pode ser mais curto que o da planilha e ainda ser o mesmo
# produto. 2 caracteres cobrem erro de grafia ("VERDICT MAXX" x "VERDICT MAX") sem
# deixar passar produto diferente: "TORDON ULTRA-S" sobre "TORDON" sobra 6 e é outro
# defensivo - a primeira versão desta regra casou os dois e teria posto 2 unidades no
# produto errado.
SOBRA_MAXIMA = 2


def _desempatar_por_apresentacao(descricao: str, cadastro: dict, score_minimo: float):
    """Escolhe entre apresentações do MESMO produto quando o nome não diz qual.

    Só age quando (a) a descrição não traz tamanho nenhum e (b) os candidatos do topo
    são o mesmo produto em embalagens diferentes. Nesse caso fica com a de 1 litro (ou
    1 kg) - a regra que o usuário confirmou. Se nem isso resolver, devolve None e a
    linha continua pendente: o importador nunca chuta sozinho.

    Mora aqui, e não em match_produto, de propósito: na entrada por NOTA FISCAL o
    conservadorismo é o certo - lá um palpite errado vira entrada de estoque no produto
    errado, e quem digita está na frente da tela pra escolher."""
    if match_produto.tamanho(descricao):
        return None  # a descrição diz o tamanho; se não casou, não é ambiguidade de embalagem
    k_origem = match_produto.chave(descricao)
    topo = []
    for c, sc in match_produto.melhores(descricao, cadastro, n=6):
        if sc < score_minimo:
            continue
        k_cand = match_produto.chave(cadastro[c])
        # Candidato pode ser MAIS específico que a planilha (falta detalhe no
        # documento). O contrário não: palavra que só a planilha tem é sinal de produto
        # diferente, não de embalagem diferente.
        if k_cand.startswith(k_origem):
            topo.append((c, sc))
        elif k_origem.startswith(k_cand) and len(k_origem) - len(k_cand) <= SOBRA_MAXIMA:
            topo.append((c, sc))
    if len(topo) < 2:
        return None
    for unidade in UNIDADES_BASE:
        unitarios = [c for c, _sc in topo if match_produto.tamanho(cadastro[c]) == (1.0, unidade)]
        if len(unitarios) == 1:
            return unitarios[0]
    return None


def ler_csv(caminho: Path) -> list[dict]:
    linhas = []
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        for n, linha in enumerate(csv.DictReader(f, delimiter=";"), start=2):
            desc = (linha.get("descricao") or "").strip()
            if not desc:
                continue
            cod = (linha.get("cod_produto") or "").strip()
            bruto = (linha.get("quantidade") or "").strip().replace(",", ".")
            try:
                qtd = float(bruto)
            except ValueError:
                raise SystemExit(f"Linha {n}: quantidade ilegível ({bruto!r}) em {desc!r}.")
            linhas.append({"descricao": desc, "quantidade": qtd, "cod_produto": cod,
                           "pagina": (linha.get("pagina") or "").strip()})
    return linhas


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Importa contagem física como saldo inicial de uma unidade.")
    ap.add_argument("csv", help="Arquivo descricao;quantidade[;pagina].")
    ap.add_argument("--unidade", required=True, help='Nome exato da unidade (ex.: "Piatã").')
    ap.add_argument("--data", required=True, help="Data da contagem, DD/MM/AAAA.")
    ap.add_argument("--score-minimo", type=float, default=SCORE_MINIMO)
    ap.add_argument("--gravar", action="store_true", help="Grava. Sem isto, só mostra.")
    args = ap.parse_args(argv)

    dia = datetime.strptime(args.data, "%d/%m/%Y").date()
    linhas = ler_csv(Path(args.csv))

    try:
        db.garantir_schema_robos()
        unidades = {u["nome"] for u in db.listar_unidades()}
    except Exception as e:  # noqa: BLE001
        print(f"ERRO: não consegui ler as unidades ({e}). Rode scripts/checar_unidades.py --aplicar.")
        return 1
    if args.unidade not in unidades:
        print(f"ERRO: unidade {args.unidade!r} não existe. Cadastradas: {sorted(unidades)}")
        return 1

    cadastro = db.produtos_para_match()
    print(f"{len(linhas)} linha(s) na contagem; {len(cadastro)} produto(s) ativos no cadastro.\n")

    casados, duvidosos = [], []
    for l in linhas:
        # Código escrito à mão no CSV manda: é a forma de resolver o que o matcher não
        # consegue decidir sozinho, e não deve ser "corrigido" por palpite nenhum.
        if l["cod_produto"]:
            if l["cod_produto"] not in cadastro:
                print(f"AVISO: código {l['cod_produto']} do CSV ({l['descricao']}) não existe "
                      "no cadastro ativo - linha ignorada.")
                continue
            casados.append({**l, "cadastro": cadastro[l["cod_produto"]], "score": 1.0})
            continue
        achado = match_produto.sugerir_produto(l["descricao"], cadastro)
        if achado and achado[1] >= args.score_minimo:
            cod, score = achado
            casados.append({**l, "cod_produto": cod, "cadastro": cadastro[cod], "score": score})
            continue
        inferido = _desempatar_por_apresentacao(l["descricao"], cadastro, args.score_minimo)
        if inferido:
            casados.append({**l, "cod_produto": inferido, "cadastro": cadastro[inferido],
                            "score": 0.0, "inferido": True})
        else:
            # Mostra os candidatos em vez de só dizer que não achou: quase sempre o
            # produto EXISTE e o que falta é a apresentação ("VERDADERO" está no
            # cadastro como 1 KG e 5 KG). Com os códigos à vista, resolver é preencher
            # a coluna cod_produto no CSV.
            l["candidatos"] = [(c, cadastro[c], sc) for c, sc in
                               match_produto.melhores(l["descricao"], cadastro, n=3) if sc > 0.5]
            duvidosos.append(l)

    # Duas descrições diferentes que casem no MESMO produto seriam uma sobrescrevendo a
    # outra em silêncio - sempre erro de transcrição ou cadastro duplicado.
    vistos = {}
    for c in casados:
        vistos.setdefault(c["cod_produto"], []).append(c)
    repetidos = {cod: v for cod, v in vistos.items() if len(v) > 1}

    print(f"CASARAM ({len(casados)}):")
    for c in sorted(casados, key=lambda x: x["cadastro"]):
        if c.get("inferido"):
            marca, nota = "*", f"   (planilha: {c['descricao']} — apresentação inferida)"
        elif c["score"] >= 0.99:
            marca, nota = " ", ""
        else:
            marca, nota = "~", f"   (planilha: {c['descricao']}, score {c['score']:.2f})"
        print(f" {marca} {c['cod_produto']}  {c['cadastro'][:38]:<38} qtd={c['quantidade']:g}{nota}")
    inferidos = [c for c in casados if c.get("inferido")]
    if inferidos:
        print(f"\n * {len(inferidos)} com apresentação INFERIDA (nome sem tamanho -> a de 1 "
              "litro/kg). Confira estes antes de gravar.")

    if duvidosos:
        print(f"\nNÃO CASARAM ({len(duvidosos)}) - ficam de fora:")
        for d in duvidosos:
            print(f"   pág.{d['pagina']}  {d['descricao'][:40]:<40} qtd={d['quantidade']:g}")
            for cod, desc, sc in d["candidatos"]:
                print(f"        candidato: {cod}  {desc[:44]:<44} ({sc:.2f})")
            if not d["candidatos"]:
                print("        nenhum candidato parecido no cadastro")
        print("   Para incluir, preencha a coluna cod_produto no CSV com o código certo.")

    if repetidos:
        print(f"\n!! {len(repetidos)} produto(s) com DUAS linhas da planilha apontando pro mesmo código:")
        for cod, v in repetidos.items():
            print(f"   {cod} {cadastro[cod]}: " + " | ".join(f"{x['descricao']}={x['quantidade']:g}" for x in v))
        print("   Corrija o CSV antes de gravar - uma sobrescreveria a outra.")
        return 1

    if not args.gravar:
        print(f"\n[simulação] nada gravado. Para valer: repita com --gravar.")
        return 0

    for c in casados:
        db.definir_saldo_inicial_unidade(c["cod_produto"], args.unidade, c["quantidade"], dia)
    print(f"\nGravado: saldo inicial de {len(casados)} produto(s) em {args.unidade} "
          f"na data {dia:%d/%m/%Y}.")
    print("A partir de agora o saldo dessa unidade conta só o que se movimentar DEPOIS "
          "dessa data - o que veio antes já está dentro do número contado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
