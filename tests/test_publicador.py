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

    async def count(self):
        return 1 if self.existe else 0

    async def wait_for(self, state="visible", timeout=0):
        if not self.existe:
            raise RuntimeError("timeout: %s" % self.sel)
        if state == "visible" and not self.visivel:
            raise RuntimeError("não ficou visível: %s" % self.sel)

    async def click(self):
        self.page.acoes.append(("click", self.sel))
        self.page.foco = self
        # Um clique pode CRIAR tela (o "Novo post" do Threads abre o modal).
        # Sem isto o duble so saberia testar telas estaticas, e justo o passo
        # de abrir ficaria sem cobertura.
        for sel in self.page.nasceNoClique.get(self.sel, []):
            self.page.mapa.setdefault(sel, {})["existe"] = True
            self.page._cache.pop(sel, None)

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

    def __init__(self, mapa, nasceNoClique=None):
        self.mapa, self.acoes, self.foco = mapa, [], None
        self.nasceNoClique = nasceNoClique or {}
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


# ══ Facebook: anel por role, porque aria-label carrega o NOME DA CONTA ═══
# Medido em 23/10: o editor e `div[aria-label="Em que estas a pensar,
# Uncensored?"]` -- nome da conta E idioma dentro do seletor. Por isso a receita
# ancora em role=textbox, que e unico na tela (a busca e input/combobox).
MAPA_FB = {
    'input[type="file"][accept*="image"]': {},
    'div[role="textbox"][contenteditable="true"]': {},
    'div[aria-label="Publicar"]': {"habilitado": False},
}
pg = Page(MAPA_FB)
r = asyncio.run(pub.preencher(pg, "facebook", "/tmp/arte.jpg", "oi pessoal"))
t(r["legenda"] == 'div[role="textbox"][contenteditable="true"]',
  "ancora no papel, nunca no aria-label que tem o nome da conta")
t(r["arquivo"] == 'input[type="file"][accept*="image"]',
  "e filtra o input pelo accept: o terceiro da tela e so video")
t(r["publicar_habilitado"] is False,
  "relata que o botao ainda esta desabilitado em vez de fingir que esta pronto")
t(r["publicou"] is False, "e nao publica")

# o botao de publicar e LOCALIZADO; a receita lista idiomas em vez de assumir um
MAPA_EN = {
    'input[type="file"][accept*="image"]': {},
    'div[role="textbox"][contenteditable="true"]': {},
    'div[aria-label="Post"]': {},
}
r = asyncio.run(pub.preencher(Page(MAPA_EN), "facebook", "", "hello"))
t(r["publicar"] == 'div[aria-label="Post"]',
  "interface em ingles tambem acha o botao")

t(pub.receita("facebook")["url"].endswith("/post/create"),
  "vai direto para o compositor, sem depender do gatilho do feed")


# ══ THREADS: compositor MODAL, e dois "Postar" na mesma pagina ═══════════
# Medido em 04/11. A URL nao muda ao abrir o compositor (ao contrario do
# Facebook, que tem /post/create), entao esta e a unica receita com um passo de
# CLIQUE antes de preencher. E tudo e ancorado em [role=dialog] porque "Postar"
# existe duas vezes: o da linha do tempo e o do modal.
MAPA_TH_FECHADO = {
    'div[role="button"]:text-is("Novo post")': {},
    # o modal ainda nao existe -> o textbox so aparece depois do clique
    '[role="dialog"] div[role="textbox"]': {"existe": False},
}
pg = Page(MAPA_TH_FECHADO)
try:
    asyncio.run(pub.preencher(pg, "threads", "", "oi"))
    t(False, "com o modal fechado e o textbox ausente, deveria estourar")
except RuntimeError:
    t(True, "sem o modal, o preenchimento para com erro em vez de seguir no vazio")
t(("click", 'div[role="button"]:text-is("Novo post")') in pg.acoes,
  "mas ANTES disso ele clicou em Novo post para tentar abrir")

# modal FECHADO que abre com o clique -- o caminho de verdade
GATILHO = 'div[role="button"]:text-is("Novo post")'
DO_MODAL = ['[role="dialog"] div[role="textbox"]',
            '[role="dialog"] input[type="file"][accept*="image"]',
            '[role="dialog"] div[role="button"]:text-is("Postar")']
MAPA_TH = dict({GATILHO: {}}, **{sel: {"existe": False} for sel in DO_MODAL})
pg = Page(MAPA_TH, nasceNoClique={GATILHO: DO_MODAL})
r = asyncio.run(pub.preencher(pg, "threads", "/tmp/arte.jpg", "ola threads"))
t(r["abriu_compositor"] == 'div[role="button"]:text-is("Novo post")',
  "abriu o compositor pelo gatilho medido")
t(r["legenda"].startswith('[role="dialog"]'),
  "e escreve DENTRO do modal: " + r["legenda"])
t(r["publicar"].startswith('[role="dialog"]'),
  "o botao de publicar tambem e o do modal, nunca o da linha do tempo")
t(r["publicou"] is False, "e segue sem publicar")
ordem = [a[0] for a in pg.acoes]
t(ordem.index("click") < ordem.index("arquivo"),
  "o clique de abrir vem ANTES do arquivo: o input so existe dentro do modal")

# ⚠ o caso perigoso: modal JA ABERTO. Clicar de novo no "Novo post" podia
# fechar o modal, abrir um segundo ou cair num menu.
MAPA_TH_ABERTO = dict({GATILHO: {}}, **{sel: {} for sel in DO_MODAL})
pg = Page(MAPA_TH_ABERTO)
r = asyncio.run(pub.preencher(pg, "threads", "", "ja aberto"))
t(r["abriu_compositor"] == "já estava aberto",
  "com o modal aberto, ele NAO clica de novo")
t(not [a for a in pg.acoes
       if a[0] == "click" and 'Novo post' in a[1]],
  "nenhum clique no gatilho -- conferir o estado antes custa menos que descobrir depois")

# as outras tres NAO tem passo de abrir: o Facebook tem URL propria, X e TikTok
# abrem prontos. Um clique a mais ali seria clique no escuro.
for rede in ("x", "tiktok", "facebook"):
    t(not pub.receita(rede).get("abrir_compositor"),
      "%s nao precisa de clique para abrir o compositor" % rede)


# ══ Rede sem receita ═════════════════════════════════════════════════════
t(all(pub.tem_receita(r) for r in ("x", "tiktok", "facebook", "threads")),
  "X, TikTok, Facebook e Threads tem receita")
for sem in ("youtube", "kwai", "telegram"):
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
