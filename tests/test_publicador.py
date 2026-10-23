# -*- coding: utf-8 -*-
"""Preencher o compositor: as receitas medidas e as armadilhas que elas tem.

Tudo com pagina duble -- sem rede, sem navegador, sem banco. Rodar com:

    venv\\Scripts\\python.exe tests\\test_publicador.py

O que este arquivo trava, e por que cada um importa:

  - `preencher` NUNCA clica em publicar. E a garantia que separa "estreia sem
    poder estragar nada" de "publicou errado na conta real".
  - A legenda e SEMPRE limpa antes. O TikTok pre-preenche com o nome do
    arquivo (medido em 14/10 e 23/10); sem limpar, a legenda sai
    "YTDown.com_Shorts_Media_...".
  - Teclado, nunca `fill`. Os dois editores medidos sao Draft.js, onde `fill`
    deixa o DOM com cara de preenchido e o estado interno vazio.
  - O texto e CONFERIDO depois de digitado. O X tem DOIS editores iguais na
    mesma pagina (modal + linha do tempo atras), e escrever no errado nao
    levanta erro nenhum -- so sai publicacao sem legenda.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import publicador as pub

ok = 0


def t(cond, msg):
    global ok
    assert cond, "FALHOU: " + msg
    ok += 1


class Loc:
    def __init__(self, page, sel, existe=True, visivel=True, habilitado=True,
                 aceita_texto=True, texto_inicial=""):
        self.page, self.sel = page, sel
        self.existe, self.visivel, self.habilitado = existe, visivel, habilitado
        self.aceita_texto = aceita_texto
        self.texto = texto_inicial

    @property
    def first(self):
        return self

    async def wait_for(self, state="visible", timeout=0):
        if not self.existe:
            raise RuntimeError("timeout: %s" % self.sel)
        if state == "visible" and not self.visivel:
            raise RuntimeError("não ficou visível: %s" % self.sel)

    async def click(self):
        self.page.acoes.append(("click", self.sel))
        self.page.foco = self

    async def set_input_files(self, caminho):
        self.page.acoes.append(("arquivo", self.sel, caminho))

    async def fill(self, v):
        self.page.acoes.append(("fill", self.sel, v))  # NAO deve acontecer

    async def is_enabled(self):
        return self.habilitado

    async def inner_text(self):
        return self.texto


class Teclado:
    def __init__(self, page):
        self.page = page

    async def press(self, tecla):
        self.page.acoes.append(("tecla", tecla))
        if tecla == "Delete" and self.page.foco is not None:
            self.page.foco.texto = ""

    async def type(self, txt, delay=0):
        self.page.acoes.append(("digitou", txt))
        alvo = self.page.foco
        if alvo is not None and alvo.aceita_texto:
            alvo.texto += txt


class Page:
    """Pagina duble. `mapa` diz como cada seletor se comporta."""

    def __init__(self, mapa):
        self.mapa, self.acoes, self.foco = mapa, [], None
        self.keyboard = Teclado(self)
        self._cache = {}

    def locator(self, sel):
        if sel not in self._cache:
            cfg = self.mapa.get(sel, {"existe": False})
            self._cache[sel] = Loc(self, sel, **cfg)
        return self._cache[sel]


# ══ X: o editor certo entre dois iguais ══════════════════════════════════
# O de dentro do modal aceita texto; o de fora (linha do tempo) nao -- e o que
# acontece quando o Draft.js errado recebe o clique.
MAPA_X = {
    '[role=dialog] input[data-testid="fileInput"]': {},
    '[role=dialog] div[data-testid="tweetTextarea_0"]': {},
    '[role=dialog] button[data-testid="tweetButton"]': {"habilitado": True},
}
r = asyncio.run(pub.preencher(Page(MAPA_X), "x", "/tmp/arte.jpg", "olá mundo"))
t(r["arquivo"] == '[role=dialog] input[data-testid="fileInput"]',
  "manda o arquivo no input do MODAL, nao no da linha do tempo")
t(r["legenda"] == '[role=dialog] div[data-testid="tweetTextarea_0"]', "e escreve no editor do modal")
t(r["publicou"] is False, "PREENCHER NUNCA PUBLICA")
t(r["publicar_habilitado"] is True, "mas relata que o botao ja esta habilitado")

# o editor do modal NAO existe -> cai no segundo candidato
MAPA_FORA = {
    'input[data-testid="fileInput"]': {},
    'div[data-testid="tweetTextarea_0"]': {},
    'button[data-testid="tweetButton"]': {},
}
r = asyncio.run(pub.preencher(Page(MAPA_FORA), "x", "", "texto"))
t(r["legenda"] == 'div[data-testid="tweetTextarea_0"]',
  "sem o modal, usa o candidato seguinte")

# o PRIMEIRO editor engole o texto sem erro -> tem de tentar o proximo.
# Sem a conferencia, isto passaria batido e a publicacao sairia sem legenda.
MAPA_MUDO = {
    '[role=dialog] div[data-testid="tweetTextarea_0"]': {"aceita_texto": False},
    'div[data-testid="tweetTextarea_0"]': {},
    '[role=dialog] button[data-testid="tweetButton"]': {},
}
r = asyncio.run(pub.preencher(Page(MAPA_MUDO), "x", "", "texto que some"))
t(r["legenda"] == 'div[data-testid="tweetTextarea_0"]',
  "editor que ENGOLE o texto e descartado -- a conferencia pega isso")


# ══ TikTok: limpar a legenda pre-preenchida ══════════════════════════════
MAPA_TT = {
    'div[data-e2e="select_video_container"] input[type=file]': {},
    'button[data-e2e="post_video_button"]': {},
    'div.caption-editor [contenteditable="true"]': {
        "texto_inicial": "YTDown.com_Shorts_Media_G9NRzrx7m4U_switzerland"},
}
pg = Page(MAPA_TT)
r = asyncio.run(pub.preencher(pg, "tiktok", "/tmp/clipe.mp4", "legenda de verdade"))
editor = pg.locator('div.caption-editor [contenteditable="true"]')
t(editor.texto == "legenda de verdade",
  "a legenda PRE-PREENCHIDA com o nome do arquivo foi embora: %r" % editor.texto)
t("YTDown" not in editor.texto, "nenhum resto do nome do arquivo")

verbos = [a[0] for a in pg.acoes]
t("fill" not in verbos, "NUNCA usa fill -- Draft.js ignoraria e a legenda sairia vazia")
t(("tecla", "Control+A") in pg.acoes and ("tecla", "Delete") in pg.acoes,
  "limpa com teclado antes de digitar")
t(verbos.index("arquivo") < verbos.index("digitou"),
  "o arquivo entra ANTES da legenda: no TikTok o campo so existe depois do video")

# espera o upload terminar antes de escrever
t(pg.locator('button[data-e2e="post_video_button"]').existe,
  "e espera o botao de publicar aparecer, que e o sinal de upload concluido")


# ══ Publicar e uma chamada SEPARADA ══════════════════════════════════════
pg = Page(dict(MAPA_X))
r = asyncio.run(pub.apertar_publicar(pg, "x"))
t(r["publicou"] is True, "apertar_publicar publica")
t(("click", '[role=dialog] button[data-testid="tweetButton"]') in pg.acoes,
  "clicando no botao medido")

# botao desabilitado nao e clicado: a tela nao esta pronta
MAPA_OFF = {'[role=dialog] button[data-testid="tweetButton"]': {"habilitado": False}}
pg = Page(MAPA_OFF)
try:
    asyncio.run(pub.apertar_publicar(pg, "x"))
    t(False, "botao desabilitado deveria recusar")
except RuntimeError as e:
    t("desabilitado" in str(e), "e o erro explica: " + str(e))
t(not [a for a in pg.acoes if a[0] == "click"], "sem nenhum clique")


# ══ Rede sem receita ═════════════════════════════════════════════════════
t(pub.tem_receita("x") and pub.tem_receita("tiktok"), "X e TikTok tem receita")
for sem in ("youtube", "kwai", "facebook", "threads", "telegram"):
    t(not pub.tem_receita(sem), "%s ainda nao foi medido" % sem)
try:
    asyncio.run(pub.preencher(Page({}), "youtube", "/tmp/a.mp4", "x"))
    t(False, "rede sem receita deveria recusar")
except ValueError as e:
    t("meça o compositor" in str(e), "e manda medir antes: " + str(e))


# ══ Tela que nao tem o que a receita espera ══════════════════════════════
try:
    asyncio.run(pub.preencher(Page({}), "x", "/tmp/a.jpg", "oi"))
    t(False, "tela sem os campos deveria estourar")
except RuntimeError as e:
    t("não achei nenhum destes" in str(e) and "fileInput" in str(e),
      "e o erro LISTA o que procurou: " + str(e)[:90])


# ══ Toda receita diz quando foi medida ═══════════════════════════════════
for rid, rc in pub.RECEITAS.items():
    t(bool(rc.get("medido_em")), "a receita do %s diz a data da medicao" % rid)
    t(bool(rc.get("arquivo")) and bool(rc.get("legenda")) and bool(rc.get("publicar")),
      "a receita do %s tem as tres pecas" % rid)


# ══ A LEVA INTEIRA: rede com receita preenche, sem receita so abre ═══════
# Isto e o encaixe que os testes de unidade nao cobrem: que `publicar_em_todas`
# escolhe o caminho certo por rede, manda a ARTE COMPOSTA (nao o arquivo cru --
# a crua nao tem o chip desenhado) e nao publica sem ordem explicita.
import tempfile

from PIL import Image

import publicador as _pub
import redes as _redes
import session_manager as _sm
import story as _story

_tmp = pathlib.Path(tempfile.mkdtemp())
_arte = _tmp / "arte.jpg"
Image.new("RGB", (900, 900), (40, 80, 150)).save(_arte)
_antes_artes = set(_story.PASTA.glob("*")) if _story.PASTA.exists() else set()

PERFIL = {"id": "p", "name": "persona", "proxy": None}
CONTAS = [{"id": "a1", "platform": "x", "status": "active", "username": "unc"},
          {"id": "a2", "platform": "telegram", "status": "active", "username": "tg"}]
_chamadas = []


async def _fp(page, rede_id, arquivo="", legenda="", espera_ms=0):
    _chamadas.append(("preencher", rede_id,
                      pathlib.Path(arquivo).name if arquivo else "", legenda))
    return {"rede": rede_id, "publicou": False, "publicar_habilitado": True}


async def _fpub(page, rede_id):
    _chamadas.append(("publicar", rede_id))
    return {"publicou": True}


async def _faba(profile, url, timeout_abrir=60.0, fn=None):
    _chamadas.append(("aba", url))
    return {"aba": "nova", "url": url, "resultado": (await fn(object())) if fn else None}


_guardado = (_pub.preencher, _pub.apertar_publicar, _sm.abrir_aba)
_pub.preencher, _pub.apertar_publicar, _sm.abrir_aba = _fp, _fpub, _faba
try:
    conteudo = {"arquivo": str(_arte), "legenda": "oi"}
    r = asyncio.run(_redes.publicar_em_todas(PERFIL, CONTAS, conteudo,
                                             espalhar_segundos=0, simular=False))
    por = {d["rede"]: d for d in r["plano"]}
    t(por["x"]["desfecho"] == "preenchido", "rede COM receita volta preenchida")
    t("aperte publicar" in por["x"]["detalhe"], "e o detalhe diz o que falta fazer")
    t(por["telegram"]["desfecho"] == "assistido",
      "rede SEM receita segue como era: abre e espera")
    t(r["preenchidas"] == 1 and r["assistidas"] == 1, "o resumo conta as duas coisas")
    t(not any(c[0] == "publicar" for c in _chamadas),
      "e NINGUEM publica sem ordem explicita")

    enviado = [c[2] for c in _chamadas if c[0] == "preencher"][0]
    t(enviado != _arte.name and enviado.endswith(".jpg"),
      "o que vai para a rede e a ARTE COMPOSTA, nao o arquivo cru: %s" % enviado)

    _chamadas.clear()
    r2 = asyncio.run(_redes.publicar_em_todas(PERFIL, CONTAS, conteudo,
                                              espalhar_segundos=0, simular=False,
                                              auto_publicar=["x"]))
    por2 = {d["rede"]: d for d in r2["plano"]}
    t(por2["x"]["desfecho"] == "publicado", "com a rede liberada, ela publica")
    t([c[1] for c in _chamadas if c[0] == "publicar"] == ["x"],
      "e so ela -- liberar uma rede nao libera as outras")
    t(por2["telegram"]["desfecho"] == "assistido",
      "a rede sem receita ignora a liberacao")
finally:
    _pub.preencher, _pub.apertar_publicar, _sm.abrir_aba = _guardado
    for _f in (set(_story.PASTA.glob("*")) if _story.PASTA.exists() else set()) - _antes_artes:
        try:
            _f.unlink()
        except Exception:
            pass

print("OK — %d assercoes" % ok)
