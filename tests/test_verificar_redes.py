# -*- coding: utf-8 -*-
"""Verificar a sessao de QUALQUER rede (2026-11-05).

Nasceu do YouTube: a medicao do compositor voltou com a URL de
`accounts.google.com/v3/signin/identifier` e um campo de senha na tela -- a
sessao nao estava no perfil. A pergunta que veio junto foi "e as outras redes,
estao logadas?", e ate entao o app so sabia responder isso sobre o Instagram.

O que este arquivo trava e a ASSIMETRIA, que e deliberada e facil de quebrar
sem querer:

  - "caiu no login" e CONCLUSIVO -> marca `login_required`.
  - "abriu normal" NAO e conclusivo -> o status fica como estava. Abrir uma
    pagina sem ser barrado nao prova que ha sessao, e marcar "ativa" aqui seria
    afirmar o que nao foi medido.
  - "nao carregou" e inconclusivo, e dito assim.
  - "Em uso" e rotulo MANUAL e a verificacao nao pode apaga-lo.

Puro: sem rede e sem navegador. Rodar com:

    venv\\Scripts\\python.exe tests\\test_verificar_redes.py

⚠ O isolamento do banco vem do MONKEYPATCH, nao de um banco temporario:
`paths.DB_PATH` e calculado no import e nao le variavel de ambiente, entao nao
ha como apontar para outro arquivo sem mexer no modulo. Por isso `rodar()`
substitui `db.update_account` E `acc._apply_status` -- os dois unicos caminhos
de escrita desta funcao. Se um terceiro aparecer, ele escreve no banco de
verdade: acrescente o duble aqui junto.
"""
import asyncio
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import db  # noqa: E402
import accounts as acc  # noqa: E402
import ig_session as igs  # noqa: E402

ok = 0


def t(cond, msg):
    global ok
    assert cond, "FALHOU: " + msg
    ok += 1


PERFIL = {"id": "p1", "name": "persona"}


def rodar(rede, resposta, status_atual="active"):
    """Roda a verificacao com um probe duble que responde o que eu mandar."""
    conta = {"id": "a1", "platform": rede, "status": status_atual,
             "username": "quem"}
    gravado = {}

    async def _probe(profile, url, timeout_ms=25000):
        gravado["url"] = url
        if isinstance(resposta, Exception):
            raise resposta
        return resposta

    def _update(aid, **kw):
        gravado.update(kw)

    def _apply(aid, status, detalhe, extra=None):
        gravado["status_aplicado"] = status
        gravado.update(extra or {})

    real = (igs.probe_generico, db.update_account, acc._apply_status)
    igs.probe_generico, db.update_account, acc._apply_status = _probe, _update, _apply
    try:
        r = asyncio.run(acc._check_outra_rede("a1", conta, PERFIL, rede))
        r["_gravado"] = gravado
        return r
    finally:
        igs.probe_generico, db.update_account, acc._apply_status = real


DESLOGADO = {"logado": False, "certeza": "alta",
             "motivo": "o site levou para a tela de login (accounts.google.com/...)"}
SENHA = {"logado": False, "certeza": "média",
         "motivo": "a página mostra campo de senha — sessão não reconhecida"}
ABRIU = {"logado": True, "certeza": "baixa",
         "motivo": "o site abriu sem pedir login (https://www.tiktok.com/)"}
MUDO = {"logado": None, "erro": "não carregou: net::ERR_TIMED_OUT"}


# ── O CASO DO YOUTUBE: caiu no login, e isso e conclusivo ────────────────
r = rodar("youtube", DESLOGADO)
t(r["status"] == "login_required",
  "caiu na tela de login -> a conta vira 'sessao expirada'")
t(r["_gravado"].get("status_aplicado") == "login_required",
  "e o status e de fato gravado, nao so devolvido")
t("tela de login" in r["detail"], "o detalhe diz o que foi visto: " + r["detail"][:50])
t(r["_gravado"]["url"] == "https://www.youtube.com/",
  "abriu o SITE da rede, vindo do registro, nao uma URL chutada")

# campo de senha tambem e conclusivo, mesmo sem URL de login
r = rodar("x", SENHA)
t(r["status"] == "login_required", "campo de senha tambem conclui que nao esta logada")


# ── "abriu normal" NAO vira "ativa" ──────────────────────────────────────
r = rodar("tiktok", ABRIU, status_atual="active")
t(r["status"] == "active", "o status fica como estava")
t(r["_gravado"].get("status_aplicado") is None,
  "e NENHUM status novo e aplicado -- abrir nao prova sessao")
t(r["certeza"] == "baixa", "a certeza baixa viaja junto, para a tela nao exagerar")
t("abriu sem pedir login" in r["_gravado"]["last_check_result"],
  "o registro guarda o que foi visto, nao um veredito")

# conta que ja estava com problema NAO e promovida por um "parece logado"
r = rodar("tiktok", ABRIU, status_atual="login_required")
t(r["status"] == "login_required",
  "conta com sessao caida nao volta a ativa so porque a pagina abriu")


# ── nao carregou = inconclusivo ──────────────────────────────────────────
r = rodar("facebook", MUDO)
t(r["status"] == "active" and r["_gravado"].get("status_aplicado") is None,
  "pagina que nao carrega NAO derruba a conta")
t("não carregou" in r["_gravado"]["last_check_result"], "e o registro diz por que")

# o probe explodindo tambem nao derruba
r = rodar("threads", RuntimeError("perfil travado"))
t(r["_gravado"].get("status_aplicado") is None, "erro no probe nao muda status")
t("não consegui verificar" in r["detail"], "e aparece como falha de verificacao")


# ── "Em uso" e rotulo MANUAL: a verificacao nao o apaga ──────────────────
r = rodar("x", DESLOGADO, status_atual="in_use")
t(r["status"] == "in_use", "conta emprestada continua emprestada")
t(r["_gravado"].get("status_aplicado") is None,
  "mesmo caindo no login, o rotulo manual vence")
t(r["_gravado"]["last_check_result"].startswith("(em uso)"),
  "mas o que foi visto fica registrado: " + r["_gravado"]["last_check_result"][:40])


# ── rede sem site cadastrado nao explode ─────────────────────────────────
r = rodar("orkut", ABRIU)
t(r["_gravado"].get("status_aplicado") is None and "não sei qual é o site" in r["detail"],
  "rede desconhecida vira aviso, nao exceção")

print("OK — %d assercoes" % ok)
