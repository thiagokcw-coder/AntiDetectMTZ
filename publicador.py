# -*- coding: utf-8 -*-
"""Preencher o compositor de cada rede — com seletores MEDIDOS, nunca chutados.

POR QUE ESTE MÓDULO EXISTE
--------------------------
A aba Publicar promete publicar em todas as redes de uma vez. Só o Instagram
fazia isso sozinho (instagrapi); as outras abriam o compositor e esperavam você
arrastar o arquivo e escrever a legenda — ou seja, continuava uma a uma.

Tudo aqui saiu de `compositor.py` lendo a tela de verdade, com a data ao lado de
cada receita. **Nenhum seletor foi escrito de cabeça**: adivinhar rota é o erro
que custou dias a esta base (o 404 de agosto), e dez fluxos de DOM inventados
seriam dez vezes o mesmo erro.

O QUE ELE FAZ, E ONDE PARA
--------------------------
`preencher` leva o arquivo e a legenda até a tela e **para antes do clique
final**. Publicar é `apertar_publicar`, uma chamada à parte, que a leva só faz
quando aquela rede já acertou uma vez — a decisão do usuário em 13/10, e o mesmo
espírito do `dryRun` do InstaFollow: motor novo estreia sem poder estragar nada.

AS DUAS ARMADILHAS QUE AS MEDIÇÕES MOSTRARAM
--------------------------------------------
1. **Os editores são Draft.js** (X e TikTok). `fill()` e escrever em `value` não
   funcionam neles: o DOM muda e o estado interno do editor continua vazio, e aí
   a publicação sai sem legenda. Só evento de teclado de verdade entra — daí
   `_escrever`, que clica, limpa e digita.
2. **O TikTok pré-preenche a legenda com o NOME DO ARQUIVO** (visto na tela em
   14/10 e de novo em 23/10). Sem limpar antes, a legenda sai
   "YTDown.com_Shorts_Media_...". Por isso `_escrever` sempre limpa, em toda
   rede: campo que deveria estar vazio e está não custa nada para limpar.
"""
import asyncio

#: Quanto esperar, em ms, por um elemento que só aparece depois de uma ação
#: (o botão de publicar do TikTok só nasce quando o vídeo termina de subir).
ESPERA_PADRAO_MS = 120000


# ─── As receitas, uma por rede ───────────────────────────────────────────────
#
# `arquivo`   : onde mandar o arquivo. `input[type=file]` oculto serve: o
#               `set_input_files` funciona em campo escondido, clique não.
# `depois_do_arquivo`: o que esperar aparecer para saber que o upload acabou.
# `legenda`   : o editor. Lista de candidatos, tentados em ordem.
# `publicar`  : o botão final. NUNCA é clicado por `preencher`.
# `medido_em` : quando alguém olhou esta tela. Receita sem data é receita velha.
RECEITAS: dict[str, dict] = {
    "x": {
        "url": "https://x.com/compose/post",
        # Medido em 23/10 no perfil cap.martim.rs.2026 (@Uncensoredbras).
        # ⚠ `fileInput` e `tweetTextarea_0` aparecem DUAS vezes na página: o X
        # desenha o compositor do modal E o da linha do tempo atrás dele. Por
        # isso os candidatos são tentados em ordem e o resultado é CONFERIDO
        # (ver `_escrever`) — escolher no escuro escreveria no lugar errado.
        "arquivo": ['[role=dialog] input[data-testid="fileInput"]',
                    'input[data-testid="fileInput"]'],
        "depois_do_arquivo": "",
        "legenda": ['[role=dialog] div[data-testid="tweetTextarea_0"]',
                    'div[data-testid="tweetTextarea_0"]'],
        "publicar": ['[role=dialog] button[data-testid="tweetButton"]',
                     'button[data-testid="tweetButton"]',
                     'button[data-testid="tweetButtonInline"]'],
        "medido_em": "2026-10-23",
        # A interface desta conta está em INGLÊS ("Post text", "Post"), enquanto
        # a do TikTok veio em português. Nenhum seletor daqui depende de texto
        # visível, e é por isso que isso não quebra nada.
        "nota": "aceita jpeg/png/webp/gif e mp4/mov; o botão nasce desabilitado "
                "e habilita quando há texto ou mídia",
    },
    "tiktok": {
        "url": "https://www.tiktok.com/tiktokstudio/upload",
        # Medido em 23/10, nas DUAS telas. A ordem importa e não é obvia: o
        # `input[type=file]` só existe ANTES do vídeo entrar, e o botão de
        # publicar só existe DEPOIS. Uma leitura só nunca veria os dois.
        "arquivo": ['div[data-e2e="select_video_container"] input[type=file]',
                    'input[accept*="video"]'],
        "depois_do_arquivo": 'button[data-e2e="post_video_button"]',
        "legenda": ['div.caption-editor [contenteditable="true"]',
                    'div.caption-editor'],
        "publicar": ['button[data-e2e="post_video_button"]'],
        "medido_em": "2026-10-23",
        "nota": "só vídeo; a legenda vem PRÉ-PREENCHIDA com o nome do arquivo",
    },
}


