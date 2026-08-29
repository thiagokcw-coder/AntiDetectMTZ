# -*- coding: utf-8 -*-
"""Story com sticker de link: compor a arte e publicar pela API do app.

POR QUE ESTE MODULO EXISTE (2026-08-28)
---------------------------------------
Medido no mesmo dia, nesta ordem:

1. O Instagram web mobile TEM criação de story, mas NÃO tem o sticker de link —
   que era o ponto, porque o destino é o grupo de WhatsApp.
2. A API privada do app tem o sticker. E a sessão do perfil Camoufox autentica
   nela SEM login novo (`sondar_story.py`: ds_user_id do cookie == pk da API),
   o que evita o evento de "login de novo dispositivo" que queimava conta.
3. Publicou. Mas **o sticker saiu invisível**: o link era tocável e não aparecia.

A causa do (3) está no payload da própria instagrapi: o sticker leva posição,
tamanho e URL e **nenhum campo de aparência** — sem texto, sem cor, sem fonte.
O chip visível é desenhado pelo APP na hora de compor; pela API sobe uma imagem
achatada e o servidor guarda só a região tocável.

Daí este módulo: **desenhar o chip na imagem** e publicar com a MESMA coordenada.
Desenho e região tocável saem do mesmo retângulo, então não há como ficarem
desalinhados — que seria o pior defeito possível aqui (um chip visível que não
clica é pior que nenhum chip).
"""
import io
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Onde a arte composta é guardada. Fica ao lado dos CSVs do Spider, no mesmo
# `data/`, para o backup do projeto levar junto.
PASTA = Path(__file__).parent / "data" / "_stories"

# 9:16 é o formato do story. Imagem fora disso é encaixada com fundo, em vez de
# esticada — esticar deforma a arte e denuncia montagem automática.
LARGURA, ALTURA = 1080, 1920

# Fontes candidatas, da mais próxima do Instagram para a mais genérica. Se
# nenhuma existir, cai na bitmap do Pillow: feia, mas nunca derruba a composição.
_FONTES = ["segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "arial.ttf"]


def _fonte(tamanho: int):
    for nome in _FONTES:
        try:
            return ImageFont.truetype(nome, tamanho)
        except Exception:
            continue
    return ImageFont.load_default()


def _texto_do_link(link: str) -> str:
    """O que aparece no chip. O Instagram mostra o domínio, não a URL inteira."""
    t = (link or "").strip()
    for prefixo in ("https://", "http://"):
        if t.lower().startswith(prefixo):
            t = t[len(prefixo):]
    return (t[:38] + "…") if len(t) > 39 else t or "link"


def _encaixar(img: Image.Image) -> Image.Image:
    """Encaixa em 1080x1920 SEM esticar: a arte inteira cabe, o resto vira fundo."""
    img = img.convert("RGB")
    if (img.width, img.height) == (LARGURA, ALTURA):
        return img
    escala = min(LARGURA / img.width, ALTURA / img.height)
    novo = img.resize((max(1, int(img.width * escala)), max(1, int(img.height * escala))),
                      Image.LANCZOS)
    fundo = Image.new("RGB", (LARGURA, ALTURA), (16, 16, 16))
    fundo.paste(novo, ((LARGURA - novo.width) // 2, (ALTURA - novo.height) // 2))
    return fundo


def compor(dados: bytes, link: str, x: float, y: float,
           largura: float, altura: float) -> dict:
    """Desenha o chip do link na imagem e devolve o arquivo pronto para publicar.

    x/y são o CENTRO do retângulo, e largura/altura o tamanho — todos de 0 a 1,
    exatamente como o `StoryLink` da instagrapi os interpreta. Usar a mesma
    convenção nos dois lados é o que garante que o desenho fique embaixo da área
    que o Instagram torna tocável.
    """
    img = _encaixar(Image.open(io.BytesIO(dados)))
    d = ImageDraw.Draw(img, "RGBA")

    cx, cy = float(x) * LARGURA, float(y) * ALTURA
    cw, ch = float(largura) * LARGURA, float(altura) * ALTURA
    caixa = [cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2]
    raio = max(8, int(min(cw, ch) * 0.28))

    # Chip claro com texto escuro, que é como o sticker de link aparece hoje.
    d.rounded_rectangle(caixa, radius=raio, fill=(255, 255, 255, 235))

    texto = _texto_do_link(link)
    tam = max(14, int(ch * 0.34))
    fonte = _fonte(tam)
    # Encolhe até caber com folga: texto estourando a borda seria pior que pequeno.
    while tam > 12:
        larg = d.textbbox((0, 0), texto, font=fonte)[2]
        if larg <= cw * 0.86:
            break
        tam = int(tam * 0.9)
        fonte = _fonte(tam)

    x0, y0, x1, y1 = d.textbbox((0, 0), texto, font=fonte)
    d.text((cx - (x1 - x0) / 2 - x0, cy - (y1 - y0) / 2 - y0), texto,
           font=fonte, fill=(20, 20, 20, 255))

    PASTA.mkdir(parents=True, exist_ok=True)
    nome = "story_%d.jpg" % int(time.time() * 1000)
    caminho = PASTA / nome
    img.save(caminho, "JPEG", quality=92)
    return {"arquivo": nome, "caminho": str(caminho),
            "largura_px": LARGURA, "altura_px": ALTURA}


def publicar(perfil: dict, caminho: str, link: str,
             x: float, y: float, largura: float, altura: float) -> dict:
    """Publica o story pela API do app, reusando a sessão do perfil.

    NÃO faz login: os cookies saem do perfil Camoufox (sem nenhuma requisição ao
    Instagram) e entram por `login_by_sessionid`. Era o login com usuário e senha
    que queimava conta em 26/07 — "não é a velocidade nem o cap, é o LOGIN".
    """
    import asyncio
    import ig_session as igs
    from instagrapi import Client
    from instagrapi.types import StoryLink
    from paths import APP_DIR

    cookies = asyncio.run(igs.export_cookies(perfil))
    sid = next((c.get("value") for c in cookies
                if c.get("name") == "sessionid" and c.get("value")), None)
    if not sid:
        raise RuntimeError("este perfil não tem sessionid — abra o perfil e confirme "
                           "que a conta está logada")

    cl = Client()
    sessoes = Path(APP_DIR) / "ig_sessions"
    sessoes.mkdir(parents=True, exist_ok=True)
    # Device ESTÁVEL por perfil: aparelho diferente a cada publicação seria pior
    # que o risco que estamos evitando.
    arq = sessoes / ("story_%s.json" % perfil.get("id"))
    if arq.exists():
        try:
            cl.load_settings(arq)
        except Exception:
            pass

    # Coerência com o resto da operação: contas do RS saindo pelo modem de POA.
    cl.set_country("BR")
    cl.set_locale("pt_BR")
    cl.set_timezone_offset(-3 * 3600)

    proxy = perfil.get("proxy")
    if proxy and proxy.get("host"):
        usuario = proxy.get("username")
        cred = ("%s:%s@" % (usuario, proxy.get("password"))) if usuario else ""
        cl.set_proxy("%s://%s%s:%s" % (proxy.get("protocol", "http"), cred,
                                       proxy["host"], proxy["port"]))

    cl.login_by_sessionid(sid)
    try:
        cl.dump_settings(arq)
    except Exception:
        pass

    story = cl.photo_upload_to_story(
        Path(caminho),
        links=[StoryLink(webUri=link, x=x, y=y, width=largura, height=altura)],
    )
    return {"pk": str(story.pk), "conta": cl.username}
