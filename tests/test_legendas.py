# -*- coding: utf-8 -*-
"""Importar legendas escritas FORA do app, uma secao por rede (2026-11-06).

O usuario quer publicacao com texto de IA. Em vez de chamar a API de um modelo
de dentro do app -- chave, custo por token e decisao nao-deterministica no
motor, contra a regra firme "SEM IA embutida" --, o modelo trabalha fora e
entrega um arquivo. O app so le.

O arquivo deste teste e o REAL, gerado pelo Grok e enviado pelo usuario. Os
casos de borda nao sao inventados: saem do que aquele arquivo ja trazia errado.

O que fica travado:

  - nada e descartado em SILENCIO. Secao de rede desconhecida, vazia ou
    repetida vira aviso. Arquivo escrito por modelo erra nome, e um
    "importei 4 de 5" sem dizer qual sumiu e pior que um erro;
  - o destino do cabecalho e CONFERIDO contra o que a rede tem. O arquivo real
    trazia "FACEBOOK (Reels)", e o Facebook nao tem reels;
  - o limite de caracteres e avisado na IMPORTACAO, que e quando ainda da para
    voltar ao modelo e pedir mais curto.

Puro: sem rede, sem banco. Rodar com:

    venv\\Scripts\\python.exe tests\\test_legendas.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import legendas  # noqa: E402
import redes  # noqa: E402

ok = 0


def t(cond, msg):
    global ok
    assert cond, "FALHOU: " + msg
    ok += 1


INFO = {r["id"]: {"legenda_max": r["legenda_max"], "destinos": list(r["destinos"])}
        for r in redes.REDES}
PERSONA = ["facebook", "instagram", "threads", "tiktok", "x", "youtube"]

# O arquivo real, reproduzido aqui para o teste nao depender de Downloads.
REAL = """=== INSTAGRAM (Reels) ===
TSE aceita ação do Novo contra Lula e Alckmin.

A AIJE questiona 35 medidas do governo no ano eleitoral.

#UncensoredBrasil #TSE

=== FACEBOOK (Reels) ===
TSE aceita ação do Novo contra Lula e Alckmin por abuso de poder político.

Fonte: CNN Brasil, Valor, DW

=== TIKTOK ===
TSE aceita ação do Novo contra Lula e Alckmin.

#UncensoredBrasil #TSE

=== X ===
TSE aceita ação do Novo contra Lula e Alckmin. AIJE questiona 35 medidas.

#UncensoredBrasil

=== THREADS ===
TSE aceita ação do Novo contra Lula e Alckmin. Decisão admite o processo.

