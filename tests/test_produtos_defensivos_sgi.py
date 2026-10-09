from estoque.produtos_defensivos_sgi import PRODUTOS_SGI, produtos_faltando


def test_lista_sem_duplicatas():
    cods = [c for c, _ in PRODUTOS_SGI]
    assert len(cods) == len(set(cods))


def test_produtos_faltando_so_lista_quem_nao_esta_cadastrado():
    cadastrados = {c for c, _ in PRODUTOS_SGI[:-1]}  # todos menos o último
    faltando = produtos_faltando(cadastrados)
    assert len(faltando) == 1
    assert faltando.iloc[0]["cod_produto"] == PRODUTOS_SGI[-1][0]


def test_produtos_faltando_vazio_quando_todos_cadastrados():
    cadastrados = {c for c, _ in PRODUTOS_SGI}
    assert produtos_faltando(cadastrados).empty
