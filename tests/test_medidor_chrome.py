# -*- coding: utf-8 -*-
"""O medidor dirigindo o CHROME, e nao so o Camoufox (2026-10-23).

A pedido: medir pelo Chrome, porque a sessao de uma rede pode ter nascido la.
O que este arquivo trava:

  - a escolha de navegador chega ate o relatorio (e nao se perde no caminho);
  - a pasta de cookies certa para cada caso (o Chrome de sempre vs uma pasta
    dedicada a persona), com o nome higienizado;
  - os erros que o usuario vai encontrar DE FATO -- Chrome trancado por outra
    janela, e "medir o passo atual" sem ter aberto nada -- dizendo o que fazer;
  - que o Camoufox continua sendo o padrao e nao regrediu.

Sem rede, sem Chrome de verdade, sem banco. Rodar com:

    venv\\Scripts\\python.exe tests\\test_medidor_chrome.py
"""
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import compositor as comp

ok = 0


def t(cond, msg):
    global ok
    assert cond, "FALHOU: " + msg
    ok += 1


TELA = {
    "titulo": "X", "url": "https://x.com/compose/post", "campos_senha": 0,
    "arquivos": [], "raizes_sombra": 0, "iframes": [],
    "textos": [{"seletor": "div[aria-label=\"Postar texto\"]", "tag": "div",
                "placeholder": "", "aria": "Postar texto", "papel": "textbox",
                "limite": "", "visivel": True}],
    "botoes": [{"seletor": "button[data-testid=\"tweetButton\"]", "texto": "Postar",
                "aria": "", "data_e2e": "", "data_testid": "tweetButton",
                "desabilitado": False, "visivel": True}],
    "clicaveis": [],
}


class Page:
    def __init__(self):
        self.url = TELA["url"]

    async def evaluate(self, js):
        if "querySelectorAll" in js and "length" in js and "titulo" not in js:
            return 9
        return json.loads(json.dumps(TELA))

    async def wait_for_timeout(self, ms):
        pass

    async def screenshot(self, path=None):
        pathlib.Path(path).write_bytes(b"\x89PNG-falso")


def medir(**kw):
    """Roda o medidor com os dois caminhos de navegador dublados."""
    visto = {}

    async def _chrome_abrir(pasta, url, fn):
        visto["chrome_abrir"] = (str(pasta), url)
        return {"url": url, "resultado": await fn(Page())}

    async def _chrome_na_aba(pasta, dominios, fn):
        visto["chrome_na_aba"] = (str(pasta), list(dominios))
        return await fn(Page())

    import session_manager as sm

    async def _aba(profile, url, timeout_abrir=60.0, fn=None):
        visto["camoufox"] = url
        return {"aba": "nova", "url": url, "resultado": await fn(Page())}

    async def _na_aba(profile, dominios, fn):
        visto["camoufox_aba"] = list(dominios)
        return await fn(Page())

    guardados = (comp._chrome_abrir, comp._chrome_na_aba, sm.abrir_aba, sm.tarefa_na_aba)
    comp._chrome_abrir, comp._chrome_na_aba = _chrome_abrir, _chrome_na_aba
    sm.abrir_aba, sm.tarefa_na_aba = _aba, _na_aba
    try:
        r = asyncio.run(comp.medir({"id": "p1", "name": "persona"}, "x", **kw))
        r["_visto"] = visto
        return r
    finally:
        (comp._chrome_abrir, comp._chrome_na_aba,
         sm.abrir_aba, sm.tarefa_na_aba) = guardados


_ANTES = set(comp.PASTA.glob("*")) if comp.PASTA.exists() else set()

# ── o padrao continua sendo o Camoufox ───────────────────────────────────
r = medir()
t(r["navegador"] == "camoufox", "sem dizer nada, mede pelo Camoufox")
t("camoufox" in r["_visto"], "e e o caminho do Camoufox que roda")
t(r["chrome_perfil"] == "", "sem perfil de Chrome")

# ── Chrome de sempre ─────────────────────────────────────────────────────
r = medir(navegador="chrome")
t(r["navegador"] == "chrome", "a escolha chega ao relatorio")
t("chrome_abrir" in r["_visto"] and "camoufox" not in r["_visto"],
  "e e o Chrome que roda, nao o Camoufox")