#UncensoredBrasil
"""

r = legendas.importar(REAL, PERSONA, INFO)

# ── o arquivo real, inteiro ──────────────────────────────────────────────
t(r["secoes"] == 5, "leu as cinco secoes")
t(set(r["legendas"]) == {"instagram", "facebook", "tiktok", "x", "threads"},
  "e mapeou as cinco redes: %s" % sorted(r["legendas"]))
t(r["legendas"]["instagram"]["destino"] == "reels",
  "o parentese do cabecalho vira destino")
t("#UncensoredBrasil" in r["legendas"]["tiktok"]["legenda"],
  "as hashtags ficam na legenda")
t("\n" in r["legendas"]["instagram"]["legenda"],
  "as quebras de linha do texto sao preservadas")
t(not r["legendas"]["x"]["legenda"].startswith("\n"),
  "mas o corpo vem sem a linha em branco da frente")

# ── o erro que o modelo cometeu no arquivo REAL ──────────────────────────
t(r["legendas"]["facebook"]["destino"] == "",
  "«FACEBOOK (Reels)» nao vira destino reels -- o Facebook nao tem reels")
t(any("reels" in a and "facebook" in a for a in r["avisos"]),
  "e o aviso diz o que foi feito: %s" % r["avisos"])

# ── nomes que o modelo escreve de outro jeito ────────────────────────────
for titulo, esperado in [("=== X (Twitter) ===", "x"), ("=== Twitter ===", "x"),
                         ("=== tiktok ===", "tiktok"), ("=== Tik Tok ===", "tiktok"),
                         ("=== YouTube Shorts ===", "youtube"),
                         ("=== INSTAGRAM ===", "instagram"), ("=== IG ===", "instagram")]:
    d = legendas.importar(titulo + "\ntexto", None, INFO)
    t(esperado in d["legendas"], "«%s» cai em %s" % (titulo.strip('= '), esperado))

# cabecalho com folga de espacos e de iguais
t("x" in legendas.importar("=====   X   =====\ntexto")["legendas"],
  "cabecalho com mais iguais e mais espacos tambem e lido")

# ── nada some em silencio ────────────────────────────────────────────────
d = legendas.importar("=== ORKUT ===\ntexto do orkut", None, INFO)
t(not d["legendas"] and any("ORKUT" in a for a in d["avisos"]),
  "rede desconhecida vira aviso NOMEANDO a secao, nao sumico")

d = legendas.importar("=== X ===\n\n   \n=== THREADS ===\nok", None, INFO)
t("x" not in d["legendas"] and any("vazia" in a for a in d["avisos"]),
  "secao vazia e avisada")
t("threads" in d["legendas"], "e nao atrapalha as outras")

d = legendas.importar("=== X ===\nprimeiro\n=== X ===\nsegundo", None, INFO)
t(d["legendas"]["x"]["legenda"] == "segundo", "secao repetida: fica a ultima")
t(any("duas vezes" in a for a in d["avisos"]), "e avisa que repetiu")

d = legendas.importar("so um texto solto, sem cabecalho nenhum", None, INFO)
t(not d["legendas"] and any("cabeçalho" in a for a in d["avisos"]),
  "arquivo sem cabecalho explica o que faltou")

t(legendas.importar("")["legendas"] == {}, "arquivo vazio nao explode")
t(legendas.importar(None)["legendas"] == {}, "nem None")

# preambulo do modelo ("aqui estao as legendas:") e ignorado
d = legendas.importar("Claro! Aqui estao:\n\n=== X ===\ntexto", None, INFO)
t(d["legendas"]["x"]["legenda"] == "texto",
  "o que vem ANTES do primeiro cabecalho e descartado")

# ── o limite avisa na importacao, nao so na publicacao ───────────────────
d = legendas.importar("=== X ===\n" + ("a" * 400), None, INFO)
t(any("280" in a for a in d["avisos"]),
  "legenda de 400 no X de 280 e avisada agora: %s" % d["avisos"])
t(d["legendas"]["x"]["legenda"], "mas a legenda ENTRA -- a adaptacao encurta depois")

# ── rede que a persona nao tem: avisa, nao filtra ────────────────────────
d = legendas.importar("=== KWAI ===\ntexto", PERSONA, INFO)
t("kwai" in d["legendas"], "a legenda do Kwai e lida mesmo sem conta")
t(any("não tem conta" in a for a in d["avisos"]), "e o aviso diz que falta conta")

# ── o briefing fecha o ciclo ─────────────────────────────────────────────
b = legendas.modelo_de_briefing([
    {"id": "x", "nome": "X (Twitter)", "legenda_max": 280, "destinos": ["feed"]},
    {"id": "tiktok", "nome": "TikTok", "legenda_max": 4000, "destinos": ["feed"]}])
t("=== NOME DA REDE ===" in b, "o briefing mostra o formato esperado")
t("280" in b and "4000" in b,
  "e traz os limites -- a informacao que o modelo nao tem e que faz a legenda voltar grande")

# o que o briefing pede, o importador le: o ciclo fecha de verdade
exemplo = "=== X (Twitter) ===\ncurto\n\n=== TikTok ===\noutro"
t(set(legendas.importar(exemplo, None, INFO)["legendas"]) == {"x", "tiktok"},
  "o formato do briefing e exatamente o que o importador aceita")

print("OK — %d assercoes" % ok)