def receita(rede_id: str) -> dict:
    return RECEITAS.get((rede_id or "").strip().lower()) or {}


def tem_receita(rede_id: str) -> bool:
    return bool(receita(rede_id))


# ─── Peças ───────────────────────────────────────────────────────────────────

async def _primeiro_que_existe(page, seletores, timeout_ms: int = 15000):
    """O primeiro candidato que aparecer. Candidatos em vez de um seletor só
    porque a mesma tela pode ter duas cópias do campo (o X tem) e porque uma
    classe gerada por build muda sozinha — o segundo candidato é o colchão."""
    por_candidato = max(1000, int(timeout_ms / max(1, len(seletores))))
    ultimo = None
    for sel in seletores:
        try:
            alvo = page.locator(sel).first
            await alvo.wait_for(state="attached", timeout=por_candidato)
            return alvo, sel
        except Exception as e:
            ultimo = e
    raise RuntimeError("não achei nenhum destes na tela: %s (%s)"
                       % (" | ".join(seletores), str(ultimo)[:120]))


async def _escrever(page, seletores, texto: str) -> str:
    """Escreve no editor e CONFERE que entrou. Devolve o seletor que funcionou.

    Teclado de verdade, nunca `fill`: os dois editores medidos são Draft.js, e
    nele `fill` deixa o DOM com cara de preenchido e o estado interno vazio — a
    publicação sairia sem legenda e ninguém veria o erro até depois de publicar.

    Limpa antes SEMPRE. O TikTok pré-preenche a legenda com o nome do arquivo
    (medido), e limpar um campo que já está vazio não custa nada.

    A conferência é o que resolve o caso do X, que tem DOIS editores iguais na
    mesma página: se o texto não apareceu neste, tenta o próximo.
    """
    erros = []
    for sel in seletores:
        try:
            alvo = page.locator(sel).first
            await alvo.wait_for(state="visible", timeout=8000)
            await alvo.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            if texto:
                await page.keyboard.type(texto, delay=12)
            # Conferir: o editor tem que CONTER o que foi digitado.
            if texto:
                lido = (await alvo.inner_text()) or ""
                if texto.strip()[:40] not in lido:
                    erros.append("%s: digitei e não encontrei o texto de volta" % sel)
                    continue
            return sel
        except Exception as e:
            erros.append("%s: %s" % (sel, str(e)[:90]))
    raise RuntimeError("não consegui escrever a legenda — " + " · ".join(erros))


async def preencher(page, rede_id: str, arquivo: str = "", legenda: str = "",
                    espera_ms: int = ESPERA_PADRAO_MS) -> dict:
    """Leva arquivo e legenda até o compositor e PARA antes de publicar.

    Não clica em publicar, em nenhuma circunstância. Quem publica é
    `apertar_publicar`, de propósito numa chamada separada: um fluxo que
    preenche e publica no mesmo caminho não tem onde parar quando o seletor
    estiver errado.
    """
    r = receita(rede_id)
    if not r:
        raise ValueError("não tenho receita medida para %r — meça o compositor "
                         "dela antes (botão 🔬 na aba Publicar)" % rede_id)

    feito = {"rede": rede_id, "arquivo": "", "legenda": "", "publicou": False,
             "medido_em": r.get("medido_em", "")}

    if arquivo:
        alvo, sel = await _primeiro_que_existe(page, r["arquivo"])
        await alvo.set_input_files(arquivo)
        feito["arquivo"] = sel
        esperar = r.get("depois_do_arquivo")
        if esperar:
            # O upload pode demorar: o botão de publicar do TikTok só nasce
            # quando o vídeo termina de subir. Esperar o ELEMENTO, e não um
            # relógio, é a mesma lição do medidor (14/10).
            await page.locator(esperar).first.wait_for(state="visible",
                                                       timeout=espera_ms)

    if legenda or r.get("limpa_sempre", True):
        feito["legenda"] = await _escrever(page, r["legenda"], legenda)

    # O botão tem que existir — senão "preenchido" seria mentira: a tela não
    # chegou no estado de publicar.
    alvo, sel = await _primeiro_que_existe(page, r["publicar"])
    feito["publicar"] = sel
    try:
        feito["publicar_habilitado"] = await alvo.is_enabled()
    except Exception:
        feito["publicar_habilitado"] = None
    return feito


async def apertar_publicar(page, rede_id: str) -> dict:
    """Clica no botão final. Chamada à parte, e só quando quem manda disse que
    sim — a leva só chega aqui com a rede marcada como já conferida."""
    r = receita(rede_id)
    if not r:
        raise ValueError("não tenho receita medida para %r" % rede_id)
    alvo, sel = await _primeiro_que_existe(page, r["publicar"])
    if not await alvo.is_enabled():
        raise RuntimeError("o botão de publicar do %s está desabilitado — a tela "
                           "não está pronta (falta mídia ou legenda?)" % rede_id)
    await alvo.click()
    await asyncio.sleep(2.0)
    return {"rede": rede_id, "publicou": True, "publicar": sel}