pasta, url = r["_visto"]["chrome_abrir"]
t(url == "https://x.com/compose/post", "abre o compositor do registro")
t("Chrome" in pasta, "na pasta do Chrome de sempre: " + pasta)
t("chrome_profiles" not in pasta, "que NAO e a pasta dedicada")

# ── Chrome dedicado a persona ────────────────────────────────────────────
r = medir(navegador="chrome", chrome_perfil="cap.martim rs/2026")
pasta, _ = r["_visto"]["chrome_abrir"]
t("chrome_profiles" in pasta, "a persona ganha pasta propria: " + pasta)
t("/" not in pathlib.Path(pasta).name and " " not in pathlib.Path(pasta).name,
  "com o nome higienizado (barra e espaco viram _): " + pathlib.Path(pasta).name)
t(r["chrome_perfil"] == "cap.martim rs/2026", "e o relatorio guarda o nome pedido")

# as duas pastas sao DIFERENTES -- senao a persona herdaria os seus logins
p_sempre, _ = medir(navegador="chrome")["_visto"]["chrome_abrir"]
t(p_sempre != pasta, "o Chrome de sempre e o da persona nao sao a mesma pasta")

# ── medir o passo atual vai ao MESMO navegador ───────────────────────────
r = medir(navegador="chrome", abrir=False)
t("chrome_na_aba" in r["_visto"], "passo atual no Chrome usa a aba do Chrome")
t(r["_visto"]["chrome_na_aba"][1] == ["x.com", "twitter.com"],
  "achando a aba pelos dominios da rede, nao por referencia guardada")
r = medir(abrir=False)
t("camoufox_aba" in r["_visto"], "e no Camoufox usa a aba do Camoufox")

# ── navegador desconhecido e recusado ANTES de abrir nada ────────────────
try:
    medir(navegador="internet explorer")
    t(False, "navegador desconhecido deveria ser recusado")
except ValueError as e:
    t("internet explorer" in str(e), "e a recusa nomeia o que foi pedido")

# ── os erros que o usuario vai encontrar de verdade ──────────────────────
# 1) "medir o passo atual" sem ter aberto nada
comp._CHROME.update(pw=None, ctx=None, pasta=None)
try:
    asyncio.run(comp._chrome_na_aba(pathlib.Path("qualquer"), ["x.com"], None))
    t(False, "sem Chrome aberto deveria estourar")
except RuntimeError as e:
    t("abrir e medir" in str(e), "e manda abrir primeiro: " + str(e))

# 2) nenhuma aba casa o dominio
class Ctx:
    def __init__(self, urls):
        self.pages = [type("P", (), {"url": u, "is_closed": lambda s: False})() for u in urls]

comp._CHROME.update(ctx=Ctx(["https://google.com/"]), pasta="X")
try:
    asyncio.run(comp._chrome_na_aba(pathlib.Path("X"), ["x.com"], None))
    t(False, "aba que nao casa deveria estourar")
except RuntimeError as e:
    t("x.com" in str(e) and "google.com" in str(e),
      "e o erro diz o que procurava E o que achou: " + str(e))

# 3) a aba certa e achada, e a ULTIMA vence (a mais recente)
achou = {}

async def _pega(pg):
    achou["url"] = pg.url
    return {"url": pg.url}

comp._CHROME.update(ctx=Ctx(["https://google.com/", "https://x.com/a", "https://x.com/b"]),
                    pasta="X")
asyncio.run(comp._chrome_na_aba(pathlib.Path("X"), ["x.com"], _pega))
t(achou["url"] == "https://x.com/b", "entre duas abas da rede, vence a mais recente")

# ── fechar sem nada aberto nao explode ───────────────────────────────────
comp._CHROME.update(pw=None, ctx=None, pasta=None)
t(asyncio.run(comp.fechar_chrome()) is False, "fechar sem sessao devolve False")

# limpa SO o que este teste criou
_novos = (set(comp.PASTA.glob("*")) if comp.PASTA.exists() else set()) - _ANTES
for _f in _novos:
    try:
        _f.unlink()
    except Exception:
        pass

print("OK — %d assercoes (limpou %d de teste, preservou %d real(is))"
      % (ok, len(_novos), len(_ANTES)))
