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


def _texto_do_link(link: str, rotulo: str = "") -> str:
    """O que aparece escrito no chip.

    Padrão: só o DOMÍNIO. O sticker do Instagram nunca mostra a URL inteira, e
    caminho longo (`chat.whatsapp.com/HpTiqPpQmR3DRQLAkS2tME`) além de feio não
    cabe no chip — foi o que apareceu no primeiro teste com arte real.

    `rotulo` sobrepõe: contas reais escrevem "ENTRE NO GRUPO" em vez da URL, e
    isso converte melhor do que um endereço que ninguém lê.
    """
    if (rotulo or "").strip():
        r = rotulo.strip()
        return (r[:28] + "…") if len(r) > 29 else r
    t = (link or "").strip()
    for prefixo in ("https://", "http://"):
        if t.lower().startswith(prefixo):
            t = t[len(prefixo):]
    t = t.split("/")[0].split("?")[0]          # só o domínio
    return (t[:28] + "…") if len(t) > 29 else t or "link"


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


# ─── Repostar publicação de OUTRA pessoa ─────────────────────────────────────
#
# O Instagram chama isso de "compartilhar no story": o post de alguém vira um
# card tocável dentro do seu story, com o @ do autor. Na API é
# `medias=[StoryMedia(...)]`, que o payload manda como `type: feed_media` +
# `reshared_media_id`.
#
# ⚠ MESMA ARMADILHA DO CHIP DE LINK (medida em 28/08): o sticker do payload leva
# posição, tamanho e ids, e NENHUM campo de aparência. Quem desenha o card é o
# app na hora de compor. Pela API sobe uma imagem achatada, então o card tem que
# estar DESENHADO na arte — senão o post fica tocável e invisível, exatamente o
# que aconteceu com o primeiro story de link.
#
# ⚠ POST/REEL, não STORY de outro. A instagrapi tem sticker de `feed_media` e não
# tem nada para republicar o story alheio — no Instagram isso só existe quando a
# pessoa te menciona no story dela. Link de post (`/p/`, `/reel/`, `/tv/`) serve;
# link de story (`/stories/...`) não.


def resolver_post(cl, link: str) -> dict:
    """Descobre de quem é a publicação do link, e a miniatura para desenhar.

    `cl` é um Client da instagrapi já autenticado (o mesmo que publica) — a
    consulta é da API privada, então precisa de sessão.
    """
    alvo = (link or "").strip()
    if "/stories/" in alvo:
        raise ValueError("isso é link de STORY. Só dá para repostar publicação "
                         "(/p/, /reel/ ou /tv/) — story de outra pessoa o "
                         "Instagram só deixa republicar quando ela te menciona.")
    try:
        pk = cl.media_pk_from_url(alvo)
    except Exception as e:
        raise ValueError("não reconheci o link da publicação (%s)" % e)

    m = cl.media_info(pk)
    autor = getattr(m, "user", None)
    return {
        "media_pk": str(m.pk),
        "media_code": getattr(m, "code", "") or "",
        "user_id": str(getattr(autor, "pk", "") or ""),
        "username": getattr(autor, "username", "") or "",
        "thumbnail_url": str(getattr(m, "thumbnail_url", "") or ""),
        "media_type": getattr(m, "media_type", None),
        "legenda": (getattr(m, "caption_text", "") or "")[:120],
    }


