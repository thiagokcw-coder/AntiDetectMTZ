# -*- coding: utf-8 -*-
"""O medidor sabendo DENTRO DE QUE cada controle esta (2026-11-04).

Nasceu da medicao do Threads: "Postar" aparece DUAS vezes na mesma pagina -- o
compositor da linha do tempo e o do modal -- e os dois so tem classe atomica da
Meta. Sem saber o conteiner de cada um, escolher seria adivinhar qual e qual,
que e o que estas medicoes existem para impedir.

Puro: sem rede, sem navegador, sem banco. Rodar com:

    venv\\Scripts\\python.exe tests\\test_medidor_dialogo.py

(O trecho de JS que CALCULA a ancora nao da para exercitar aqui -- precisa de
DOM. Ele foi conferido contra uma pagina real que reproduz a armadilha; o que
este arquivo trava e o lado Python: que a preferencia pelo modal existe, que ela
nao estraga a tela sem modal, e que o relatorio carrega a ancora adiante.)
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import compositor as comp

ok = 0


def t(cond, msg):
    global ok
    assert cond, "FALHOU: " + msg
    ok += 1


def botao(texto, dialogo=False, ancora="", visivel=True):
    return {"seletor": "div.x1 > div.x2", "texto": texto, "aria": "",
            "data_e2e": "", "data_testid": "", "desabilitado": False,
            "visivel": visivel, "em_dialogo": dialogo, "ancora": ancora}


DIALOGO = '[role="dialog"][aria-label="Novo tópico"]'


# ── A ARMADILHA DO THREADS: dois "Postar", so um publica ─────────────────
tela = {
    "botoes": [botao("Postar"), botao("Postar", True, DIALOGO)],
    "textos": [
        {"seletor": "div.a", "tag": "div", "papel": "textbox", "aria": "",
         "placeholder": "", "limite": "", "visivel": True, "em_dialogo": False, "ancora": ""},
        {"seletor": "div.b", "tag": "div", "papel": "textbox", "aria": "",
         "placeholder": "", "limite": "", "visivel": True, "em_dialogo": True, "ancora": DIALOGO},
    ],
    "arquivos": [
        {"seletor": "input.fora", "nome": "", "id": "", "aceita": "image/png",
         "varios": False, "visivel": False, "em_dialogo": False, "ancora": ""},
        {"seletor": "input.dentro", "nome": "", "id": "", "aceita": "image/jpeg",
         "varios": False, "visivel": False, "em_dialogo": True, "ancora": DIALOGO},
    ],
    "clicaveis": [],
}

p = comp._palpite(tela)
t(p["publicar"][0]["em_dialogo"] is True,
  "entre dois 'Postar' iguais, o do MODAL vem primeiro")
t(p["publicar"][0]["ancora"] == DIALOGO,
  "e ele traz a ancora que o torna inequivoco: " + p["publicar"][0]["ancora"])
t(len(p["publicar"]) == 2, "o de fora continua na lista, so que depois")
t(p["legenda"]["seletor"] == "div.b", "o editor do modal vence o da linha do tempo")
t(p["arquivo"]["seletor"] == "input.dentro",
  "e o input de arquivo do modal vence o de fora")


# ── Sem modal, a ordem NAO muda ──────────────────────────────────────────
# Senao esta regra estaria reordenando telas onde ela nao tem nada a dizer --
# TikTok e X nao usam modal com role=dialog no fluxo medido.
sem = {"botoes": [botao("Publicar"), botao("Enviar")], "textos": [], "arquivos": [],
       "clicaveis": []}
t([b["texto"] for b in comp._palpite(sem)["publicar"]] == ["Publicar", "Enviar"],
  "tela sem modal mantem a ordem do documento")

t(comp._do_dialogo_primeiro([]) == [], "lista vazia nao explode")
so_fora = [botao("a"), botao("b")]
t(comp._do_dialogo_primeiro(so_fora) == so_fora, "nada no modal = nada muda")


# ── O VISIVEL ainda manda dentro do grupo ────────────────────────────────
# Um input oculto no modal continua sendo o certo (as redes escondem o
# input[type=file]), mas entre dois do mesmo grupo o visivel vem antes.
mistura = {"botoes": [botao("Postar", True, DIALOGO, visivel=False),
                      botao("Postar", True, DIALOGO, visivel=True)],
           "textos": [], "arquivos": [], "clicaveis": []}
t(comp._palpite(mistura)["publicar"][0]["visivel"] is True,
  "entre dois do modal, o visivel vem primeiro")


# ── O leitor pede a ancora para TODOS os coletores ───────────────────────
js = comp._LER_TELA
for campo in ("ancora: ancora(el)", "em_dialogo: emDialogo(el)"):
    t(js.count(campo) >= 3, "o leitor coleta %r nas tres listas (%d)" % (campo, js.count(campo)))
t("role=\"dialog\"" in js or "role='dialog'" in js,
  "e sabe procurar um modal")
t("tem_dialogo" in js, "o relatorio diz se havia modal na tela")

print("OK — %d assercoes" % ok)
