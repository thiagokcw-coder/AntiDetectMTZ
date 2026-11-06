"""Publicação multi-rede: um perfil, uma conta em cada rede, um clique.

O perfil deixa de ser "um navegador com uma conta de Instagram" e passa a ser
uma PESSOA: a mesma persona com conta no Instagram, no Facebook, no X, no
TikTok, no YouTube e nas demais. Publicar é um ato só — o conteúdo sai para
todas as redes daquele perfil na mesma leva.

## O que "ao mesmo tempo" significa aqui, e por que não é no mesmo milissegundo

As publicações são disparadas na MESMA leva, sem intervenção entre elas: um
clique, todas as redes. Mas elas não saem no mesmo instante de propósito.

Seis requisições partindo do mesmo IP no mesmo milissegundo é assinatura de
máquina — é o mesmo erro que `comportamento.py` existe para não cometer (lá a
lição custou uma conta suspensa: *"tudo que for padronizado será visto como
bot"*). Uma pessoa com seis redes no celular leva alguns segundos entre uma e
outra. Então o orquestrador espalha as saídas por poucos segundos
(`espalhar_segundos`, padrão 12) e RELATA o instante real de cada uma.

Pôr `espalhar_segundos=0` devolve o disparo simultâneo exato, para quem quiser.

## Uma rotação de IP para a leva inteira — nunca uma por rede

A rotação do modem derruba a internet do PC inteiro por ~20s (ver a seção do
modem no CLAUDE.md). Rotacionar entre redes seria errado duas vezes: custaria
dois minutos de queda numa publicação, e separaria em IPs diferentes contas que
são a MESMA PESSOA — o oposto do que a rotação existe para fazer. Ela separa
personas, não as redes de uma persona.

Então: no máximo uma rotação ANTES da leva, e todas as redes publicam dentro da
mesma época de IP. Isso é o que acontece de verdade com alguém postando do
sofá: um aparelho, um IP, seis redes.

## O formato manda, e a recusa é explícita

YouTube e TikTok são redes de vídeo: imagem parada não entra. Em vez de tentar
e falhar no meio da leva, `montar_plano` recusa ANTES, com motivo escrito, e as
outras redes seguem. O relatório mostra a recusa — publicação que não aconteceu
nunca aparece como sucesso.

## O que está AUTOMÁTICO e o que está ASSISTIDO (estado honesto)

Só o Instagram publica sozinho hoje, e publica porque o caminho foi medido:
cookies do perfil → `login_by_sessionid` → `instagrapi` (ver `story.py`). As
outras redes entram no modo ASSISTIDO: a leva abre o compositor de cada uma na
MESMA janela Camoufox do perfil, com o arquivo e a legenda a um colar de
distância, e o relatório diz quais ficaram esperando confirmação.

Isto é deliberado, não preguiça. Escrever dez fluxos de DOM sem nunca ter
aberto a tela de cada rede produziria dez seletores inventados — e este projeto
já pagou por afirmação sem medição (ver a correção de 28/08 no CLAUDE.md). Cada
rede sai do assistido para o automático quando o fluxo dela for medido uma vez,
trocando `transporte` no registro abaixo. O campo `medido` diz, por rede, se
alguém já conferiu.

As URLs de compositor abaixo também são ponto de partida, não fato conferido:
rede troca de domínio e de rota (o Threads mudou de `threads.net` para
`threads.com`). Abrir a primeira vez é o que confirma — e se a rede abrir em
outro lugar, é uma linha para corrigir aqui, num lugar só.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# O REGISTRO DAS REDES
# ---------------------------------------------------------------------------
# Um lugar só, porque é a tabela que vai ser corrigida com o uso: rede troca de
# URL, de limite de legenda e de formato aceito. Espalhar isso pelo código faria
# cada correção virar uma caçada.
#
# `transporte`:
#   instagrapi → publica sozinho pela API privada, com a sessão do perfil
#   navegador  → abre o compositor no Camoufox do perfil (assistido)
#   api        → HTTP com credencial própria (nenhuma rede está aqui ainda)
#
# `cookie_dominios` são os domínios cujos cookies carregam a sessão daquela
# rede, e `cookie_chaves` os nomes que a gente ESPERA ver num material bom. As
# chaves só servem para AVISAR — nunca para recusar: lista de cookie muda, e
# recusar por uma lista minha deixaria de funcionar um material que funcionava.
# Lista de chaves vazia = não sabemos (ou a sessão nem mora em cookie: o
# Telegram Web guarda a dele em localStorage, então importar cookie não loga).
#
# `site` é a casa da rede — é o que o botão "Abrir" da aba Contas carrega.
# `compositor` é a tela de publicar, usada pela leva no modo assistido. São
# endereços diferentes de propósito: abrir uma conta para conferir não é o mesmo
# que abrir a tela de upload dela.
#
# `medido`: alguém já exercitou este caminho ao vivo? Começa False em tudo que
# não é o Instagram, e é o campo que impede o painel de prometer o que não foi
# conferido.

REDES: tuple[dict, ...] = (
    {
        "id": "instagram",
        "nome": "Instagram",
        "transporte": "instagrapi",
        "destinos": ("story", "feed", "reels"),
        "formato_padrao": {"story": "story", "feed": "vertical",
                           "reels": "story"},
        # Que TIPO cada destino aceita, nesta rede. Só o Instagram tem a chave
        # porque só ele publica sozinho: o que limita aqui é o transporte, não
        # a rede. `photo_upload*` é foto e `clip_upload` é vídeo, então vídeo no
        # Instagram só sai como Reels -- recusar com esse motivo escrito é
        # melhor que chegar no instagrapi e levar um
        # "Invalid file format. Only JPG/JPEG/PNG/WEBP", que foi o que
        # acontecia antes de Reels existir aqui.
        "combina": {"story": ("imagem",), "feed": ("imagem",),
                    "reels": ("video",)},
        "cookie_dominios": ['instagram.com'],
        "cookie_chaves": ['sessionid'],
        "site": "https://www.instagram.com/",
        "compositor": "https://www.instagram.com/",
        "formatos": ("imagem", "video"),
        "legenda_max": 2200,
        "medido": True,
        "nota": "story com link e repost já funcionam (ver story.py)",
    },
    {
        "id": "facebook",
        "nome": "Facebook",
        "transporte": "navegador",
        "destinos": ("story", "feed"),
        "formato_padrao": {"story": "story", "feed": "quadrado"},
        "cookie_dominios": ['facebook.com'],
        "cookie_chaves": ['c_user', 'xs'],
        "site": "https://www.facebook.com/",
        # MEDIDO em 23/10: clicar em "Em que estás a pensar" leva para
        # /post/create, ou seja, o compositor TEM url propria. Antes isto
        # apontava para a home, que era o chute de quem nunca tinha olhado.
        "compositor": "https://www.facebook.com/post/create",
        "formatos": ("imagem", "video", "texto"),
        "legenda_max": 63206,
        "medido": False,
        "nota": "a Graph API só posta em PÁGINA, com app aprovado — conta "
                "pessoal publica pelo navegador",
    },
    {
        "id": "threads",
        "nome": "Threads",
        "transporte": "navegador",
        "destinos": ("feed",),
        "formato_padrao": {"feed": "quadrado"},
        "cookie_dominios": ['threads.com', 'threads.net', 'instagram.com'],
        "cookie_chaves": ['sessionid'],
        "site": "https://www.threads.com/",
        "compositor": "https://www.threads.com/",
        "formatos": ("imagem", "video", "texto"),
        "legenda_max": 500,
        "medido": False,
        "nota": "mudou de threads.net para threads.com — confirmar ao abrir",
    },
    {
        "id": "x",
        "nome": "X (Twitter)",
        "transporte": "navegador",
        "destinos": ("feed",),
        "formato_padrao": {"feed": "paisagem"},
        "cookie_dominios": ['x.com', 'twitter.com'],
        "cookie_chaves": ['auth_token', 'ct0'],
        "site": "https://x.com/",
        "compositor": "https://x.com/compose/post",
        "formatos": ("imagem", "video", "texto"),
        "legenda_max": 280,
        "medido": False,
        "nota": "a API v2 de escrita é paga; o navegador não cobra",
    },
    {
        "id": "tiktok",
        "nome": "TikTok",
        "transporte": "navegador",
        "destinos": ("feed",),
        "formato_padrao": {"feed": "story"},
        "cookie_dominios": ['tiktok.com'],
        "cookie_chaves": ['sessionid', 'sid_tt'],
        "site": "https://www.tiktok.com/",
        "compositor": "https://www.tiktok.com/tiktokstudio/upload",
        "formatos": ("video",),
        # 4000, MEDIDO na tela do TikTok Studio em 14/10 (o contador do campo
        # dizia "92/4000"). Estava 2200 aqui, que era o limite antigo -- numero
        # que eu tinha posto de cabeca, e que teria recusado legenda boa.
        "legenda_max": 4000,
        "medido": False,
        "nota": "a Content Posting API exige app aprovado e domínio "
                "verificado — fora do alcance destas contas. ⚠ O Studio tem "
                "aba 'Fotos' além de 'Vídeos' (visto em 14/10): imagem talvez "
                "entre, mas o fluxo de foto NÃO foi medido",
    },
    {
        "id": "youtube",
        "nome": "YouTube",
        "transporte": "navegador",
        "destinos": ("feed",),
        "formato_padrao": {"feed": "paisagem"},
        "cookie_dominios": ['youtube.com', 'google.com'],
        "cookie_chaves": ['SID', 'SAPISID', '__Secure-1PSID'],
        "site": "https://www.youtube.com/",
        "compositor": "https://studio.youtube.com/",
        "formatos": ("video",),
        "legenda_max": 5000,
        "medido": False,
        "nota": "só vídeo. A Data API v3 é gratuita (100 envios/dia) e o "
                "código dela está pronto em `youtube_api.py`, mas ficou "
                "DORMENTE em 05/11 — decisão do usuário: o preparo no Google "
                "Cloud é muito para o que esta rede devolve. Caminho atual: "
                "sessão por cookie (de google.com E youtube.com) + compositor "
                "do Studio, que é um fluxo de várias telas",
    },
    {
        "id": "kwai",
        "nome": "Kwai",
        "transporte": "navegador",
        "destinos": ("feed",),
        "formato_padrao": {"feed": "story"},
        "cookie_dominios": ['kwai.com'],
        "cookie_chaves": [],
        "site": "https://www.kwai.com/",
        "compositor": "https://www.kwai.com/",
        "formatos": ("video",),
        "legenda_max": 500,
        "medido": False,
        "nota": "relevante no Brasil; sem API pública e upload web a confirmar",
    },
    {
        "id": "telegram",
        "nome": "Telegram",
        "transporte": "navegador",
        "destinos": ("feed",),
        "formato_padrao": {"feed": "quadrado"},
        "cookie_dominios": ['web.telegram.org', 'telegram.org'],
        "cookie_chaves": [],
        "site": "https://web.telegram.org/",
        "compositor": "https://web.telegram.org/",
        "formatos": ("imagem", "video", "texto"),
        "legenda_max": 1024,
        "medido": False,
        "nota": "canal próprio publica por Bot API sem custo nem aprovação",
    },
)

_POR_ID = {r["id"]: r for r in REDES}

#: Mantido para o código que ainda pensa em uma rede só. A conta de Instagram
#: continua sendo a padrão em toda leitura que não disser a rede — é o que
#: segura os 10 chamadores de `get_account_by_profile`.
REDE_PADRAO = "instagram"

#: Quantos segundos a leva espalha as saídas. Ver a nota no topo do arquivo.
ESPALHAR_SEGUNDOS_PADRAO = 12


def rede(rede_id: str) -> Optional[dict]:
    """O registro de uma rede, ou None se o id não existe."""
    return _POR_ID.get((rede_id or "").strip().lower())


def ids() -> tuple[str, ...]:
    return tuple(r["id"] for r in REDES)


def nome(rede_id: str) -> str:
    """Rótulo para a tela. Id desconhecido volta como veio — rede que alguém
    cadastrou à mão não deve virar string vazia no painel."""
    r = rede(rede_id)
    return r["nome"] if r else (rede_id or "")


# ---------------------------------------------------------------------------
# O PLANO DA LEVA — função pura, e é ela que decide
# ---------------------------------------------------------------------------
# Pura de propósito, igual ao que o Mobiliza faz com a saúde das instâncias: a
# decisão de quem publica e quem é recusado precisa de teste, e não precisa de
# rede nem de banco para ser conferida.


@dataclass
class Alvo:
    """Uma rede da leva, já decidida.

    Chamava-se `Destino`. Virou `Alvo` quando o Story Creator passou a fazer
    post de feed: aí "destino" passou a significar story-ou-feed, e dois
    sentidos para a mesma palavra no mesmo arquivo é como se lê errado.
    """

    rede_id: str
    conta_id: Optional[str] = None
    username: str = ""
    transporte: str = ""
    publica: bool = False
    motivo: str = ""
    #: story | feed | reels — o que esta rede vai receber nesta leva
    destino: str = "feed"
    #: nome do formato de arte (ver story.FORMATOS)
    formato: str = ""
    #: já com a personalização aplicada por cima do conteúdo comum
    legenda: str = ""
    link: str = ""
    arquivo: str = ""
    #: a arte composta NO FORMATO desta rede (preenchida na hora de publicar)
    arquivo_composto: str = ""
    #: imagem de capa (só Reels). `capa` é o que veio da tela; `capa_composta`
    #: é ela depois do compositor -- com o chip, no formato da rede.
    capa: str = ""
    capa_composta: str = ""
    #: onde o chip acabou desenhado nessa arte -- vira a area tocavel
    link_rect: dict = field(default_factory=dict)
    #: publicacao de outra pessoa a repostar (so story)
    post: dict = field(default_factory=dict)
    personalizado: tuple = ()
    #: o que o app ajustou SOZINHO para caber nesta rede (ver adaptar_para_rede)
    adaptado: tuple = ()
    #: preenchido depois, pelo orquestrador
    desfecho: str = ""
    detalhe: str = ""
    quando: float = 0.0
    resultado: dict = field(default_factory=dict)

    def como_dict(self) -> dict:
        return {
            "rede": self.rede_id,
            "rede_nome": nome(self.rede_id),
            "conta_id": self.conta_id,
            "username": self.username,
            "transporte": self.transporte,
            "publica": self.publica,
            "motivo": self.motivo,
            "destino": self.destino,
            "formato": self.formato,
            "legenda": self.legenda,
            "link": self.link,
            "arquivo": self.arquivo,
            "arquivo_composto": self.arquivo_composto,
            "capa": self.capa,
            "capa_composta": self.capa_composta,
            "personalizado": list(self.personalizado),
            "adaptado": list(self.adaptado),
            "desfecho": self.desfecho,
            "detalhe": self.detalhe,
            "quando": self.quando,
            "resultado": self.resultado,
        }


def tipo_do_conteudo(conteudo: dict) -> str:
    """'video', 'imagem' ou 'texto', a partir do que a leva recebeu.

    O tipo sai do conteúdo e não de um campo que o chamador preenche, porque
    campo preenchido à mão mente: quem manda um vídeo e esquece de trocar o
    seletor levaria o vídeo recusado no Instagram e aceito no TikTok, pelo
    mesmo arquivo.
    """
    arq = (conteudo.get("arquivo") or "").lower()
    if arq.endswith((".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv")):
        return "video"
    if arq:
        return "imagem"
    return "texto"


def conteudo_da_rede(conteudo: dict, personalizacao: dict, r: dict,
                     destino: str) -> tuple:
    """O conteúdo COMUM com a personalização daquela rede por cima.

    Função à parte, e pura, porque "o que cada rede recebe" é a pergunta que a
    tela faz o tempo todo (para a prévia) e o orquestrador faz uma vez (para
    publicar). Se cada lado calculasse por conta, a prévia mostraria uma coisa e
    sairia outra — e seria a prévia que o usuário leria.

    Devolve `(conteudo_final, campos_personalizados)`. O segundo é só para a
    tela poder marcar o que foi trocado: rede que herdou tudo e rede que
    reescreveu a legenda não devem parecer iguais.
    """
    p = personalizacao or {}
    final = dict(conteudo)
    trocados = []

    for campo in ("legenda", "link", "arquivo"):
        v = p.get(campo)
        # String vazia NÃO é herança: é "quero esta rede sem legenda". Só a
        # ausência da chave herda — senão não haveria como apagar a legenda de
        # uma rede só, que é metade do motivo de existir personalização.
        if campo in p and v != final.get(campo):
            final[campo] = v or ""
            trocados.append(campo)

    formato = p.get("formato") or (r.get("formato_padrao") or {}).get(destino) or "quadrado"
    if p.get("formato"):
        trocados.append("formato")
    final["formato"] = formato
    final["destino"] = destino
    return final, tuple(trocados)


def encurtar_legenda(texto: str, limite: int, link: str = "") -> tuple:
    """Corta a legenda para caber no limite da rede, SEM perder o link.

    Três decisões que não são óbvias:

    1. **Corta na palavra, não no caractere.** Cortar no meio de uma palavra
       denuncia máquina, e é o tipo de detalhe que ninguém revisa depois.
    2. **O link sobrevive.** Se ele está na legenda, é ele que leva a pessoa ao
       destino — cortar o texto e levar o link junto deixaria a publicação sem
       a única coisa que ela existe para fazer. Então o link é reservado e
       recolocado no fim.
    3. **Se nem o link cabe, não adapta.** Devolve `None` para quem chamou
       recusar com motivo, em vez de publicar um link pela metade, que não
       clica e não se percebe.

    Devolve `(texto_novo, descricao_do_que_foi_feito)` ou `(None, motivo)`.
    """
    texto = texto or ""
    if len(texto) <= limite:
        return texto, ""

    tem_link = bool(link) and link in texto
    reticencia = "…"
    if tem_link:
        # espaço para "… " + o link
        reserva = len(link) + len(reticencia) + 2
        if reserva > limite:
            return None, ("o link sozinho tem %d caracteres e o limite é %d"
                          % (len(link), limite))
        corpo = texto.replace(link, " ").strip()
        espaco = limite - reserva
    else:
        corpo = texto
        espaco = limite - len(reticencia)

    cortado = corpo[:espaco]
    # Volta até o último espaço, para não partir palavra. Se a "palavra" ocupa
    # o trecho inteiro (URL colada, hashtag enorme), corta no seco mesmo —
    # melhor um corte feio que uma legenda que não entra.
    ultimo = cortado.rfind(" ")
    if ultimo > espaco * 0.6:
        cortado = cortado[:ultimo]
    cortado = cortado.rstrip(" ,;.-")

    novo = (cortado + reticencia + " " + link) if tem_link else (cortado + reticencia)
    return novo, ("legenda encurtada de %d para %d caracteres%s"
                  % (len(texto), len(novo), " (link preservado)" if tem_link else ""))


def _destino_que_aceita(r: dict, destino: str, tipo: str) -> str:
    """O destino desta rede que aceita este TIPO de mídia, se o pedido não aceitar.

    É o que transforma "o Instagram publica vídeo como Reels — troque o
    destino" de instrução em ação: a adaptação faz a troca e a prévia mostra.
    """
    combina = r.get("combina") or {}
    if not combina or tipo in (combina.get(destino) or (tipo,)):
        return destino
    for alvo in r.get("destinos") or ():
        if tipo in (combina.get(alvo) or ()):
            return alvo
    return destino


def adaptar_para_rede(final: dict, r: dict, destino: str, tipo: str) -> tuple:
    """Ajusta o conteúdo ao que ESTA rede aceita. Função PURA.

    Existe porque recusar não é adaptar: antes, legenda de 400 caracteres fazia
    o X sair da leva com "personalize a legenda desta rede" — o usuário tinha
    de resolver à mão o que o app sabia resolver sozinho.

    O que ela NÃO faz, de propósito: inventar mídia. Imagem não vira vídeo, e
    rede que só aceita vídeo continua sendo recusada. Adaptar é escolher entre
    o que existe; o resto seria fingir.

    Devolve `(conteudo_adaptado, lista_do_que_mudou, erro_ou_vazio)`.
    """
    saida = dict(final)
    mudancas = []

    # 1) destino: o que a rede tem, e que aceita este tipo de mídia
    alvo = destino
    if alvo not in (r.get("destinos") or ()):
        alvo = (r.get("destinos") or ("feed",))[0]
        mudancas.append("destino %s → %s (o %s não tem %s)"
                        % (destino, alvo, r["nome"], destino))
    novo_alvo = _destino_que_aceita(r, alvo, tipo)
    if novo_alvo != alvo:
        mudancas.append("destino %s → %s (é o que aceita %s)" % (alvo, novo_alvo, tipo))
        alvo = novo_alvo
    saida["destino"] = alvo
    if alvo != destino:
        # O formato acompanha o destino novo, senão a arte sairia no tamanho
        # do destino antigo — 9:16 num post que virou feed, por exemplo.
        saida["formato"] = (r.get("formato_padrao") or {}).get(alvo) or saida.get("formato")

    # 2) legenda: encurtada para caber, com o link protegido
    legenda = saida.get("legenda") or ""
    limite = int(r.get("legenda_max") or 0)
    if legenda and limite and len(legenda) > limite:
        novo, nota = encurtar_legenda(legenda, limite, saida.get("link") or "")
        if novo is None:
            return saida, mudancas, nota
        saida["legenda"] = novo
        mudancas.append(nota)

    return saida, mudancas, ""


def montar_plano(contas: list[dict], conteudo: dict,
                 redes_pedidas: Optional[list[str]] = None,
                 personalizacoes: Optional[dict] = None,
                 adaptar: bool = True) -> list[Alvo]:
    """Quem publica, com o quê, e quem é recusado por quê — antes de tocar em
    rede nenhuma.

    `contas` são as contas do perfil (uma por rede, como `list_accounts_by_profile`
    devolve). `redes_pedidas` vazio significa TODAS as que o perfil tem.
    `personalizacoes` é `{rede: {legenda, link, arquivo, formato, destino}}` e
    vence o conteúdo comum, campo a campo.

    `adaptar` (padrão **ligado**) deixa o app ajustar sozinho o que dá para
    ajustar — encurtar a legenda que não cabe, trocar o destino que a rede não
    tem, mandar vídeo do Instagram para Reels. Antes isso tudo era RECUSA com
    um "personalize esta rede", ou seja, o app sabia resolver e mandava o
    usuário resolver. O que foi ajustado sai em `adaptado`, para a prévia
    mostrar — adaptação que não aparece é adaptação que engana.

    Desligar devolve o comportamento antigo: a rede sai da leva com o motivo.
    Serve para quem quer ver o conflito em vez de deixar o app escolher.

    A ordem das recusas é do mais barato para o mais caro de descobrir: rede
    desconhecida, conta que não está ativa, destino que a rede não tem, formato
    incompatível, legenda longa. **A legenda conferida é a da rede**, não a
    comum — senão personalizar para caber no X de 280 não adiantaria nada.
    """
    pedidas = {r.strip().lower() for r in (redes_pedidas or []) if r and r.strip()}
    pers = {k.strip().lower(): v for k, v in (personalizacoes or {}).items() if k}
    # Padrão FEED, não story: nove das onze redes não têm story nenhum, e
    # "publicar" sem dizer mais nada significa uma publicação normal. Story é o
    # caso especial, e quem o quer diz.
    destino_comum = (conteudo.get("destino") or "feed").strip().lower()

    plano: list[Alvo] = []
    for c in contas:
        rid = (c.get("platform") or REDE_PADRAO).strip().lower()
        if pedidas and rid not in pedidas:
            continue

        d = Alvo(rede_id=rid, conta_id=c.get("id"),
                 username=c.get("username") or "")
        r = rede(rid)
        if not r:
            d.motivo = "rede não reconhecida pelo app — publique na mão"
            plano.append(d)
            continue

        d.transporte = r["transporte"]
        meu = pers.get(rid) or {}
        destino = (meu.get("destino") or destino_comum).strip().lower()
        d.destino = destino

        final, trocados = conteudo_da_rede(conteudo, meu, r, destino)

        # ADAPTAR antes de recusar. Roda depois da personalização de propósito:
        # o que o usuário escreveu para aquela rede é o ponto de partida, e a
        # adaptação só mexe no que ainda não cabe.
        if adaptar:
            tipo_do_conteudo_ = tipo_do_conteudo(final)
            final, mudou, erro_adapt = adaptar_para_rede(
                final, r, destino, tipo_do_conteudo_)
            d.adaptado = tuple(mudou)
            destino = final.get("destino") or destino
            d.destino = destino
            if erro_adapt:
                d.motivo = erro_adapt
                plano.append(d)
                continue

        d.legenda = final.get("legenda") or ""
        d.link = final.get("link") or ""
        d.arquivo = final.get("arquivo") or ""
        d.formato = final.get("formato") or ""
        # A capa do Reels acompanha o conteúdo COMUM, como o repost: capas
        # diferentes por rede não são uma leva, são várias.
        d.capa = conteudo.get("capa") or ""
        # O repost acompanha o conteúdo comum: é a publicação de outra pessoa
        # que vai virar card. Não é personalizável por rede de propósito —
        # repostar coisas diferentes em cada rede não é uma leva, são várias.
        d.post = conteudo.get("post") or {}
        d.personalizado = trocados
        tipo = tipo_do_conteudo(final)

        status = (c.get("status") or "").strip().lower()
        if status != "active":
            # "Em uso" é conta emprestada: publicar nela pelo nosso IP é
            # exatamente o que o rótulo existe para evitar.
            rotulo = {"in_use": "está emprestada (Em uso)",
                      "banned": "está banida",
                      "discarded": "foi descartada",
                      "checkpoint": "está em suspensão",
                      "suspended": "está em suspensão",
                      "login_required": "está com a sessão expirada"}.get(
                          status, "não está ativa (%s)" % (status or "sem status"))
            d.motivo = "a conta %s" % rotulo
            plano.append(d)
            continue

        if destino not in r["destinos"]:
            # Story no X ou no YouTube não existe. Recusar aqui é melhor que
            # publicar no feed sem avisar: a arte 9:16 iria para um lugar que
            # não é 9:16.
            d.motivo = "%s não tem %s — ele só recebe %s" % (
                r["nome"], destino, " e ".join(r["destinos"]))
            plano.append(d)
            continue

        if tipo not in r["formatos"]:
            d.motivo = "%s aceita %s, e o conteúdo é %s" % (
                r["nome"], " ou ".join(r["formatos"]), tipo)
            plano.append(d)
            continue

        # O que o TRANSPORTE daquela rede aceita em cada destino (hoje só o
        # Instagram declara, por ser o único que publica sozinho).
        combina = (r.get("combina") or {}).get(destino)
        if combina and tipo not in combina:
            if destino != "reels" and tipo == "video":
                d.motivo = ("o %s publica vídeo como Reels — troque o destino "
                            "desta rede para Reels" % r["nome"])
            else:
                d.motivo = "%s em %s aceita %s, e o conteúdo é %s" % (
                    r["nome"], destino, " ou ".join(combina), tipo)
            plano.append(d)
            continue

        if destino == "reels" and not (d.capa or "").strip():
            # A capa é o que aparece na grade do perfil, e é onde o chip do
            # destino fica. Sem ela o instagrapi tentaria tirar um quadro do
            # vídeo com MoviePy, que este projeto não instalou — então a recusa
            # aqui troca um erro de dependência por uma instrução.
            d.motivo = ("Reels precisa de uma imagem de capa — envie a capa no "
                        "editor (é nela que o chip do link é desenhado)")
            plano.append(d)
            continue

        if d.legenda and len(d.legenda) > r["legenda_max"]:
            d.motivo = "a legenda tem %d caracteres e o limite do %s é %d%s" % (
                len(d.legenda), r["nome"], r["legenda_max"],
                "" if "legenda" in trocados else " — personalize a legenda desta rede")
            plano.append(d)
            continue

        d.publica = True
        base = ("publica sozinho" if r["transporte"] == "instagrapi"
                else "abre o compositor para você confirmar")
        d.motivo = base + (" · personalizado: " + ", ".join(trocados) if trocados else "")
        plano.append(d)

    return plano


def resumo_do_plano(plano: list[Alvo]) -> dict:
    """Contagens para a tela, sem ela precisar recontar."""
    automaticas = [d for d in plano if d.publica and d.transporte == "instagrapi"]
    assistidas = [d for d in plano if d.publica and d.transporte != "instagrapi"]
    recusadas = [d for d in plano if not d.publica]
    return {
        "total": len(plano),
        "automaticas": len(automaticas),
        "assistidas": len(assistidas),
        "recusadas": len(recusadas),
        "redes_automaticas": [d.rede_id for d in automaticas],
        "redes_assistidas": [d.rede_id for d in assistidas],
    }


# ---------------------------------------------------------------------------
# O ORQUESTRADOR
# ---------------------------------------------------------------------------


def _espalhamento(quantas: int, total_segundos: float) -> list[float]:
    """Quando cada rede sai, em segundos a partir do início da leva.

    Espaçamento regular é de propósito aqui: o que se evita é a saída
    simultânea, não a regularidade — e intervalo sorteado dentro de uma janela
    de 12 segundos não compra humanidade nenhuma, só tira a reprodutibilidade
    do relatório. A primeira sai na hora.
    """
    if quantas <= 1 or total_segundos <= 0:
        return [0.0] * max(0, quantas)
    passo = float(total_segundos) / float(quantas - 1)
    return [i * passo for i in range(quantas)]


async def publicar_em_todas(perfil: dict, contas: list[dict], conteudo: dict,
                            redes_pedidas: Optional[list[str]] = None,
                            espalhar_segundos: float = ESPALHAR_SEGUNDOS_PADRAO,
                            simular: bool = False,
                            personalizacoes: Optional[dict] = None,
                            rotacionar=None,
                            auto_publicar: Optional[list] = None,
                            adaptar: bool = True,
                            log=None) -> dict:
    """Publica o conteúdo em todas as redes do perfil, na mesma leva.

    `auto_publicar` é a lista de redes liberadas a APERTAR publicar sozinhas.
    Vazia por padrão: rede com receita medida preenche a tela e PARA, e só
    entra aqui depois de ter acertado uma vez na sua frente (decisão do usuário
    em 13/10, o mesmo espírito do `dryRun` do InstaFollow).

    Devolve o plano com o desfecho de cada rede. **Nenhuma falha derruba a
    leva**: rede que falha é relatada e as outras seguem — a alternativa seria
    uma publicação sair em três redes, estourar na quarta e deixar o usuário
    sem saber onde parou.

    `simular=True` decide tudo e não publica nada. É o padrão do projeto para
    estreia de motor (ver o `dryRun` do InstaFollow): a camada que nunca rodou
    é justamente a que não se liga confiando.
    """
    fala = log or (lambda *_: None)
    # `adaptar` TEM de chegar aqui: a previa e a leva precisam usar a mesma
    # regra, senao a tela mostra uma coisa e a publicacao sai outra -- que e
    # o defeito que esta aba ja teve quando edicao e publicacao viviam
    # separadas (13/10).
    plano = montar_plano(contas, conteudo, redes_pedidas, personalizacoes,
                         adaptar=adaptar)
    vao = [d for d in plano if d.publica]

    resumo = resumo_do_plano(plano)
    fala("📣 leva de publicação: %d rede(s) na fila, %d recusada(s)"
         % (len(vao), resumo["recusadas"]))
    for d in plano:
        if not d.publica:
            fala("   ⤫ %s — %s" % (nome(d.rede_id), d.motivo))

    if not vao:
        return {"ok": False, "erro": "nenhuma rede pôde receber este conteúdo",
                "plano": [d.como_dict() for d in plano], "resumo": resumo}

    # UMA rotação para a leva inteira — ver a nota no topo do arquivo.
    executaveis = [d for d in vao if d.transporte != "instagrapi"
                   or d.rede_id in (auto_publicar or ())]
    rotacao = (await _rotacionar_uma_vez(perfil, simular, fala, rotacionar)
               if executaveis else {"ok": True, "pulou": "sem envio autorizado"})

    atrasos = _espalhamento(len(vao), espalhar_segundos)
    inicio = time.time()

    async def uma(d: Alvo, atraso: float):
        if not simular and d.transporte == "instagrapi" and d.rede_id not in (auto_publicar or ()):
            d.desfecho = "pendente"
            d.detalhe = "Instagram não enviado: publicação automática não autorizada"
            fala("   ⏸ %s: %s" % (nome(d.rede_id), d.detalhe))
            return
        if atraso > 0:
            await asyncio.sleep(atraso)
        d.quando = time.time() - inicio
        if simular:
            d.desfecho = "simulado"
            d.detalhe = "simulação ligada — nada foi publicado"
            fala("   ◌ %s: simulado (+%.1fs)" % (nome(d.rede_id), d.quando))
            return
        try:
            # CADA REDE RECEBE A ARTE NO FORMATO DELA. Compor uma vez só e mandar
            # para todas devolveria 9:16 num post 1:1 — e, pior, a versão antiga
            # mandava a imagem CRUA com coordenadas fixas (0.5, 0.8, 0.6, 0.1),
            # o que publica um sticker de link sem chip desenhado: exatamente o
            # defeito invisível de 28/08, de volta por outro caminho.
            await _compor_para(d, conteudo)
            # O conteúdo que sai é o DA REDE (já personalizado no plano), nunca
            # o comum: era aqui que a prévia e a publicação divergiriam.
            if d.transporte == "instagrapi":
                d.resultado = await _publicar_instagram(perfil, d)
                d.desfecho = "publicado"
                d.detalhe = "%s publicado como @%s" % (
                    d.destino, d.resultado.get("conta") or d.username)
                if d.resultado.get("aviso"):
                    d.detalhe += " — " + d.resultado["aviso"]
            else:
                liberada = d.rede_id in (auto_publicar or ())
                d.resultado = await _abrir_compositor(perfil, d, liberada)
                modo = d.resultado.get("modo") or "assistido"
                if modo == "publicado":
                    d.desfecho = "publicado"
                    d.detalhe = "publicado pelo compositor da rede"
                elif modo == "preenchido":
                    d.desfecho = "preenchido"
                    d.detalhe = ("arquivo e legenda já estão na tela — confira e "
                                 "aperte publicar na janela do perfil")
                    if d.resultado.get("publicar_habilitado") is False:
                        d.detalhe += " (⚠ o botão está desabilitado)"
                else:
                    d.desfecho = "assistido"
                    d.detalhe = "compositor aberto — confirme na janela do perfil"
            fala("   ✓ %s: %s (+%.1fs)" % (nome(d.rede_id), d.desfecho, d.quando))
        except Exception as e:  # a leva não cai por causa de uma rede
            d.desfecho = "falhou"
            d.detalhe = str(e)
            fala("   ✗ %s: %s" % (nome(d.rede_id), e))

    await asyncio.gather(*(uma(d, a) for d, a in zip(vao, atrasos)))

    publicadas = sum(1 for d in plano if d.desfecho == "publicado")
    preenchidas = sum(1 for d in plano if d.desfecho == "preenchido")
    assistidas = sum(1 for d in plano if d.desfecho == "assistido")
    falhas = sum(1 for d in plano if d.desfecho == "falhou")
    pendentes = sum(1 for d in plano if d.desfecho == "pendente")
    fala("📣 leva encerrada: %d publicada(s), %d preenchida(s) esperando o seu "
         "clique, %d aberta(s), %d falha(s)"
         % (publicadas, preenchidas, assistidas, falhas))

    return {
        "ok": falhas == 0,
        "simulado": simular,
        "publicadas": publicadas,
        "assistidas": assistidas,
        "preenchidas": preenchidas,
        "pendentes": pendentes,
        "falhas": falhas,
        "rotacao_ip": rotacao,
        "plano": [d.como_dict() for d in plano],
        "resumo": resumo,
    }


async def _rotacionar_uma_vez(perfil: dict, simular: bool, fala, rotacionar) -> dict:
    """Renova o IP ANTES da leva, uma vez, chamando quem o chamador injetou.

    A rotação NÃO é reimplementada aqui de propósito. `api._rotate_ip_for` já
    resolve essa pergunta — inclusive a guarda do perfil com proxy próprio, o
    no-op quando a rotação está desligada e a tolerância a falha — e duas
    versões da mesma decisão é como nascem os dois comportamentos que
    divergem. A primeira tentativa deste arquivo tinha uma versão própria, com
    dois erros que a função existente não tem: chamava `modem.rotate` (que é
    `async`) por `to_thread`, e lia `config()["rotate_enabled"]`, chave que não
    existe — a de verdade é `enabled`, então a rotação nunca teria rodado e o
    terminal diria "desligada" para sempre.

    Sem `rotacionar` injetado, a leva não rotaciona e diz isso.
    """
    if simular:
        return {"ok": True, "pulou": "simulação"}
    if rotacionar is None:
        return {"ok": True, "pulou": "sem rotação (nenhuma função injetada)"}
    fala("🔄 renovando o IP antes da leva (~20s) — uma vez para todas as redes")
    r = await rotacionar(perfil)
    if r.get("skipped"):
        fala("🔄 rotação pulada: %s" % (r.get("reason") or "desligada nos ajustes"))
    elif r.get("ips"):
        fala("🔄 IP renovado: %s" % ", ".join(r["ips"]))
    return r


#: Onde o chip fica quando ninguém posicionou nada. Serve só de rede de
#: segurança: na prática a tela sempre manda o retângulo que você arrastou.
_RECT_PADRAO = {"x": 0.5, "y": 0.8, "largura": 0.6, "altura": 0.1}


async def _compor_para(d: Alvo, conteudo: dict) -> None:
    """Desenha a arte DESTA rede, no formato dela, e guarda no Alvo.

    Roda por rede e não uma vez para todas porque o formato é por rede: a mesma
    foto vira 4:5 no Instagram, 16:9 no X e 9:16 no TikTok, e o chip é uma
    fração do QUADRO — então ele muda de tamanho junto.

    Vídeo passa direto: o compositor é de imagem (Pillow), e mexer num vídeo
    exigiria ffmpeg, que este projeto ainda não instalou.

    Falha ao compor não é engolida: ela sobe e a rede é marcada como falha, com
    as outras seguindo. Publicar a imagem crua "para não perder a leva" sairia
    sem o chip, que é o defeito que esta função existe para impedir.
    """
    import story

    # No Reels a mídia é o vídeo e a ARTE é a capa: é ela que ganha o chip e
    # é ela que aparece na grade do perfil. Então o compositor recebe a capa, e
    # o vídeo passa intacto -- mexer num vídeo exigiria ffmpeg, que este
    # projeto ainda não instalou.
    if d.destino == "reels":
        d.arquivo_composto = d.arquivo
        if not d.capa:
            raise ValueError("Reels sem capa — o plano deveria ter recusado antes")
        fonte = d.capa
    elif not d.arquivo or tipo_do_conteudo({"arquivo": d.arquivo}) == "video":
        d.arquivo_composto = d.arquivo
        return
    else:
        fonte = d.arquivo

    dados = Path(fonte).read_bytes()
    post = d.post or None
    mini = None
    if post and post.get("mini"):
        f = story.PASTA / post["mini"]
        if f.exists():
            mini = f.read_bytes()

    rect = conteudo.get("rect") or _RECT_PADRAO
    r = await asyncio.to_thread(
        story.compor, dados, d.link,
        float(rect["x"]), float(rect["y"]),
        float(rect["largura"]), float(rect["altura"]),
        conteudo.get("rotulo") or "", post, mini,
        story.tamanho_do_formato(d.formato),
        bool(conteudo.get("com_chip", True)),
    )
    if d.destino == "reels":
        d.capa_composta = r["caminho"]
    else:
        d.arquivo_composto = r["caminho"]
    # Com repost o chip desce para debaixo do card; o `compor` diz para onde, e
    # é esse retângulo que vira a área tocável — senão desenho e toque se
    # separam (a armadilha medida em 28/08).
    d.link_rect = r.get("link_rect") or dict(rect)


async def _publicar_instagram(perfil: dict, d: Alvo) -> dict:
    """O caminho que já funciona, reusado inteiro — agora story OU feed.

    Delega ao `story.py` em vez de refazer: a autenticação por cookie sem login
    é o que não queima a conta, e duplicar isso aqui criaria uma segunda versão
    do único trecho que não pode divergir.

    Recebe o `Alvo` e não o conteúdo comum porque é o Alvo que carrega a
    personalização já resolvida — arquivo, legenda, link e destino desta rede.
    """
    import story

    arte = d.arquivo_composto or d.arquivo
    if not arte:
        raise ValueError("o Instagram precisa de imagem ou vídeo")

    r = d.link_rect or _RECT_PADRAO
    return await asyncio.to_thread(
        story.publicar, perfil, arte, d.link,
        r["x"], r["y"], r["largura"], r["altura"],
        d.post or None, d.link_rect or None, None, d.destino, d.legenda,
        d.capa_composta or d.capa,
    )


async def _abrir_compositor(perfil: dict, d: Alvo,
                            auto_publicar: bool = False) -> dict:
    """Abre o compositor da rede na janela do perfil — e PREENCHE, se houver
    receita medida para ela.

    Uma aba por rede, na MESMA janela — é o que uma pessoa faz, e é o que
    mantém a leva dentro de uma sessão e de uma época de IP.

    Rede com receita em `publicador.RECEITAS` recebe o arquivo e a legenda e
    para **antes do clique final**; só aperta publicar se `auto_publicar`, que
    a leva liga por rede, depois daquela rede ter acertado uma vez (decisão do
    usuário em 13/10). Rede sem receita continua como era: abre e espera você.

    Falha ao preencher NÃO derruba a aba: ela fica aberta no ponto onde parou,
    com o que já entrou. Fechar seria jogar fora o upload e obrigar a refazer.
    """
    import publicador as pub
    import session_manager as sm

    r = rede(d.rede_id) or {}
    url = r.get("compositor")
    if not url:
        raise ValueError("não sei onde fica o compositor do %s" % nome(d.rede_id))

    # A arte COMPOSTA é o que vai para a rede — não a imagem crua, que não tem
    # o chip desenhado.
    arte = d.arquivo_composto or d.arquivo
    base = {"compositor": url, "arquivo": arte, "capa": d.capa_composta or "",
            "legenda": d.legenda, "destino": d.destino, "formato": d.formato}

    if not pub.tem_receita(d.rede_id):
        await sm.abrir_aba(perfil, url)
        base["modo"] = "assistido"
        return base

    async def _na_aba(page):
        feito = await pub.preencher(page, d.rede_id, arte, d.legenda)
        if auto_publicar:
            feito.update(await pub.apertar_publicar(page, d.rede_id))
        return feito

    aberta = await sm.abrir_aba(perfil, url, fn=_na_aba)
    base.update(aberta.get("resultado") or {})
    base["modo"] = "publicado" if base.get("publicou") else "preenchido"
    return base
