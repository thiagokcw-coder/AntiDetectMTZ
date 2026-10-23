# -*- coding: utf-8 -*-
"""A deteccao de "o site me mandou para o login" -- a regra e os seus tropecos.

POR QUE ESTE ARQUIVO ESTA VERSIONADO (2026-10-23)
-------------------------------------------------
Os testes deste projeto sempre moraram no scratchpad, fora do git. Em 23/10 as
pastas temporarias foram limpas e as ~393 assercoes sumiram de uma vez -- entao
nao havia como provar que uma mudanca de uma linha nao tinha quebrado nada.

Este arquivo e o comeco do conserto: teste PURO (sem rede, sem banco, sem
navegador) do que e facil errar de novo. Rodar com:

    venv\\Scripts\\python.exe tests\\test_login_detect.py

Cada caso aqui foi MEDIDO contra o site de verdade, e a data esta no comentario.
Os dois casos `False` no fim sao os falsos positivos que a regra ja teve: eles
existem para que a proxima correcao nao os reabra.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ig_session as igs

ok = 0


def t(cond, msg):
    global ok
    assert cond, "FALHOU: " + msg
    ok += 1


# ── O caminho diz login ──────────────────────────────────────────────────
for url, nome in [
    ("https://www.tiktok.com/login/phone-or-email/email", "TikTok"),
    ("https://x.com/i/flow/login", "X"),
    ("https://www.instagram.com/accounts/login/?next=/", "Instagram"),
    ("https://accounts.google.com/signin/v2/identifier", "Google"),
    ("https://www.facebook.com/login.php?next=x", "Facebook"),
]:
    t(igs.parece_tela_de_login(url) is True, "tela de login do %s" % nome)


# ── O X NAO usa a palavra no caminho (MEDIDO 23/10) ──────────────────────
# `x.com/compose/post` deslogado redireciona para
# `x.com/i/jf/onboarding/web?redirect_after_login=%2Fcompose%2Fpost&mode=login`.
# O caminho e `/i/jf/onboarding/web`: nao ha "login" nele em lugar nenhum. Olhar
# so o caminho diria "parece logado" sobre uma sessao que o X acabou de recusar.
# O sinal e o NOME do parametro, que so existe quando o site esta te mandando
# para o login e guardando para onde voltar.
X_BOUNCE = ("https://x.com/i/jf/onboarding/web"
            "?redirect_after_login=%2Fcompose%2Fpost&mode=login")
t(igs.parece_tela_de_login(X_BOUNCE) is True,
  "o X mandando para o login e detectado pelo NOME do parametro")
t(igs.parece_tela_de_login(
    "https://exemplo.com/x?redirect_after_signup=/a") is True,
  "o mesmo vale para o parametro de cadastro")


# ── Os FALSOS POSITIVOS que a regra ja teve: devem seguir negativos ──────
# Achados pelo proprio teste em 13/10, quando a regra olhava a URL inteira.
t(igs.parece_tela_de_login("https://www.tiktok.com/foryou?next=/login") is False,
  "'/login' no VALOR de um parametro nao conta -- e so para onde voltar")
t(igs.parece_tela_de_login("https://login.tiktok.com/foryou") is False,
  "um dominio CHAMADO login nao e a tela de login")


# ── Telas logadas de verdade ─────────────────────────────────────────────
for url, nome in [
    ("https://x.com/compose/post", "compositor do X"),
    ("https://www.tiktok.com/tiktokstudio/upload", "TikTok Studio"),
    ("https://www.instagram.com/", "Instagram"),
    ("https://studio.youtube.com/", "YouTube Studio"),
]:
    t(igs.parece_tela_de_login(url) is False, "%s nao e tela de login" % nome)


# ── Entradas degeneradas nao explodem ────────────────────────────────────
for url in ("", None, "javascript:alert(1)", "nao-e-url"):
    t(igs.parece_tela_de_login(url) is False, "entrada %r nao explode" % (url,))


print("OK — %d assercoes" % ok)
