# -*- coding: utf-8 -*-
"""Importar a sessao do GOOGLE, que tem regras que as outras redes nao tem.

Nasceu do YouTube nao logar (05/11). A sessao do Google vem cheia de cookies
com prefixo `__Secure-` e `__Host-`, e esses prefixos nao sao enfeite de nome:
sao regras que o navegador IMPOE, recusando o cookie que as viole.

Duas coisas travadas aqui:

  1. Os prefixos sao respeitados na normalizacao. Forcar o flag e melhor que
     recusar depois -- o que falta e um campo que o exportador esqueceu, nao uma
     informacao que nao temos.

  2. **Um cookie recusado nao derruba os outros.** `add_cookies` recebe a LISTA
     e falha inteira se um item for invalido: um flag errado num cookie
     secundario levava a sessao toda junto, e a mensagem nao dizia qual era.

Puro: sem rede e sem navegador. Rodar com:

    venv\\Scripts\\python.exe tests\\test_cookies_google.py
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ig_session as igs  # noqa: E402

ok = 0


def t(cond, msg):
    global ok
    assert cond, "FALHOU: " + msg
    ok += 1


# ── 1. As regras de prefixo ──────────────────────────────────────────────
o = igs._clean({"name": "__Secure-1PSID", "value": "x", "domain": ".google.com"},
               ".google.com")
t(o["secure"] is True, "__Secure- ganha secure mesmo se o export esqueceu")

o = igs._clean({"name": "__Secure-3PAPISID", "value": "x", "domain": ".google.com",
                "secure": False}, ".google.com")
t(o["secure"] is True, "e secure=False explicito tambem e corrigido")

o = igs._clean({"name": "__Host-GAPS", "value": "y",
                "domain": ".accounts.google.com", "path": "/algo"}, ".google.com")
t(o["secure"] is True, "__Host- exige secure")
t(o["path"] == "/", "__Host- exige path na raiz")
t(o["domain"] == "accounts.google.com",
  "__Host- e do HOST exato: o ponto da frente sai, senao valeria para subdominios")

# cookie comum nao e mexido -- a regra so vale para quem tem o prefixo
o = igs._clean({"name": "SID", "value": "z", "domain": ".google.com",
                "secure": False, "path": "/x"}, ".google.com")
t(o["secure"] is False and o["path"] == "/x",
  "cookie sem prefixo passa como veio")

# e a normalizacao de sempre continua valendo
o = igs._clean({"name": "SAPISID", "value": "z", "sameSite": "no_restriction"},
               ".google.com")
t(o["domain"] == ".google.com", "sem dominio, cai no padrao da rede")
t(o["sameSite"] in ("Strict", "Lax", "None"), "sameSite vira um dos tres aceitos")


# ── 2. Um recusado nao derruba os outros ─────────────────────────────────
class Contexto:
    """Duble do contexto. `veneno` e o nome do cookie que o navegador recusa."""

    def __init__(self, veneno=None):
        self.guardados, self.veneno = [], veneno
        self.chamadas_em_lote = 0

    async def add_cookies(self, lista):
        if len(lista) > 1:
            self.chamadas_em_lote += 1
        for c in lista:
            if c.get("name") == self.veneno:
                raise RuntimeError("Cookie should have a valid expires, only -1 or a positive number")
        self.guardados.extend(lista)

    async def cookies(self):
        return list(self.guardados)


def importar(cookies, veneno=None):
    ctx = Contexto(veneno)

    class Page:
        context = ctx

    async def _fake(profile, fn, headless=True):
        return await fn(Page())

    real = igs.sm.run_profile_task
    igs.sm.run_profile_task = _fake
    try:
        r = asyncio.run(igs.import_cookies({"id": "p"}, cookies,
                                           dominios=["youtube.com", "google.com"]))
        r["_ctx"] = ctx
        return r
    finally:
        igs.sm.run_profile_task = real


SESSAO = [
    {"name": "SID", "value": "1", "domain": ".google.com"},
    {"name": "__Secure-1PSID", "value": "2", "domain": ".google.com"},
    {"name": "SAPISID", "value": "3", "domain": ".google.com"},
    {"name": "VISITOR_INFO1_LIVE", "value": "4", "domain": ".youtube.com"},
    {"name": "LOGIN_INFO", "value": "5", "domain": ".youtube.com"},
]

# o caso normal: tudo entra numa chamada so
r = importar(SESSAO)
t(r["imported"] == 5 and not r["recusados"], "sessao boa entra inteira")
t(r["_ctx"].chamadas_em_lote == 1,
  "e numa chamada SO -- o caminho rapido continua sendo o padrao")

# o caso do YouTube: um cookie ruim no meio
r = importar(SESSAO, veneno="__Secure-1PSID")
t(r["imported"] == 4, "quatro entram mesmo com um recusado")
t(len(r["recusados"]) == 1 and r["recusados"][0]["name"] == "__Secure-1PSID",
  "e o recusado e NOMEADO, em vez de sumir numa mensagem generica")
t(r["recusados"][0]["motivo"], "com o motivo do navegador junto")
t(r["enviados"] == 5, "o relatorio diz quantos foram tentados")
nomes = {c["name"] for c in r["_ctx"].guardados}
t("SID" in nomes and "LOGIN_INFO" in nomes,
  "os bons foram guardados: antes, um ruim levava TODOS junto")

# os dois dominios do Google entram -- a sessao dele mora nos dois
t({c["name"] for c in importar(SESSAO)["_ctx"].guardados} ==
  {"SID", "__Secure-1PSID", "SAPISID", "VISITOR_INFO1_LIVE", "LOGIN_INFO"},
  "google.com E youtube.com entram juntos: a sessao do Google vive nos dois")

# cookie de outra rede no mesmo arquivo e ignorado, nao importado por engano
r = importar(SESSAO + [{"name": "sessionid", "value": "x", "domain": ".instagram.com"}])
t("sessionid" not in {c["name"] for c in r["_ctx"].guardados},
  "cookie de outra rede fica de fora do import do YouTube")

print("OK — %d assercoes" % ok)
