"""
estoque/github_actions.py - Dispara e acompanha, pela API do GitHub, o workflow
sync_sgi.yml (botão "Sincronizar agora" do painel).

O painel não roda o Playwright: o Streamlit Community Cloud não instala um Chromium
confiável. O botão só aciona à distância o mesmo workflow do cron e faz poll até acabar.

Secrets: GITHUB_TOKEN (PAT com "Actions: Read and write" só neste repositório) e
GITHUB_REPO ("dono/repositório"), em variável de ambiente, .env ou st.secrets.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import requests

WORKFLOW = "sync_sgi.yml"


class SincronizacaoIndisponivel(Exception):
    """Erro esperado (config faltando, API fora) — a tela mostra a mensagem, sem traceback."""


def _segredo(nome: str) -> str | None:
    v = os.environ.get(nome)
    if v:
        return v
    try:
        import streamlit as st
        return st.secrets.get(nome)
    except Exception:  # noqa: BLE001
        return None


def _repo() -> str:
    repo = _segredo("GITHUB_REPO")
    if not repo or "/" not in repo:
        raise SincronizacaoIndisponivel(
            "Falta o secret GITHUB_REPO no formato 'dono/repositorio' (ex.: andradesmath/estoque-defensivos)."
        )
    return repo


def _headers() -> dict:
    token = _segredo("GITHUB_TOKEN")
    if not token:
        raise SincronizacaoIndisponivel(
            "Falta o secret GITHUB_TOKEN (Personal Access Token com permissão 'Actions: Read and write' "
            "neste repositório). Defina no .env local ou nas Secrets do Streamlit Cloud."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _base() -> str:
    return f"https://api.github.com/repos/{_repo()}/actions/workflows/{WORKFLOW}"


def disparar_sincronizacao(loja: str | None = None, data: str | None = None,
                           forcar_tudo: bool = False, ref: str = "main") -> datetime:
    """workflow_dispatch. `data` = 'DD/MM/AAAA' ou None. Devolve o instante do disparo (UTC)."""
    inputs = {}
    if loja:
        inputs["loja"] = loja
    if data:
        inputs["data"] = data
    if forcar_tudo:
        inputs["forcar_tudo"] = "true"
    disparado_em = datetime.now(timezone.utc)
    try:
        resp = requests.post(f"{_base()}/dispatches", headers=_headers(),
                             json={"ref": ref, "inputs": inputs}, timeout=20)
    except requests.RequestException as e:
        raise SincronizacaoIndisponivel(f"Não consegui falar com a API do GitHub: {e}")
    if resp.status_code != 204:
        raise SincronizacaoIndisponivel(
            f"Falha ao disparar o workflow (HTTP {resp.status_code}): {resp.text[:300]}")
    return disparado_em


def _achar_run(disparado_em: datetime):
    resp = requests.get(f"{_base()}/runs", headers=_headers(),
                        params={"event": "workflow_dispatch", "per_page": 5}, timeout=20)
    resp.raise_for_status()
    for run in resp.json().get("workflow_runs", []):
        criada = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if criada >= disparado_em.replace(microsecond=0):
            return run
    return None


def aguardar_conclusao(disparado_em: datetime, timeout_s: int = 600, intervalo_s: int = 6, callback_status=None):
    """Poll até o run terminar. Retorna (sucesso, url_do_run):
    True = concluiu com sucesso; False = concluiu com erro; None = não deu tempo (pode
    continuar rodando — não é falha)."""
    inicio = time.monotonic()
    run = None
    while run is None and time.monotonic() - inicio < min(timeout_s, 120):
        run = _achar_run(disparado_em)
        if run is None:
            if callback_status:
                callback_status("Aguardando o GitHub Actions iniciar...")
            time.sleep(intervalo_s)
    if run is None:
        raise SincronizacaoIndisponivel(
            "Não encontrei a execução disparada; confira na aba Actions do repositório.")
    url_run = run.get("html_url")
    while True:
        resp = requests.get(run["url"], headers=_headers(), timeout=20)
        resp.raise_for_status()
        run = resp.json()
        status = run.get("status")
        if callback_status:
            callback_status(f"GitHub Actions: {status}...")
        if status == "completed":
            return run.get("conclusion") == "success", url_run
        if time.monotonic() - inicio > timeout_s:
            return None, url_run
        time.sleep(intervalo_s)
