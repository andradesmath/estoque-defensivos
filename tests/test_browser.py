"""
Teste de NAVEGADOR OFFLINE da camada Playwright de scripts/sync_sgi.py.

Sobe um servidor HTTP local que serve uma réplica do formulário do SGI (mesmos
ids/names e mesmas libs: jQuery + Bootstrap 3 + bootstrap-select + bootstrap-datepicker)
e um endpoint de PDF. Valida seletores, digitação nos campos, serialize() do form,
captura por requisição direta e por clique/nova aba — sem SGI ao vivo.

Requer: playwright + Chromium e `cd tests/fixtures && npm install` (libs JS). Se algo
faltar, os testes são PULADOS (não falham).
"""
import importlib.util
import io
import sys
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from estoque import parser_sgi, sync_core

FIXT = Path(__file__).resolve().parent / "fixtures"
NODE = FIXT / "node_modules"
pytest.importorskip("playwright")
pytest.importorskip("reportlab")
if not (NODE / "jquery" / "dist" / "jquery.min.js").exists():
    pytest.skip("rode `cd tests/fixtures && npm install` para habilitar o teste de navegador", allow_module_level=True)

spec = importlib.util.spec_from_file_location("sync_sgi", Path(__file__).resolve().parent.parent / "scripts" / "sync_sgi.py")
sync_sgi = importlib.util.module_from_spec(spec)
sys.modules["sync_sgi"] = sync_sgi
spec.loader.exec_module(sync_sgi)

from playwright.sync_api import sync_playwright  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

ITENS = [("09490", "DEXTER PLUS 1LT", 10, "1.397,24"), ("00004", "EPINGLE LT", 9, "535,72")]
GRUPOS = {"00001": "DEFENSIVOS", "00018": "ADUBO"}


