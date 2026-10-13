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
# `medido`: alguém já exercitou este caminho ao vivo? Começa False em tudo que
# não é o Instagram, e é o campo que impede o painel de prometer o que não foi
# conferido.

REDES: tuple[dict, ...] = (
    {
        "id": "instagram",
        "nome": "Instagram",
        "transporte": "instagrapi",
        "destinos": ("story", "feed"),
        "formato_padrao": {"story": "story", "feed": "vertical"},
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
        "compositor": "https://www.facebook.com/",
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
        "compositor": "https://www.tiktok.com/tiktokstudio/upload",
        "formatos": ("video",),
        "legenda_max": 2200,
        "medido": False,
        "nota": "a Content Posting API exige app aprovado e domínio "
                "verificado — fora do alcance destas contas",
    },
    {
        "id": "youtube",
        "nome": "YouTube",
        "transporte": "navegador",
        "destinos": ("feed",),
        "formato_padrao": {"feed": "paisagem"},
        "compositor": "https://studio.youtube.com/",
        "formatos": ("video",),
        "legenda_max": 5000,
        "medido": False,
        "nota": "única rede grande com API gratuita viável (Data API v3, "
                "OAuth) — candidata a sair do assistido primeiro",
    },
    {
        "id": "kwai",
        "nome": "Kwai",
        "transporte": "navegador",
        "destinos": ("feed",),
        "formato_padrao": {"feed": "story"},
        "compositor": "https://www.kwai.com/",
        "formatos": ("video",),
        "legenda_max": 500,
        "medido": False,
        "nota": "relevante no Brasil; sem API pública e upload web a confirmar",
    },
    {
        "id": "linkedin",
        "nome": "LinkedIn",
        "transporte": "navegador",
        "destinos": ("feed",),
        "formato_padrao": {"feed": "quadrado"},
        "compositor": "https://www.linkedin.com/feed/",
        "formatos": ("imagem", "video", "texto"),
        "legenda_max": 3000,
        "medido": False,
        "nota": "a API de publicação exige aprovação da Community "
                "Management API",
    },
    {
        "id": "pinterest",
        "nome": "Pinterest",
        "transporte": "navegador",
        "destinos": ("feed",),
        "formato_padrao": {"feed": "pin"},
        "compositor": "https://www.pinterest.com/pin-builder/",
        "formatos": ("imagem", "video"),
        "legenda_max": 500,
        "medido": False,
        "nota": "pin precisa de imagem; texto puro não existe aqui",
    },
    {
        "id": "bluesky",
        "nome": "Bluesky",
        "transporte": "navegador",
        "destinos": ("feed",),
        "formato_padrao": {"feed": "quadrado"},
        "compositor": "https://bsky.app/",
        "formatos": ("imagem", "video", "texto"),
        "legenda_max": 300,
        "medido": False,
        "nota": "o AT Protocol é aberto e aceita app password — a mais "
                "fácil de virar automática de verdade",
    },
    {
        "id": "telegram",
        "nome": "Telegram",
        "transporte": "navegador",
        "destinos": ("feed",),
        "formato_padrao": {"feed": "quadrado"},
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
    #: story | feed — o que esta rede vai receber nesta leva
    destino: str = "feed"
    #: nome do formato de arte (ver story.FORMATOS)
    formato: str = ""
    #: já com a personalização aplicada por cima do conteúdo comum
    legenda: str = ""
    link: str = ""
    arquivo: str = ""
    #: a arte composta NO FORMATO desta rede (preenchida na hora de publicar)
    arquivo_composto: str = ""
    #: onde o chip acabou desenhado nessa arte -- vira a area tocavel
    link_rect: dict = field(default_factory=dict)
    #: publicacao de outra pessoa a repostar (so story)
    post: dict = field(default_factory=dict)
    personalizado: tuple = ()
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
            "personalizado": list(self.personalizado),
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


def montar_plano(contas: list[dict], conteudo: dict,
                 redes_pedidas: Optional[list[str]] = None,
                 personalizacoes: Optional[dict] = None) -> list[Alvo]:
    """Quem publica, com o quê, e quem é recusado por quê — antes de tocar em
    rede nenhuma.

    `contas` são as contas do perfil (uma por rede, como `list_accounts_by_profile`
    devolve). `redes_pedidas` vazio significa TODAS as que o perfil tem.
    `personalizacoes` é `{rede: {legenda, link, arquivo, formato, destino}}` e
    vence o conteúdo comum, campo a campo.

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
        d.legenda = final.get("legenda") or ""
        d.link = final.get("link") or ""
        d.arquivo = final.get("arquivo") or ""
        d.formato = final.get("formato") or ""
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
                            log=None) -> dict:
    """Publica o conteúdo em todas as redes do perfil, na mesma leva.

    Devolve o plano com o desfecho de cada rede. **Nenhuma falha derruba a
    leva**: rede que falha é relatada e as outras seguem — a alternativa seria
    uma publicação sair em três redes, estourar na quarta e deixar o usuário
    sem saber onde parou.

    `simular=True` decide tudo e não publica nada. É o padrão do projeto para
    estreia de motor (ver o `dryRun` do InstaFollow): a camada que nunca rodou
    é justamente a que não se liga confiando.
    """
    fala = log or (lambda *_: None)
    plano = montar_plano(contas, conteudo, redes_pedidas, personalizacoes)
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
    rotacao = await _rotacionar_uma_vez(perfil, simular, fala, rotacionar)

    atrasos = _espalhamento(len(vao), espalhar_segundos)
    inicio = time.time()

    async def uma(d: Alvo, atraso: float):
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
                d.resultado = await _abrir_compositor(perfil, d)
                d.desfecho = "assistido"
                d.detalhe = "compositor aberto — confirme na janela do perfil"
            fala("   ✓ %s: %s (+%.1fs)" % (nome(d.rede_id), d.desfecho, d.quando))
        except Exception as e:  # a leva não cai por causa de uma rede
            d.desfecho = "falhou"
            d.detalhe = str(e)
            fala("   ✗ %s: %s" % (nome(d.rede_id), e))

    await asyncio.gather(*(uma(d, a) for d, a in zip(vao, atrasos)))

    publicadas = sum(1 for d in plano if d.desfecho == "publicado")
    assistidas = sum(1 for d in plano if d.desfecho == "assistido")
    falhas = sum(1 for d in plano if d.desfecho == "falhou")
    fala("📣 leva encerrada: %d publicada(s), %d aguardando confirmação, %d falha(s)"
         % (publicadas, assistidas, falhas))

    return {
        "ok": falhas == 0,
        "simulado": simular,
        "publicadas": publicadas,
        "assistidas": assistidas,
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
    foto vira 4:5 no Instagram, 16:9 no X e 2:3 no Pinterest, e o chip é uma
    fração do QUADRO — então ele muda de tamanho junto.

    Vídeo passa direto: o compositor é de imagem (Pillow), e mexer num vídeo
    exigiria ffmpeg, que este projeto ainda não instalou.

    Falha ao compor não é engolida: ela sobe e a rede é marcada como falha, com
    as outras seguindo. Publicar a imagem crua "para não perder a leva" sairia
    sem o chip, que é o defeito que esta função existe para impedir.
    """
    import story

    if not d.arquivo or tipo_do_conteudo({"arquivo": d.arquivo}) == "video":
        d.arquivo_composto = d.arquivo
        return

    dados = Path(d.arquivo).read_bytes()
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
    )


async def _abrir_compositor(perfil: dict, d: Alvo) -> dict:
    """Modo assistido: abre o compositor da rede na janela do perfil.

    Uma aba por rede, na MESMA janela — é o que uma pessoa faz, e é o que
    mantém a leva dentro de uma sessão e de uma época de IP. A publicação em si
    é confirmada pelo usuário até o fluxo daquela rede ser medido.
    """
    import session_manager as sm

    r = rede(d.rede_id) or {}
    url = r.get("compositor")
    if not url:
        raise ValueError("não sei onde fica o compositor do %s" % nome(d.rede_id))

    await sm.abrir_aba(perfil, url)
    # A arte COMPOSTA é o que você vai arrastar para o compositor da rede — não
    # a imagem crua, que não tem o chip.
    return {"compositor": url, "arquivo": d.arquivo_composto or d.arquivo,
            "legenda": d.legenda, "destino": d.destino, "formato": d.formato}
