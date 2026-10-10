from io import BytesIO

import xlwt

from estoque.parser_compras_sgi import extrair_compras, para_linhas_movimentacao


def _xls(colunas: list[str], linhas: list[list]) -> bytes:
    """Gera um .xls real (BIFF), igual ao que o SGI exporta — não um .xlsx disfarçado,
    porque extrair_compras agora lê com xlrd (só entende o formato antigo)."""
    livro = xlwt.Workbook()
    aba = livro.add_sheet("dados")
    for c, nome in enumerate(colunas):
        aba.write(0, c, nome)
    for r, linha in enumerate(linhas, start=1):
        for c, valor in enumerate(linha):
            aba.write(r, c, valor)
    buf = BytesIO()
    livro.save(buf)
    return buf.getvalue()


_COLUNAS = ["COD_PROD", "DESCRICAO", "COMPRA_TOTAL", "BONIFICACAO_TOTAL", "QTD_TOTAL",
            "IMPOSTOS_FEDERAIS", "VL_ICMS", "cl_preco_medio"]


def test_extrai_itens_com_quantidade_e_ignora_sem_quantidade():
    dados = _xls(_COLUNAS, [
        ["08477", "JOINER 250 ML", 6228.0, 6228.0, 12.0, 0, 0, 0],
        ["05519", "ABAMEX MAXX 1 LT", 100.0, 0.0, 3.0, 0, 0, 33.3],
        ["99999", "SEM QTD", 0.0, 0.0, 0.0, 0, 0, 0],
    ])
    rel = extrair_compras(dados)
    assert rel.aviso is None
    assert [i.cod_produto for i in rel.itens] == ["08477", "05519"]
    assert rel.itens[0].qtd_total == 12.0

    linhas = para_linhas_movimentacao(rel)
    assert linhas[0] == {
        "cod_produto": "08477", "descricao": "JOINER 250 ML",
        "quantidade_entrada": 12.0, "valor_entrada": 6228.0,
    }


def test_colunas_faltando_gera_aviso():
    dados = _xls(["A", "B"], [[1, 2]])
    rel = extrair_compras(dados)
    assert rel.itens == []
    assert "Colunas esperadas" in rel.aviso


def test_sem_itens_com_quantidade_gera_aviso():
    dados = _xls(_COLUNAS, [["99999", "X", 0.0, 0.0, 0.0, 0, 0, 0]])
    rel = extrair_compras(dados)
    assert rel.itens == []
    assert rel.aviso == "Nenhum item com quantidade encontrado no período/filtro."


def test_passa_logfile_proprio_pro_xlrd(monkeypatch):
    """O SGI grava .xls sem completar o ultimo setor, e o xlrd imprime
    'WARNING *** file size (3884) not 512 + multiple of sector size (512)' em TODO
    arquivo do robo - uma vez por dia sincronizado, dando impressao de erro no log.

    O xlrd escreve isso no `logfile` (default sys.stdout, avaliado no import), nao via
    warnings: nem warnings.filterwarnings nem redirect_stdout resolvem, so passar
    logfile=. Por isso este teste olha o CONTRATO da chamada: capsys e capfd nao
    enxergam esse texto (o default do parametro ja aponta pro stdout que o pytest
    instalou antes do import), e um teste que olhasse a saida passaria mesmo sem o
    conserto - foi o que aconteceu ao escrever este."""
    import io as _io

    import xlrd as _xlrd

    from estoque import parser_compras_sgi as pc

    vistos = {}
    original = _xlrd.open_workbook

    def espiao(*a, **kw):
        vistos.update(kw)
        return original(*a, **kw)

    monkeypatch.setattr(pc.xlrd, "open_workbook", espiao)
    xls = _xls(_COLUNAS, [["01582", "DECIS EC250ML", 240.0, 0, 10.0, 0, 0, 24.0]])
    rel = pc.extrair_compras(xls + b"\x00" * 7)  # bytes sobrando = o que dispara o aviso

    assert len(rel.itens) == 1, "o parser tem que continuar lendo o arquivo normalmente"
    assert isinstance(vistos.get("logfile"), _io.StringIO), (
        "extrair_compras precisa passar logfile= proprio pro xlrd, senao o WARNING vai "
        "pro stdout do robo")