def _gerar_pdf(ini: str, fim: str, grupos: list[str]) -> bytes:
    """PDF com UMA CÉLULA POR LINHA, como o pypdf extrai o relatório real."""
    tokens = [f"{ini} a {fim}", "24/09/2026 16:46", "S.G.I. - Sistema de Gerencimanto Informatizado",
              "Relatorio Totais de Vendas por Produtos"]
    if grupos:
        tokens += ["Grupos:", " " + ",".join(GRUPOS.get(g, g) for g in grupos) + ","]
    tokens += ["N°", "Cod", "Descrição Produto", "Marca", "Fornecedor", "Posit", "Vendas", "Qtd", "Qtd Cx", "Valor Total", "%"]
    tq = 0
    tv = 0.0
    for n, (cod, desc, qtd, valor) in enumerate(ITENS):
        tokens += [str(n), cod, desc, "-", "1", "1", str(qtd), str(qtd), valor, "50,00"]
        tq += qtd
        tv += float(valor.replace(".", "").replace(",", "."))
    tokens += ["Total:", "2", "2", str(tq), str(tq), f"{tv:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), "100%"]
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for t in tokens:
        if y < 40:
            c.showPage(); y = 800
        c.drawString(40, y, t)
        y -= 12
    c.save()
    return buf.getvalue()


class _Estado:
    consultas: list = []
    data_errada = False


@pytest.fixture(scope="module")
def servidor():
    estado = _Estado()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _enviar(self, corpo: bytes, tipo: str, status=200, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(corpo)

        def do_GET(self):
            u = urlsplit(self.path)
            if u.path == "/relatorio/total-de-vendas-produto":
                self._enviar((FIXT / "form_totais_produto.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/menu":
                html = ('<html><body><ul class="sidenav-subnav collapse"><li><a class="link-menu" '
                        'href="/relatorio/total-de-vendas-produto">Totais\n de Vendas Por Produto</a></li></ul></body></html>')
                self._enviar(html.encode(), "text/html; charset=utf-8")
            elif u.path == "/relatorio/total-de-vendas-produto/pdf":
                q = parse_qs(u.query)
                estado.consultas.append(q)
                ini, fim = q["data_inicial"][0], q["data_final"][0]
                if estado.data_errada:
                    ini = fim = "24/09/2026"
                self._enviar(_gerar_pdf(ini, fim, q.get("grupos[]", [])), "application/pdf",
                             extra={"Content-Disposition": 'inline; filename="relatorio.pdf"'})
            elif u.path.startswith("/static/"):
                arq = NODE / u.path[len("/static/"):]
                if arq.is_file():
                    tipo = "text/css" if arq.suffix == ".css" else "application/javascript"
                    self._enviar(arq.read_bytes(), tipo)
                else:
                    self._enviar(b"", "text/plain", 404)
            else:
                self._enviar(b"", "text/plain", 404)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    estado.base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield estado
    srv.shutdown()


@pytest.fixture(scope="module")
def navegador():
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium indisponível: {e}")
        yield b
        b.close()


@pytest.fixture
def pagina(navegador, servidor):
    servidor.consultas.clear()
    servidor.data_errada = False
    ctx = navegador.new_context(accept_downloads=True)
    page = ctx.new_page()
    page.goto(servidor.base + "/relatorio/total-de-vendas-produto")
    page.wait_for_timeout(500)
    yield page, ctx
    ctx.close()


def test_preparar_formulario_seleciona_tudo_e_le_de_volta(pagina):
    page, _ = pagina
    sync_sgi.preparar_formulario(page, date(2026, 9, 22), ["V"])
    assert sync_sgi.ler_select(page, "grupos[]")["valores"] == ["00001"]
    assert sync_sgi.ler_select(page, "agrupamento")["valores"] == ["produto"]
    assert sync_sgi.ler_select(page, "operacoes")["valores"] == ["V"]
    assert page.input_value("input[name='data_inicial']") == "22/09/2026"
    assert page.input_value("input[name='data_final']") == "22/09/2026"


def test_operacoes_multiplas(pagina):
    page, _ = pagina
    sync_sgi.selecionar_operacoes(page, ["V", "B"])
    assert sorted(sync_sgi.ler_select(page, "operacoes")["valores"]) == ["B", "V"]


def test_grupo_inexistente_falha_alto(pagina, monkeypatch):
    page, _ = pagina
    monkeypatch.setattr(sync_sgi, "GRUPO_ALVO", "NAO EXISTE")
    with pytest.raises(Exception):
        sync_sgi.selecionar_grupo_defensivos(page)


def test_grupo_fallback_por_clique_no_dropdown(pagina, monkeypatch):
    """Força a estratégia 1 (JS) a falhar para exercitar o clique no bootstrap-select real."""
    page, _ = pagina
    original = sync_sgi.definir_select

    def so_falha_no_grupo(pg, sid, alvos, por="text"):
        if sid == "grupos[]":
            raise sync_sgi.FormularioInesperado("simulado")
        return original(pg, sid, alvos, por)

    monkeypatch.setattr(sync_sgi, "definir_select", so_falha_no_grupo)
    sync_sgi.selecionar_grupo_defensivos(page)
    assert sync_sgi.ler_select(page, "grupos[]")["textos"] == ["DEFENSIVOS"]


def test_requisicao_direta_envia_os_parametros_do_formulario(pagina, servidor):
    page, ctx = pagina
    sync_sgi.preparar_formulario(page, date(2026, 9, 22), ["V"])
    pdf = sync_sgi._pdf_por_requisicao_direta(page, ctx)
    q = servidor.consultas[-1]
    assert q["data_inicial"] == ["22/09/2026"] and q["data_final"] == ["22/09/2026"]
    assert q["grupos[]"] == ["00001"] and q["agrupamento"] == ["produto"] and q["operacoes[]"] == ["V"]
    assert q["_token"] == ["tok-teste-123"]
    res = parser_sgi.parse_relatorio_defensivos(pdf)
    assert parser_sgi.validar(res, dia_esperado=date(2026, 9, 22)) == []
    assert len(res.itens) == 2


def test_captura_por_clique_abre_nova_aba_e_devolve_pdf(pagina, servidor):
    page, ctx = pagina
    sync_sgi.preparar_formulario(page, date(2026, 9, 23), ["V"])
    pdf = sync_sgi._pdf_por_clique(page, ctx)
    assert pdf.startswith(b"%PDF")
    assert servidor.consultas[-1]["data_inicial"] == ["23/09/2026"]
    assert len(ctx.pages) == 1  # a aba aberta pelo clique foi fechada


def test_baixar_pdf_dia_completo_e_processar(pagina):
    page, ctx = pagina
    pdf = sync_sgi.baixar_pdf_dia(page, ctx, date(2026, 9, 24), ["V"], "Teste")
    gravado = {}
    sync_core.processar_pdf_dia(pdf, "Porteira", date(2026, 9, 24),
                                lambda l, d, linhas, permitir_zerar=False: gravado.update(n=len(linhas)) or {"removidos": 0})
    assert gravado["n"] == 2


def test_servidor_devolvendo_data_errada_e_barrado_pela_validacao(pagina, servidor):
    page, ctx = pagina
    servidor.data_errada = True  # simula o bug do datepicker: relatório sai com a data de hoje
    pdf = sync_sgi.baixar_pdf_dia(page, ctx, date(2026, 9, 22), ["V"], "Teste")
    with pytest.raises(sync_core.RelatorioInvalido) as e:
        sync_core.processar_pdf_dia(pdf, "Porteira", date(2026, 9, 22), lambda *a, **k: {})
    assert "difere do dia pedido" in str(e.value)
    assert len(servidor.consultas) >= 2  # tentou a requisição direta E o clique


def test_navegar_pelo_href_do_menu(navegador, servidor):
    ctx = navegador.new_context()
    page = ctx.new_page()
    page.goto(servidor.base + "/menu")
    sync_sgi.navegar_ate_totais_de_vendas_por_produto(page, servidor.base + "/login")
    assert page.url.endswith("/relatorio/total-de-vendas-produto")
    assert page.locator("form#formRelatorio").count() == 1
    ctx.close()


def test_navegar_sem_menu_usa_url_montada(navegador, servidor):
    ctx = navegador.new_context()
    page = ctx.new_page()
    page.set_content("<html><body>sem menu</body></html>")
    sync_sgi.navegar_ate_totais_de_vendas_por_produto(page, servidor.base + "/login")
    assert page.locator("form#formRelatorio").count() == 1
    ctx.close()
