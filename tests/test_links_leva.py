# -*- coding: utf-8 -*-
"""Os links que a leva devolve (2026-11-06).

Pedido: depois de publicar, poder ABRIR o que saiu. "Publicado" e uma
afirmacao; o link e o que deixa conferir.

A assimetria aqui e a parte que importa, e e honesta:

  - **Instagram devolve o permalink.** A API do app da o shortcode, e e ele
    que entra na URL -- o `pk` sozinho nao abre nada.
  - **As outras nao.** Depois do clique no compositor, a rede nao nos diz o
    endereco da publicacao; descobrir exigiria medir a tela de "publicado" de
    cada uma. Entao sobra o PERFIL, que e um link certo -- e a tela chama de
    "conferir no perfil", nunca de "ver publicacao".

Inventar uma URL daria link quebrado, que custa um clique para a pessoa
descobrir que e mentira. Link que nao abre e pior que link nenhum.

Puro: sem rede, sem navegador. Rodar com:

    venv\\Scripts\\python.exe tests\\test_links_leva.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import redes  # noqa: E402
import story  # noqa: E402

ok = 0


def t(cond, msg):
    global ok
    assert cond, "FALHOU: " + msg
    ok += 1


class Media:
    def __init__(self, pk, code=None):
        self.pk = pk
        if code is not None:
            self.code = code


# ══ O PERMALINK DO INSTAGRAM ═════════════════════════════════════════════
t(story._link_do_post(Media(123, "AbC_12"), "uncensored.brasil", "feed")
  == "https://www.instagram.com/p/AbC_12/",
  "post de feed usa /p/<shortcode>")
t(story._link_do_post(Media(123, "AbC_12"), "uncensored.brasil", "reels")
  == "https://www.instagram.com/reel/AbC_12/",
  "reels usa /reel/<shortcode> -- caminho diferente, mesmo code")
t(story._link_do_post(Media(999), "uncensored.brasil", "story")
  == "https://www.instagram.com/stories/uncensored.brasil/999/",
  "story vive sob o PERFIL e por pk: ele nao tem shortcode")

# o pk sozinho nao abre nada: sem code, melhor nada que um link quebrado
t(story._link_do_post(Media(123), "conta", "feed") == "",
  "feed sem shortcode devolve vazio, nao um /p/123 que nao abre")
t(story._link_do_post(Media(123, ""), "conta", "reels") == "",
  "code vazio idem")
t(story._link_do_post(Media(0), "", "story") == "",
  "story sem conta nem pk tambem nao inventa")


# ══ O PERFIL, para as redes que nao dizem o permalink ════════════════════
casos = {
    "instagram": "https://www.instagram.com/u/",
    "x": "https://x.com/u",
    "tiktok": "https://www.tiktok.com/@u",
    "threads": "https://www.threads.com/@u",
    "youtube": "https://www.youtube.com/@u",
    "facebook": "https://www.facebook.com/u",
    "telegram": "https://t.me/u",
}
for rede, esperado in casos.items():
    t(redes.url_do_perfil(rede, "u") == esperado,
      "%s: %s" % (rede, redes.url_do_perfil(rede, "u")))

# cada rede tem a SUA convencao -- uma regra so daria link quebrado em metade
t(redes.url_do_perfil("x", "u") != redes.url_do_perfil("tiktok", "u"),
  "o arroba entra no TikTok e nao entra no X")

t(redes.url_do_perfil("tiktok", "@ja_tem") == "https://www.tiktok.com/@ja_tem",
  "username que ja vem com arroba nao vira @@")
t(redes.url_do_perfil("tiktok", "  espacos  ") == "https://www.tiktok.com/@espacos",
  "e espacos em volta somem")
t(redes.url_do_perfil("x", "") == "", "sem username, nao ha link")
t(redes.url_do_perfil("x", None) == "", "None idem")
t(redes.url_do_perfil("orkut", "u") == "", "rede desconhecida nao inventa dominio")
t(redes.url_do_perfil("", "u") == "", "rede vazia idem")

# toda rede do registro sabe montar o perfil -- senao a coluna fica vazia
# justamente na rede nova, que e quando ninguem lembra de conferir
for r in redes.REDES:
    t(redes.url_do_perfil(r["id"], "alguem").startswith("http"),
      "o registro tem o perfil do %s" % r["id"])


# ══ O Alvo carrega os dois ate a tela ════════════════════════════════════
d = redes.Alvo(rede_id="x", username="Uncensored_BR")
d.perfil_url = redes.url_do_perfil(d.rede_id, d.username)
dic = d.como_dict()
t(dic["perfil_url"] == "https://x.com/Uncensored_BR", "o perfil chega no dict da tela")
t(dic["url"] == "", "e a publicacao fica vazia quando nao se sabe")

print("OK — %d assercoes" % ok)
