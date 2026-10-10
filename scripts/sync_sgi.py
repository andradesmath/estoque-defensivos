"""
scripts/sync_sgi.py - Sincroniza do SGI Solution as SAÍDAS POR VENDA de DEFENSIVOS.

Roteiro por loja (Porteira e Casa de Adubo, cada uma é uma "Empresa" no login):
  login -> Vendas -> "Totais de Vendas Por Produto" -> para cada dia pendente:
  período = o dia, Grupos = DEFENSIVOS, Agrupamento = Produto, Operação = V ->
  PDF -> parse -> validação -> UPSERT idempotente em movimentacao_saida.

Por que UM RELATÓRIO POR DIA: o PDF do SGI é agregado no período (sem coluna de data).
Para guardar a saída de cada dia (e permitir sobrescrever o dia sem somar duas vezes)
pedimos período = [dia, dia]. Dias antigos só voltam a ser buscados se faltarem em
sync_dias ou estiverem na janela móvel dos últimos N dias (--janela, padrão 3).

Como o PDF é obtido (ordem):
  1) REQUISIÇÃO DIRETA: o botão "Imprimir" do SGI faz
     window.open(form.attr('rota-pdf') + '?' + form.serialize()). Reproduzimos isso: lemos
     rota-pdf e o serialize() do formulário JÁ preenchido e baixamos com a sessão logada
     (context.request). Não depende de popup nem de download.
  2) CLIQUE em Imprimir capturando download / nova aba / resposta de rede com PDF
     (mesma abordagem já validada no dashboard de vendedores).
Em qualquer caso o PDF só é gravado se passar em parser_sgi.validar (período == dia
pedido, Grupos == DEFENSIVOS, soma dos produtos == "Total:" impresso).

ATENÇÃO: os seletores do formulário foram escritos a partir do HTML real salvo pelo
dashboard de vendedores (ids/names/valores conferidos), mas esta NAVEGAÇÃO ainda não foi
executada ao vivo neste ambiente. Primeiro teste recomendado, na sua máquina:
    python scripts/sync_sgi.py --loja Porteira --data 22/09/2026 --dry-run --headed
(--dry-run baixa e valida sem gravar no banco).

Variáveis de ambiente (Secrets no GitHub; .env local para teste):
    DATABASE_URL            connection string do Postgres deste sistema
    SGI_URL_LOGIN           ex.: http://138.255.35.101:8888/login
    SGI_LOGIN, SGI_SENHA    credenciais do SGI
    SGI_EMPRESA_PORTEIRA    texto exato da opção no login: "PORTEIRA AGROCOMERCIAL"
    SGI_EMPRESA_CASA_ADUBO  texto exato da opção no login: "CASA DE ADUBOS CAFE BOM"
  opcionais:
    SYNC_DATA_INICIAL       AAAA-MM-DD (padrão 2026-09-22)
    SYNC_JANELA_DIAS        padrão 3
    SGI_OPERACOES           códigos separados por vírgula (padrão "V" = só vendas;
                            "V,B,T" inclui bonificação e troca)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from urllib.parse import urljoin, urlsplit

RAIZ_PROJETO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_PROJETO)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(RAIZ_PROJETO, ".env"))
except ImportError:
    pass

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from estoque import parser_sgi, sync_core  # noqa: E402
from estoque.util import hoje_brasil  # noqa: E402

LOJA_PARA_EMPRESA_ENV = {
    "Porteira": "SGI_EMPRESA_PORTEIRA",
    "Casa de Adubo": "SGI_EMPRESA_CASA_ADUBO",
    # Filial de Piatã: estoque PRÓPRIO (unidade 'Piatã' em unidades_loja), ao contrário
    # das duas acima, que dividem o estoque de Barra da Estiva. No portal a empresa
    # chama 'PORTEIRA PIATA' (sem til) - o nome aqui é o nosso rótulo de loja, o do
    # portal fica no secret. Sem o secret definido, a loja simplesmente não é
    # sincronizada (ver main): é assim que as lojas entram uma de cada vez.
    "Piatã": "SGI_EMPRESA_PIATA",
}
GRUPO_ALVO = "DEFENSIVOS"
AGRUPAMENTO_PRODUTO = "produto"          # <option value="produto">Produto</option>
ROTA_FORM_FALLBACK = "/relatorio/total-de-vendas-produto"
ID_BOTAO_IMPRIMIR = "#buttonPdfRelatorioTotalVendasProduto"
# ids REAIS dos <select> no HTML do SGI (conferidos): note que o de operações tem
# id="operacoes" mas name="operacoes[]"; os de grupos têm id="grupos[]".
ID_SELECT_GRUPOS = "grupos[]"
ID_SELECT_AGRUPAMENTO = "agrupamento"
ID_SELECT_OPERACOES = "operacoes"


class FormularioInesperado(Exception):
    pass


# --------------------------------------------------------------------------- debug
def salvar_debug(page, loja: str, etapa: str) -> None:
    """Screenshot + HTML no ponto crítico, para diagnosticar sem reproduzir ao vivo."""
    prefixo = f"debug_{loja.replace(' ', '_')}_{etapa}"
    try:
        page.screenshot(path=f"{prefixo}.png", full_page=True)
        with open(f"{prefixo}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        print(f"  [debug] salvo {prefixo}.png / {prefixo}.html")
    except Exception as e:  # noqa: BLE001
        print(f"  [debug] não foi possível salvar debug da etapa '{etapa}': {e}")


def salvar_pdf_debug(pdf: bytes, loja: str, dia: date) -> None:
    with open(f"debug_{loja.replace(' ', '_')}_produtos_{dia:%Y%m%d}.pdf", "wb") as f:
        f.write(pdf)


def salvar_pdf_debug_e_banco(pdf: bytes, loja: str, dia: date) -> None:
    """Guarda o arquivo local de depuração (7 dias, artefato do Actions) e também no
    Postgres (fica disponível na tela 'PDFs importados' do painel, para sempre)."""
    salvar_pdf_debug(pdf, loja, dia)
    from estoque import db
    try:
        db.salvar_pdf(loja, dia, pdf)
    except Exception as e:  # noqa: BLE001 - não pode derrubar a gravação das vendas
        print(f"  [aviso] não consegui guardar o PDF de {loja} {dia:%d/%m/%Y} no banco: {e}")


# --------------------------------------------------------------------------- login
def _preencher_por_label_ou_placeholder(page, rotulos, valor) -> bool:
    for rotulo in rotulos:
        for tentativa in (
            lambda: page.get_by_label(rotulo, exact=False),
            lambda: page.get_by_placeholder(rotulo, exact=False),
        ):
            try:
                campo = tentativa()
                if campo.count() > 0:
                    campo.first.fill(valor)
                    try:
                        campo.first.press("Tab")
                    except Exception:  # noqa: BLE001
                        pass
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def fazer_login(playwright, loja, url_login, login, senha, empresa_texto, headed=False):
    """Retorna (browser, context, page) já logados. Quem chama fecha o browser."""
    browser = playwright.chromium.launch(headless=not headed)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()

    page.goto(url_login, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)

    try:
        page.get_by_label("Empresa", exact=False).select_option(label=empresa_texto)
    except Exception:  # noqa: BLE001
        page.locator("select").first.select_option(label=empresa_texto)

    if not _preencher_por_label_ou_placeholder(page, ["Login", "Usuário", "Usuario"], login):
        page.locator("input[type='text']").first.fill(login)
    if not _preencher_por_label_ou_placeholder(page, ["Senha"], senha):
        page.locator("input[type='password']").first.fill(senha)

    salvar_debug(page, loja, "antes_do_login")
    page.get_by_role("button", name="Login").click()
    page.wait_for_load_state("domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    salvar_debug(page, loja, "depois_do_login")
    return browser, context, page


def navegar_ate_totais_de_vendas_por_produto(page, url_login: str | None = None) -> None:
    """Abre o formulário 'Totais de Vendas Por Produto'. Três caminhos, do mais
    determinístico ao menos:
      1) href do link do menu (no HTML real: <a class="link-menu"
         href=".../relatorio/total-de-vendas-produto">) -> page.goto. Não depende de o
         submenu colapsado estar aberto;
      2) cliques Relatórios > Vendas > 'Totais de Vendas Por Produto' (roteiro validado
         no dashboard de vendedores);
      3) URL montada com o host do login + /relatorio/total-de-vendas-produto."""
    sel_link = "a[href$='/relatorio/total-de-vendas-produto']"
    navegou = False
    try:
        if page.locator(sel_link).count() > 0:
            href = page.locator(sel_link).first.get_attribute("href")
            page.goto(href, wait_until="domcontentloaded", timeout=60000)
            navegou = True
    except Exception:  # noqa: BLE001
        pass
    if not navegou:
        try:
            try:
                page.get_by_text("Vendas", exact=True).first.click(timeout=8000)
                page.wait_for_timeout(500)
            except Exception:  # noqa: BLE001 - menu pode já estar aberto
                pass
            page.get_by_text("Totais de Vendas Por Produto", exact=True).first.click(timeout=8000)
            page.wait_for_load_state("domcontentloaded", timeout=60000)
        except Exception:  # noqa: BLE001
            if not url_login:
                raise
            partes = urlsplit(url_login)
            page.goto(f"{partes.scheme}://{partes.netloc}{ROTA_FORM_FALLBACK}",
                      wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    if page.locator("form#formRelatorio").count() == 0:
        raise FormularioInesperado("formulário 'form#formRelatorio' não encontrado após navegar ao relatório.")


# ------------------------------------------------------------------ campos do formulário
_JS_DEFINIR_SELECT = """
([id, alvos, por]) => {
  const sel = document.getElementById(id);
  if (!sel) return {erro: 'sem_select'};
  const set = new Set(alvos.map(a => a.trim().toUpperCase()));
  let achou = 0;
  for (const o of sel.options) {
    const chave = (por === 'value' ? o.value : o.text).trim().toUpperCase();
    o.selected = set.has(chave);
    if (o.selected) achou++;
  }
  if (achou !== set.size) return {erro: 'opcao_nao_encontrada', achou: achou};
  const $ = window.jQuery;
  if ($ && $.fn && $.fn.selectpicker) { $(sel).selectpicker('refresh'); }
  if ($) { $(sel).trigger('change'); } else { sel.dispatchEvent(new Event('change', {bubbles: true})); }
  return {textos: Array.from(sel.selectedOptions).map(o => o.text.trim()),
          valores: Array.from(sel.selectedOptions).map(o => o.value)};
}
"""

_JS_LER_SELECT = """
(id) => {
  const sel = document.getElementById(id);
  if (!sel) return null;
  return {textos: Array.from(sel.selectedOptions).map(o => o.text.trim()),
          valores: Array.from(sel.selectedOptions).map(o => o.value)};
}
"""


def ler_select(page, select_id: str) -> dict | None:
    return page.evaluate(_JS_LER_SELECT, select_id)


def definir_select(page, select_id: str, alvos: list[str], por: str = "text") -> dict:
    """Define as opções do <select> (bootstrap-select esconde o nativo; o serialize()
    do form lê o nativo, então basta acertá-lo e disparar 'change'). Retorna o estado
    lido de volta. Lança FormularioInesperado se não ficou exatamente como pedido."""
    r = page.evaluate(_JS_DEFINIR_SELECT, [select_id, alvos, por])
    if r.get("erro"):
        raise FormularioInesperado(f"select '{select_id}': {r['erro']} (alvos={alvos}, por={por})")
    return r


def selecionar_grupo_defensivos(page) -> None:
    """Grupos = apenas DEFENSIVOS. Estratégia 1: acerta o <select multiple id='grupos[]'>
    direto (JS). Estratégia 2 (se a 1 falhar): clica no dropdown bootstrap-select
    (botão data-id='grupos[]') -> 'Nenhum' -> item DEFENSIVOS. Em ambas relê o select."""
    try:
        definir_select(page, ID_SELECT_GRUPOS, [GRUPO_ALVO], por="text")
    except FormularioInesperado:
        wrapper = page.locator("div.bootstrap-select:has(button[data-id='grupos[]'])").first
        wrapper.locator("button.dropdown-toggle").click()
        page.wait_for_timeout(300)
        try:
            wrapper.locator("button.bs-deselect-all").click(timeout=2000)
        except Exception:  # noqa: BLE001
            pass
        wrapper.locator(f"li:has(span.text:text-is('{GRUPO_ALVO}')) a").first.click()
        page.locator("label[for='grupos[]']").click()  # clique fora fecha o dropdown
    estado = ler_select(page, ID_SELECT_GRUPOS)
    if not estado or [t.upper() for t in estado["textos"]] != [GRUPO_ALVO]:
        raise FormularioInesperado(f"Grupos ficou {estado}, esperado apenas {GRUPO_ALVO}.")


def selecionar_agrupamento_produto(page) -> None:
    definir_select(page, ID_SELECT_AGRUPAMENTO, [AGRUPAMENTO_PRODUTO], por="value")
    estado = ler_select(page, ID_SELECT_AGRUPAMENTO)
    if not estado or estado["valores"] != [AGRUPAMENTO_PRODUTO]:
        raise FormularioInesperado(f"Agrupamento ficou {estado}, esperado '{AGRUPAMENTO_PRODUTO}'.")


def selecionar_operacoes(page, codigos: list[str]) -> None:
    definir_select(page, ID_SELECT_OPERACOES, codigos, por="value")
    estado = ler_select(page, ID_SELECT_OPERACOES)
    if not estado or sorted(estado["valores"]) != sorted(c.upper() for c in codigos):
        raise FormularioInesperado(f"Operações ficou {estado}, esperado {codigos}.")


def _valor_input(page, name: str) -> str:
    return page.locator(f"input[name='{name}']").first.input_value()


def preencher_periodo(page, data_ini: str, data_fim: str) -> None:
    """Campos de data são bootstrap-datepicker (jQuery): .fill() não atualiza o estado
    interno do plugin. Digitação real caractere a caractere + Tab (validado no
    dashboard de vendedores). Depois LÊ de volta; se divergir, força o value por JS e
    dispara change — o serialize() só lê o value. O PDF ainda é conferido pelo período."""
    ini = page.locator("input[name='data_inicial']").first
    fim = page.locator("input[name='data_final']").first
    for campo, valor in ((ini, data_ini), (fim, data_fim)):
        campo.click()
        campo.press("Control+A")
        campo.press_sequentially(valor, delay=30)
        campo.press("Tab")
    if (_valor_input(page, "data_inicial"), _valor_input(page, "data_final")) != (data_ini, data_fim):
        page.evaluate(
            """([a, b]) => {
              for (const [n, v] of [['data_inicial', a], ['data_final', b]]) {
                const el = document.querySelector(`input[name='${n}']`);
                el.value = v;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
              }
            }""",
            [data_ini, data_fim],
        )
    lido = (_valor_input(page, "data_inicial"), _valor_input(page, "data_final"))
    if lido != (data_ini, data_fim):
        raise FormularioInesperado(f"período no formulário = {lido}, esperado {(data_ini, data_fim)}.")


def preparar_formulario(page, dia: date, operacoes: list[str]) -> None:
    d = dia.strftime("%d/%m/%Y")
    preencher_periodo(page, d, d)
    selecionar_grupo_defensivos(page)
    selecionar_agrupamento_produto(page)
    selecionar_operacoes(page, operacoes)


# ------------------------------------------------------------------------- obter o PDF
def _pdf_por_requisicao_direta(page, context) -> bytes:
    """Reproduz o handler do botão Imprimir do SGI (visto no HTML real):
        rota = $('form#formRelatorio').attr('rota-pdf'); dados = ...serialize(); window.open(rota+'?'+dados)"""
    info = page.evaluate(
        """() => {
          const f = window.jQuery ? window.jQuery('form#formRelatorio') : null;
          if (!f || !f.length) return null;
          return {rota: f.attr('rota-pdf'), dados: f.serialize()};
        }"""
    )
    if not info or not info.get("rota"):
        raise FormularioInesperado("não consegui ler rota-pdf/serialize() do formulário (jQuery ausente?).")
    url = urljoin(page.url, f"{info['rota']}?{info['dados']}")  # rota-pdf pode ser relativa
    resp = context.request.get(url, timeout=90000)
    corpo = resp.body()
    if resp.status != 200 or not corpo.startswith(b"%PDF"):
        raise FormularioInesperado(
            f"requisição direta devolveu HTTP {resp.status}, {len(corpo)} bytes, "
            f"content-type={resp.headers.get('content-type')!r}."
        )
    return corpo


def _pdf_por_clique(page, context) -> bytes:
    """Clica em Imprimir e captura, numa ÚNICA clicada, o que acontecer: download,
    nova aba (cujo URL baixamos com a sessão) ou resposta de rede com content-type PDF."""
    achados: dict = {}

    def _on_response(r):
        try:
            if "pdf" in (r.headers.get("content-type", "").lower()) and "corpo" not in achados:
                achados["corpo"] = r.body()
        except Exception:  # noqa: BLE001
            pass

    def _on_page(p):
        achados.setdefault("paginas", []).append(p)
        p.on("download", lambda d: achados.setdefault("downloads", []).append(d))

    def _on_download(d):
        achados.setdefault("downloads", []).append(d)

    page.on("download", _on_download)
    context.on("response", _on_response)
    context.on("page", _on_page)
    try:
        page.locator(ID_BOTAO_IMPRIMIR).first.click()
        for _ in range(60):  # até ~30 s
            page.wait_for_timeout(500)
            if achados.get("corpo"):
                return achados["corpo"]
            if achados.get("downloads"):
                with open(achados["downloads"][0].path(), "rb") as f:
                    return f.read()
            for p in achados.get("paginas", []):
                url = p.url
                if url.startswith("http"):
                    r = context.request.get(url, timeout=90000)
                    if r.body().startswith(b"%PDF"):
                        return r.body()
        raise PlaywrightTimeoutError("clique em Imprimir não produziu download, aba nem resposta PDF em 30 s.")
    finally:
        for p in achados.get("paginas", []):
            try:
                p.close()
            except Exception:  # noqa: BLE001
                pass
        context.remove_listener("response", _on_response)
        context.remove_listener("page", _on_page)
        page.remove_listener("download", _on_download)


def _periodo_do_pdf(pdf: bytes):
    try:
        return parser_sgi.parse_relatorio_defensivos(pdf).periodo
    except Exception:  # noqa: BLE001
        return None


def baixar_pdf_dia(page, context, dia: date, operacoes: list[str], loja: str, url_login: str | None = None) -> bytes:
    try:
        preparar_formulario(page, dia, operacoes)
    except Exception:
        salvar_debug(page, loja, f"form_{dia:%Y%m%d}_erro")
        raise
    pdf = None
    try:
        pdf = _pdf_por_requisicao_direta(page, context)
    except Exception as e:  # noqa: BLE001
        print(f"  [{loja}] {dia:%d/%m/%Y}: requisição direta falhou ({e}); tentando pelo clique.")
    if pdf is None or _periodo_do_pdf(pdf) != (dia, dia):
        if pdf is not None:
            print(f"  [{loja}] {dia:%d/%m/%Y}: PDF veio com período {_periodo_do_pdf(pdf)}; tentando pelo clique.")
        try:
            pdf = _pdf_por_clique(page, context)
        except Exception:
            salvar_debug(page, loja, f"pdf_{dia:%Y%m%d}_erro")
            raise
    return pdf


# ------------------------------------------------------------------------------ orquestração
def sincronizar_loja(playwright, loja, dias, operacoes, gravar, permitir_zerar, headed=False, salvar_pdf=salvar_pdf_debug):
    url_login = os.environ["SGI_URL_LOGIN"]
    empresa = os.environ[LOJA_PARA_EMPRESA_ENV[loja]]
    print(f"[{loja}] login e {len(dias)} dia(s) a sincronizar: "
          + (", ".join(f"{d:%d/%m}" for d in dias) or "nenhum"))
    if not dias:
        return [], []
    browser, context, page = fazer_login(
        playwright, loja, url_login, os.environ["SGI_LOGIN"], os.environ["SGI_SENHA"], empresa, headed=headed)
    try:
        navegar_ate_totais_de_vendas_por_produto(page, url_login)
        salvar_debug(page, loja, "form_antes")

        def baixar(dia):
            try:
                return baixar_pdf_dia(page, context, dia, operacoes, loja, url_login)
            except Exception:
                # sessão pode ter caído / página mudado: volta ao formulário para o próximo dia
                try:
                    navegar_ate_totais_de_vendas_por_produto(page, url_login)
                except Exception:  # noqa: BLE001
                    pass
                raise

        return sync_core.sincronizar_dias(
            loja, dias, baixar, gravar, salvar_pdf=salvar_pdf, permitir_zerar=permitir_zerar)
    finally:
        browser.close()


def _data_br(txt: str) -> date:
    return datetime.strptime(txt, "%d/%m/%Y").date()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sincroniza saídas de DEFENSIVOS a partir do SGI Solution.")
    ap.add_argument("--loja", choices=list(LOJA_PARA_EMPRESA_ENV), help="Só uma loja (debug).")
    ap.add_argument("--data", type=_data_br, help="Só este dia, DD/MM/AAAA.")
    ap.add_argument("--desde", type=_data_br, help="Data inicial do controle, DD/MM/AAAA (padrão: SYNC_DATA_INICIAL ou 22/09/2026).")
    ap.add_argument("--janela", type=int, help="Dias recentes sempre rebuscados (padrão: SYNC_JANELA_DIAS ou 3).")
    ap.add_argument("--forcar-tudo", action="store_true", help="Rebusca todos os dias desde a data inicial.")
    ap.add_argument("--permitir-zerar", action="store_true", help="Aceita relatório vazio mesmo se o dia já tinha vendas.")
    ap.add_argument("--dry-run", action="store_true", help="Baixa e valida, mas NÃO grava no banco.")
    ap.add_argument("--headed", action="store_true", help="Navegador visível (só local).")
    args = ap.parse_args(argv)

    inicio = args.desde or datetime.strptime(os.environ.get("SYNC_DATA_INICIAL", "2026-09-22"), "%Y-%m-%d").date()
    janela = args.janela if args.janela is not None else int(os.environ.get("SYNC_JANELA_DIAS", "3"))
    operacoes = [o.strip().upper() for o in os.environ.get("SGI_OPERACOES", "V").split(",") if o.strip()]
    lojas = [args.loja] if args.loja else [l for l, env in LOJA_PARA_EMPRESA_ENV.items() if os.environ.get(env)]
    if not lojas:
        print("Nenhuma loja configurada — defina SGI_EMPRESA_PORTEIRA e/ou SGI_EMPRESA_CASA_ADUBO (ou passe --loja).")
        return 1
    hoje = hoje_brasil()

    if args.dry_run:
        def gravar(loja, dia, linhas, permitir_zerar=False):
            print(f"  [dry-run] {loja} {dia:%d/%m/%Y}: {len(linhas)} produto(s) — nada gravado.")
            return {"gravados": len(linhas), "removidos": 0}
        dias_sinc = lambda loja: set()  # noqa: E731
        salvar_pdf = salvar_pdf_debug  # dry-run: só o arquivo local, nada no banco
        exec_id = None
    else:
        from estoque import db
        db.init_schema()
        gravar = db.substituir_movimentacao_dia
        dias_sinc = db.dias_sincronizados
        salvar_pdf = salvar_pdf_debug_e_banco
        exec_id = db.iniciar_execucao(os.environ.get("GITHUB_EVENT_NAME", "manual"))

    todos_ok, todos_erros = [], []
    try:
        with sync_playwright() as pw:
            for loja in lojas:
                if args.data:
                    dias = [args.data]
                else:
                    jan = (hoje - inicio).days + 1 if args.forcar_tudo else janela
                    dias = sync_core.dias_a_sincronizar(inicio, hoje, dias_sinc(loja), jan)
                try:
                    ok, erros = sincronizar_loja(pw, loja, dias, operacoes, gravar, args.permitir_zerar, args.headed,
                                                 salvar_pdf=salvar_pdf)
                    todos_ok += ok
                    todos_erros += erros
                except Exception as e:  # noqa: BLE001 - falha de login/navegação da loja inteira
                    todos_erros.append(f"{loja}: {e}")
                    print(f"[{loja}] ERRO: {e}")
    finally:
        if exec_id is not None:
            resumo = f"{len(todos_ok)} dia(s) ok, {len(todos_erros)} erro(s)" + (
                "\n" + "\n".join(todos_erros) if todos_erros else "")
            db.finalizar_execucao(exec_id, "erro" if todos_erros else "ok", resumo)

    if todos_erros:
        print("\nFalhas nessa execução:")
        for e in todos_erros:
            print(f"  - {e}")
        return 1
    print("\nSincronização concluída sem erros.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