def _baixar(url: str, limite_mb: int = 8) -> bytes:
    """Baixa a miniatura. Sai pela mesma conexão do PC (o modem), como o resto."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        dados = r.read(limite_mb * 1024 * 1024 + 1)
    if not dados:
        raise ValueError("miniatura veio vazia")
    return dados


def _fundo_do_post(mini: Image.Image) -> Image.Image:
    """Fundo 9:16 a partir da própria miniatura, borrada e escurecida.

    É o que o Instagram faz quando você compartilha um post sem escolher fundo —
    e evita exigir que você prepare uma arte só para repostar.
    """
    from PIL import ImageFilter
    escala = max(LARGURA / mini.width, ALTURA / mini.height)
    g = mini.convert("RGB").resize(
        (max(1, int(mini.width * escala)), max(1, int(mini.height * escala))), Image.LANCZOS)
    esq = (g.width - LARGURA) // 2
    topo = (g.height - ALTURA) // 2
    g = g.crop((esq, topo, esq + LARGURA, topo + ALTURA)).filter(ImageFilter.GaussianBlur(40))
    escuro = Image.new("RGB", (LARGURA, ALTURA), (0, 0, 0))
    return Image.blend(g, escuro, 0.45)


def _desenhar_card(img: Image.Image, mini: Image.Image, username: str,
                   cx: float, cy: float, cw: float, ch: float) -> None:
    """Desenha o card do post: miniatura recortada + barra com o @ do autor.

    O @ importa: repost sem crédito visível é o que faz o post parecer roubado, e
    o sticker de verdade sempre mostra o autor.
    """
    raio = max(10, int(min(cw, ch) * 0.06))
    x0, y0 = int(cx - cw / 2), int(cy - ch / 2)
    x1, y1 = int(cx + cw / 2), int(cy + ch / 2)

    # Máscara de cantos arredondados, para o card não sair quadrado sobre o fundo.
    cartao = Image.new("RGB", (x1 - x0, y1 - y0), (255, 255, 255))
    barra = max(28, int((y1 - y0) * 0.13))
    area = (x1 - x0, (y1 - y0) - barra)

    escala = max(area[0] / mini.width, area[1] / mini.height)
    foto = mini.convert("RGB").resize(
        (max(1, int(mini.width * escala)), max(1, int(mini.height * escala))), Image.LANCZOS)
    esq = (foto.width - area[0]) // 2
    topo = (foto.height - area[1]) // 2
    cartao.paste(foto.crop((esq, topo, esq + area[0], topo + area[1])), (0, 0))

    d2 = ImageDraw.Draw(cartao)
    texto = "@" + (username or "instagram")
    tam = max(14, int(barra * 0.46))
    fonte = _fonte(tam)
    while tam > 11 and d2.textbbox((0, 0), texto, font=fonte)[2] > area[0] * 0.9:
        tam = int(tam * 0.9)
        fonte = _fonte(tam)
    tx0, ty0, tx1, ty1 = d2.textbbox((0, 0), texto, font=fonte)
    d2.text(((area[0] - (tx1 - tx0)) / 2 - tx0,
             area[1] + (barra - (ty1 - ty0)) / 2 - ty0),
            texto, font=fonte, fill=(20, 20, 20))

    mascara = Image.new("L", cartao.size, 0)
    ImageDraw.Draw(mascara).rounded_rectangle([0, 0, cartao.size[0] - 1, cartao.size[1] - 1],
                                              radius=raio, fill=255)
    img.paste(cartao, (x0, y0), mascara)


def compor(dados: bytes, link: str, x: float, y: float,
           largura: float, altura: float, rotulo: str = "",
           post: dict = None, mini_bytes: bytes = None) -> dict:
    """Desenha os stickers na imagem e devolve o arquivo pronto para publicar.

    x/y são o CENTRO do retângulo, e largura/altura o tamanho — todos de 0 a 1,
    exatamente como o `StoryLink` e o `StoryMedia` da instagrapi os interpretam.
    Usar a mesma convenção nos dois lados é o que garante que o desenho fique
    embaixo da área que o Instagram torna tocável.

    Com `post`, o retângulo vira o CARD da publicação repostada e o chip do link
    (se houver) desce para logo abaixo dele — um retângulo só na tela, sem exigir
    que você posicione duas coisas.

    Sem `dados` mas com `post`, o fundo é a própria miniatura borrada: repostar
    não deveria exigir que você prepare uma arte.
    """
    cx, cy = float(x) * LARGURA, float(y) * ALTURA
    cw, ch = float(largura) * LARGURA, float(altura) * ALTURA

    mini = Image.open(io.BytesIO(mini_bytes)) if mini_bytes else None
    if dados:
        img = _encaixar(Image.open(io.BytesIO(dados)))
    elif mini is not None:
        img = _fundo_do_post(mini)
    else:
        raise ValueError("sem imagem de fundo e sem publicação para repostar")

    d = ImageDraw.Draw(img, "RGBA")

    if post and mini is not None:
        _desenhar_card(img, mini, post.get("username", ""), cx, cy, cw, ch)
        d = ImageDraw.Draw(img, "RGBA")
        if not (link or "").strip():
            PASTA.mkdir(parents=True, exist_ok=True)
            nome = "story_%d.jpg" % int(time.time() * 1000)
            caminho = PASTA / nome
            img.save(caminho, "JPEG", quality=92)
            return {"arquivo": nome, "caminho": str(caminho),
                    "largura_px": LARGURA, "altura_px": ALTURA}
        # O chip desce para debaixo do card, com folga — e as coordenadas do link
        # acompanham, senao o desenho e a area tocavel se separariam.
        ch = min(ch, ALTURA * 0.075)
        cw = min(cw, LARGURA * 0.62)
        cy = min(ALTURA - ch / 2 - 20, cy + (float(altura) * ALTURA) / 2 + ch * 0.9)

    caixa = [cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2]
    raio = max(8, int(min(cw, ch) * 0.28))

    # Chip claro com texto escuro, que é como o sticker de link aparece hoje.
    d.rounded_rectangle(caixa, radius=raio, fill=(255, 255, 255, 235))

    texto = _texto_do_link(link, rotulo)
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
    saida = {"arquivo": nome, "caminho": str(caminho),
             "largura_px": LARGURA, "altura_px": ALTURA}
    if post:
        # Onde o chip do link REALMENTE ficou, para o publicar mandar a area
        # tocavel no mesmo lugar do desenho.
        saida["link_rect"] = {"x": cx / LARGURA, "y": cy / ALTURA,
                              "largura": cw / LARGURA, "altura": ch / ALTURA}
    return saida


def cliente(perfil: dict):
    """Client da instagrapi autenticado pela sessão que o perfil JÁ tem.

    NÃO faz login: os cookies saem do perfil Camoufox (sem nenhuma requisição ao
    Instagram) e entram por `login_by_sessionid`. Era o login com usuário e senha
    que queimava conta em 26/07 — "não é a velocidade nem o cap, é o LOGIN".

    Virou função à parte porque resolver a publicação a repostar também precisa
    de sessão — é consulta da API privada, não do site público.
    """
    import asyncio
    import ig_session as igs
    from instagrapi import Client
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

    _calar_expose(cl)
    return cl


def _calar_expose(cl) -> None:
    """Desliga o `expose()` da instagrapi — o Instagram apagou esse endpoint.

    Medido em 13/10, publicando story de verdade:
    `ClientNotFoundError: 404 Client Error: Not Found for url:
    https://i.instagram.com/api/v1/qe/expose/`

    **O 404 acontece DEPOIS da publicação, não antes.** Em
    `instagrapi/mixins/photo.py:636` a ordem é:

        configured = self.photo_configure_to_story(...)
        if configured:
            self.expose()                       # <- estoura aqui
            return self._extract_configured_story_or_recent(...)

    Ou seja: a story já tinha sido configurada (`configured` veio verdadeiro) e
    a exceção derrubou só o RETORNO. Quem usa via o erro e republicava, gerando
    story duplicada — o erro mentia sobre o que tinha acontecido.

    `expose` não faz parte da publicação: ela só informa ao Instagram que o
    aparelho foi exposto a um experimento (`ig_android_profile_contextual_feed`).
    Não pular, e sim engolir a exceção, custaria um 404 na API privada a cada
    publicação — e 404 na API privada é justamente o sinal que este projeto
    trata como risco. Então não se chama.

    Só o NOSSO cliente é alterado. Remendar o pacote dentro do `venv/` sumiria
    no próximo `pip install` e não está versionado.
    """
    try:
        cl.expose = lambda: {}
    except Exception:
        pass


def publicar(perfil: dict, caminho: str, link: str,
             x: float, y: float, largura: float, altura: float,
             post: dict = None, link_rect: dict = None, cl=None) -> dict:
    """Publica o story: chip de link, card de repost, ou os dois.

    `link_rect` vem do `compor` quando há repost — é onde o chip foi DESENHADO,
    e é o que mandamos como área tocável. Sem isso o desenho ficaria embaixo do
    card e o toque em outro lugar.
    """
    from instagrapi.types import StoryLink, StoryMedia

    cl = cl or cliente(perfil)
    kw = {}

    if (link or "").strip():
        r = link_rect or {"x": x, "y": y, "largura": largura, "altura": altura}
        kw["links"] = [StoryLink(webUri=link, x=r["x"], y=r["y"],
                                 width=r["largura"], height=r["altura"])]
    if post:
        kw["medias"] = [StoryMedia(media_pk=int(post["media_pk"]),
                                   user_id=int(post["user_id"]) if post.get("user_id") else None,
                                   media_code=post.get("media_code") or None,
                                   x=x, y=y, width=largura, height=altura)]
    if not kw:
        raise ValueError("nada para publicar: sem link e sem publicação a repostar")

    story = cl.photo_upload_to_story(Path(caminho), **kw)
    return {"pk": str(story.pk), "conta": cl.username,
            "repostou": bool(post), "com_link": "links" in kw}
