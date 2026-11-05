# -*- coding: utf-8 -*-
"""Adaptacao automatica por rede (2026-11-05).

A pedido: a aba de publicacao precisa mostrar uma previa do que vai sair E
adaptar sozinha para cada plataforma.

A previa por rede ja existia. O que faltava era a ADAPTACAO: o plano RECUSAVA a
rede quando a legenda nao cabia ("personalize a legenda desta rede") ou quando
o destino nao existia la. Ou seja, o app sabia resolver e mandava o usuario
resolver.

O que este arquivo trava:

  - a legenda e cortada NA PALAVRA, e o LINK sobrevive ao corte. Cortar o texto
    e levar o link junto deixaria a publicacao sem a unica coisa que ela existe
    para fazer;
  - se nem o link cabe, NAO adapta -- recusa com motivo, em vez de publicar um
    link pela metade, que nao clica e ninguem percebe;
  - o destino vira o que a rede TEM, e o formato acompanha;
  - video no Instagram vira Reels sozinho;
  - **imagem nao vira video**: rede so-de-video continua recusada. Adaptar e
    escolher entre o que existe; o resto seria fingir;
  - e tudo o que foi ajustado sai em `adaptado`, porque adaptacao que nao
    aparece e adaptacao que engana.

Puro: sem rede, sem banco, sem navegador. Rodar com:

    venv\\Scripts\\python.exe tests\\test_adaptar.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import redes  # noqa: E402

ok = 0


def t(cond, msg):
    global ok
    assert cond, "FALHOU: " + msg
    ok += 1


LINK = "https://chat.whatsapp.com/ABC123"


# ══ O ENCURTADOR ═════════════════════════════════════════════════════════
texto, nota = redes.encurtar_legenda("cabe tranquilo", 280)
t(texto == "cabe tranquilo" and nota == "",
  "legenda que cabe nao e tocada, e nao gera nota")

longa = "palavra " * 60
texto, nota = redes.encurtar_legenda(longa, 100)
t(len(texto) <= 100, "corta para caber: %d" % len(texto))
t(texto.endswith("…"), "e marca que foi cortada")
t(not texto[:-1].rstrip().endswith("palav"),
  "cortando na PALAVRA, nao no meio dela")

# o link e o que leva a pessoa ao destino -- ele nao pode ser o cortado
com_link = ("venha para o grupo " * 20) + LINK
texto, nota = redes.encurtar_legenda(com_link, 280, LINK)
t(len(texto) <= 280, "cabe no limite com o link dentro: %d" % len(texto))
t(LINK in texto, "e o LINK sobreviveu ao corte")
t(texto.endswith(LINK), "ficando no fim, onde se clica")
t("link preservado" in nota, "a nota diz que o link foi protegido")

# palavra unica gigante (URL colada, hashtag enorme): corte no seco
texto, _ = redes.encurtar_legenda("A" * 300, 50)
t(len(texto) <= 50, "palavra unica e cortada no seco em vez de estourar")

# se nem o link cabe, nao ha adaptacao honesta
texto, nota = redes.encurtar_legenda("oi " * 30 + LINK, 20, LINK)
t(texto is None, "link maior que o limite -> nao adapta")
t("o link sozinho tem" in nota, "e diz por que: " + nota)


# ══ A ADAPTACAO POR REDE ═════════════════════════════════════════════════
def plano(conteudo, redes_pedidas=None, **kw):
    contas = [{"id": "i" + r, "platform": r, "status": "active", "username": "u"}
              for r in ("instagram", "x", "threads", "tiktok", "facebook")]
    return {d.rede_id: d for d in redes.montar_plano(contas, conteudo,
                                                     redes_pedidas, **kw)}

# 380 caracteres: CABE nos 500 do Threads e NAO cabe nos 280 do X. O
# contraste e o ponto -- e so com ele da para ver que a adaptacao mexe no
# que cada rede precisa, e nao em todas por igual.
legenda = ("Entre no nosso grupo e receba as noticias em primeira mao. " * 6) + LINK
p = plano({"arquivo": "arte.jpg", "legenda": legenda, "link": LINK,
           "destino": "story"})

# o X nao tem story E a legenda nao cabe: as DUAS coisas sao resolvidas
x = p["x"]
t(x.publica is True, "o X passou a publicar -- antes era recusado duas vezes")
t(x.destino == "feed", "destino trocado para o que o X tem")
t(len(x.legenda) <= 280, "legenda dentro dos 280 do X: %d" % len(x.legenda))
t(LINK in x.legenda, "com o link intacto")
t(len(x.adaptado) == 2, "e os DOIS ajustes aparecem na lista")
t(any("destino" in m for m in x.adaptado) and any("legenda" in m for m in x.adaptado),
  "um de destino e um de legenda: %s" % list(x.adaptado))

# o formato acompanha o destino novo, senao a arte sairia no tamanho do antigo
t(x.formato == "paisagem", "o formato seguiu o destino novo: " + x.formato)

# o Threads tem 500: so o destino muda, a legenda cabe
th = p["threads"]
t(th.destino == "feed" and len(th.adaptado) == 1,
  "no Threads so o destino precisou mudar")
t(th.legenda == legenda, "a legenda dele fica inteira -- 380 cabe nos 500")
t(280 < len(legenda) <= 500, "o teste so vale com legenda entre os dois limites: %d" % len(legenda))

# o Instagram e o Facebook TEM story: nada muda
t(p["instagram"].destino == "story" and not p["instagram"].adaptado,
  "rede que tem story nao e adaptada")
t(p["facebook"].destino == "story", "o Facebook tambem tem story")

# ⚠ o TikTok so aceita video: imagem NAO vira video
t(p["tiktok"].publica is False, "imagem continua recusada no TikTok")
t("aceita video" in p["tiktok"].motivo,
  "com o motivo do FORMATO, nao do destino: " + p["tiktok"].motivo)


# ══ VIDEO: o Instagram vira Reels sozinho ════════════════════════════════
pv = plano({"arquivo": "clipe.mp4", "legenda": "curta", "destino": "feed",
            "capa": "capa.jpg"})
ig = pv["instagram"]
t(ig.publica is True and ig.destino == "reels",
  "video no Instagram vira Reels sozinho -- a instrucao virou acao")
t(any("reels" in m for m in ig.adaptado), "e o ajuste e declarado: %s" % list(ig.adaptado))
t(ig.formato == "story", "com o formato 9:16 do Reels")
t(pv["tiktok"].publica is True, "e o TikTok aceita video sem precisar de ajuste")


# ══ DESLIGAR devolve o comportamento antigo ══════════════════════════════
sem = plano({"arquivo": "arte.jpg", "legenda": legenda, "link": LINK,
             "destino": "story"}, adaptar=False)
t(sem["x"].publica is False, "sem adaptar, o X volta a ser recusado")
t("não tem story" in sem["x"].motivo, "pelo motivo de sempre")
t(not sem["x"].adaptado, "e nada aparece como adaptado")


# ══ A adaptacao roda DEPOIS da personalizacao ════════════════════════════
# O que o usuario escreveu para aquela rede e o ponto de partida; a adaptacao
# so mexe no que ainda nao cabe.
pp = plano({"arquivo": "arte.jpg", "legenda": legenda, "link": LINK,
            "destino": "story"},
           personalizacoes={"x": {"legenda": "curtinha minha"}})
t(pp["x"].legenda == "curtinha minha",
  "legenda personalizada que ja cabe nao e mexida pela adaptacao")
t(not any("legenda" in m for m in pp["x"].adaptado),
  "e nao aparece como ajuste, porque nao foi")
t(any("destino" in m for m in pp["x"].adaptado),
  "mas o destino continua sendo adaptado")


# ══ O que foi adaptado chega na tela ═════════════════════════════════════
d = plano({"arquivo": "arte.jpg", "legenda": legenda, "link": LINK,
           "destino": "story"})["x"].como_dict()
t(isinstance(d.get("adaptado"), list) and d["adaptado"],
  "o dict da tela carrega a lista de ajustes")
t(d["legenda"] == plano({"arquivo": "arte.jpg", "legenda": legenda,
                         "link": LINK, "destino": "story"})["x"].legenda,
  "e a legenda do dict e a ADAPTADA, que e a que vai sair")

print("OK — %d assercoes" % ok)
