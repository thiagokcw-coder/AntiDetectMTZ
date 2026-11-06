# -*- coding: utf-8 -*-
"""Ler um arquivo de legendas escrito FORA do app, uma seção por rede.

POR QUE ISTO EXISTE
-------------------
O usuário pediu publicação com texto gerado por IA. O caminho óbvio seria o app
chamar a API de um modelo — e esse caminho tem chave, custo por token e uma
decisão não-determinística dentro do motor, contra a regra firme do projeto
("SEM IA embutida: regras e estatística, determinístico, grátis, explicável").

Este caminho não tem nada disso: **o modelo trabalha fora e entrega um arquivo
de texto.** O app só lê. Se o texto estiver ruim, você vê antes de publicar; se
o modelo sair do ar, o app continua igual. A IA fica onde ela é boa — escrever —
e fora de onde ela é cara e arriscada, que é decidir e publicar.

O FORMATO
---------
O que o modelo devolve, e que esta função lê:

    === INSTAGRAM (Reels) ===
    Texto da legenda, com as linhas em branco que ele quiser.

    #hashtags

    === X ===
    Outro texto.

O cabeçalho é `=== NOME ===`, com um parêntese opcional que o modelo usa para
dizer o destino ("Reels", "Story"). O corpo vai até o próximo cabeçalho.

NADA É DESCARTADO EM SILÊNCIO. Seção de rede que o app não conhece, ou que a
persona não tem, vira AVISO — não some. Arquivo escrito por um modelo erra
nome, e um "importei 4 de 5" sem dizer qual sumiu é pior que um erro.
"""
import re
import unicodedata

#: `=== NOME ===` ou `=== NOME (algo) ===`, com folga de espaços e de `=`.
_CABECALHO = re.compile(r"^\s*={2,}\s*(.+?)\s*={2,}\s*$")

#: Como o modelo escreve o nome → o id da rede no registro. Serve para o que
#: `nomeDaRede` não resolve sozinho: "X (Twitter)" vira "x", e o inglês entra
#: porque modelo escreve em inglês sem avisar.
_APELIDOS = {
    "x": "x", "twitter": "x", "x twitter": "x",
    "instagram": "instagram", "ig": "instagram", "insta": "instagram",
    "facebook": "facebook", "fb": "facebook", "face": "facebook",
    "threads": "threads",
    "tiktok": "tiktok", "tik tok": "tiktok",
    "youtube": "youtube", "yt": "youtube", "youtube shorts": "youtube",
    "kwai": "kwai",
    "telegram": "telegram",
}

#: O parêntese do cabeçalho costuma trazer o destino.
_DESTINOS = {"reels": "reels", "reel": "reels", "story": "story",
             "stories": "story", "feed": "feed", "post": "feed",
             "shorts": "reels"}


def _chave(texto: str) -> str:
    """Minúsculo, sem acento e sem pontuação — para 'X (Twitter)', 'x' e
    'TWITTER' caírem todos no mesmo lugar."""
    s = unicodedata.normalize("NFKD", texto or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s).strip().lower()
    return s


def _partir_cabecalho(titulo: str) -> tuple:
    """'INSTAGRAM (Reels)' → ('instagram', 'reels'). Devolve (None, _) se não
    reconhecer a rede."""
    m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", titulo)
    nome, entre = (m.group(1), m.group(2)) if m else (titulo, "")
    rede = _APELIDOS.get(_chave(nome))
    if not rede:
        # "X (Twitter)" cai aqui quando o parêntese É o nome: tenta os dois
        # juntos antes de desistir.
        rede = _APELIDOS.get(_chave(titulo))
        if rede:
            entre = ""
    return rede, _DESTINOS.get(_chave(entre), "")


def importar(texto: str, redes_da_persona=None, info=None) -> dict:
    """Lê o arquivo e devolve o que vai para a personalização de cada rede.

    `redes_da_persona` (opcional) é a lista de redes que a persona tem: serve
    só para AVISAR que o arquivo trouxe uma rede sem conta, nunca para filtrar
    em silêncio.

    `info` é `{rede: {"legenda_max": n, "destinos": [...]}}`, vindo do registro.
    Serve para dois avisos que só fazem sentido AGORA, enquanto dá para voltar
    ao modelo e pedir de novo:

      - legenda maior que o limite da rede;
      - **destino que a rede não tem.** O modelo erra isso: o arquivo que
        motivou esta função trazia "FACEBOOK (Reels)", e o Facebook tem story
        e feed, não reels. Aceitar calado poria um destino inválido no plano.

    Devolve `{"legendas": {rede: {"legenda", "destino"}}, "avisos": [...],
    "secoes": n}`.
    """
    linhas = (texto or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocos, atual = [], None
    for linha in linhas:
        m = _CABECALHO.match(linha)
        if m:
            atual = {"titulo": m.group(1), "corpo": []}
            blocos.append(atual)
        elif atual is not None:
            atual["corpo"].append(linha)
        # texto antes do primeiro cabeçalho é preâmbulo do modelo ("aqui estão
        # as legendas:") e é ignorado de propósito

    legendas, avisos = {}, []
    tem = {r for r in (redes_da_persona or [])}
    dados = dict(info or {})

    for b in blocos:
        rede, destino = _partir_cabecalho(b["titulo"])
        corpo = "\n".join(b["corpo"]).strip()
        if not rede:
            avisos.append("não reconheci a rede «%s» — essa seção ficou de fora"
                          % b["titulo"][:40])
            continue
        if not corpo:
            avisos.append("a seção do %s veio vazia" % rede)
            continue
        if rede in legendas:
            avisos.append("o %s aparece duas vezes; fiquei com a última" % rede)

        meta = dados.get(rede) or {}
        destinos = list(meta.get("destinos") or [])
        if destino and destinos and destino not in destinos:
            avisos.append("o arquivo pede «%s» no %s, que só tem %s — deixei o "
                          "destino em branco e a adaptação escolhe"
                          % (destino, rede, " e ".join(destinos)))
            destino = ""

        legendas[rede] = {"legenda": corpo, "destino": destino}

        if tem and rede not in tem:
            avisos.append("o arquivo traz %s, mas esta persona não tem conta lá"
                          % rede)
        maximo = int(meta.get("legenda_max") or 0)
        if maximo and len(corpo) > maximo:
            avisos.append("a legenda do %s tem %d caracteres e o limite é %d — "
                          "ela será encurtada na adaptação"
                          % (rede, len(corpo), maximo))

    if not blocos:
        avisos.append("não achei nenhum cabeçalho «=== REDE ===» no arquivo")

    return {"legendas": legendas, "avisos": avisos, "secoes": len(blocos)}


def modelo_de_briefing(redes_info: list) -> str:
    """O texto para colar no modelo, para ele devolver no formato certo.

    Fecha o ciclo: sem isto, acertar o formato é tentativa e erro. Traz o
    limite de cada rede, que é a informação que o modelo não tem como saber e
    é justamente a que faz a legenda voltar grande demais.
    """
    linhas = [
        "Escreva a legenda desta publicação para cada rede abaixo.",
        "",
        "Responda SÓ no formato, sem comentários antes ou depois:",
        "",
        "=== NOME DA REDE ===",
        "texto da legenda",
        "",
        "Respeite o limite de caracteres de cada uma:",
        "",
    ]
    for r in redes_info:
        destinos = " / ".join(r.get("destinos") or [])
        linhas.append("- %s: até %s caracteres (destinos: %s)"
                      % (r.get("nome") or r.get("id"),
                         r.get("legenda_max") or "?", destinos))
    linhas += ["", "Assunto: "]
    return "\n".join(linhas)
