"""
profile_warmup.py — aquecimento de PERFIL (não da conta).

Um navegador "zero km" (sem cookies, histórico ou cache) é sinal de bot. Antes de
usar o perfil no Instagram, este módulo navega por sites comuns com comportamento
humano, acumulando cookies persistentes, localStorage, cache e histórico — deixando
o perfil com cara de navegador usado por uma pessoa real.

Como o contexto é persistente (user_data_dir por perfil), tudo isso fica salvo e
estará presente quando o perfil abrir o Instagram pela primeira vez.
"""
import asyncio
import logging
import random

import human

logger = logging.getLogger(__name__)

# Sites populares, ricos em cookies e tolerantes a navegação (baixo bloqueio de bot).
# Wikipedia Random gera histórico orgânico e variado a cada visita.
_SITES = [
    "https://en.wikipedia.org/wiki/Special:Random",
    "https://www.youtube.com/",
    "https://www.bbc.com/news",
    "https://www.imdb.com/",
    "https://weather.com/",
    "https://www.wikihow.com/Main-Page",
    "https://www.nationalgeographic.com/",
    "https://stackoverflow.com/questions",
    "https://www.reddit.com/",
    "https://dictionary.cambridge.org/",
    "https://www.allrecipes.com/",
]

# Sites por idioma para coerência com o geoip (acrescentados conforme o locale do perfil)
_LOCALE_SITES = {
    "pt": ["https://www.uol.com.br/", "https://g1.globo.com/", "https://www.wikipedia.org/"],
    "es": ["https://www.elpais.com/", "https://www.marca.com/"],
    "fr": ["https://www.lemonde.fr/", "https://www.leboncoin.fr/"],
    "de": ["https://www.spiegel.de/", "https://www.bild.de/"],
    "ja": ["https://www.yahoo.co.jp/", "https://news.yahoo.co.jp/"],
}

_COOKIE_ACCEPT = [
    'button:has-text("Accept all")', 'button:has-text("Aceitar todos")',
    'button:has-text("Accept")', 'button:has-text("I agree")',
    'button:has-text("Aceitar")', 'button:has-text("Concordo")',
    'button:has-text("Got it")', 'button:has-text("Entendi")',
    '[aria-label="Accept all"]', '#onetrust-accept-btn-handler',
]


async def _accept_cookies(page):
    for sel in _COOKIE_ACCEPT:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                await human.click_locator(page, loc)
                await human.delay(0.6, 1.4)
                return
        except Exception:
            pass


async def _click_internal_link(page):
    """Clica num link interno aleatório para aprofundar a navegação (mais histórico)."""
    try:
        links = page.locator("a[href]")
        n = await links.count()
        if not n:
            return
        for _ in range(4):  # tenta achar um link visível
            idx = random.randint(0, min(n - 1, 25))
            loc = links.nth(idx)
            try:
                if await loc.is_visible(timeout=800):
                    await human.click_locator(page, loc)
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    return
            except Exception:
                continue
    except Exception:
        pass


def _sites_for(locale: str) -> list:
    sites = list(_SITES)
    pref = (locale or "").split("-")[0].lower()
    extra = _LOCALE_SITES.get(pref)
    if extra:
        sites = extra + sites
    random.shuffle(sites)
    return sites


async def browse(page, minutes: float = 10.0, locale: str = "") -> dict:
    """
    Navega por sites comuns por ~`minutes` minutos, com scroll, pausas e cliques,
    aceitando banners de cookie (para realmente persistir cookies).
    """
    loop = asyncio.get_event_loop()
    end = loop.time() + minutes * 60
    sites = _sites_for(locale)
    visited = 0
    i = 0
    consecutive_fail = 0

    logger.info(f"profile warmup iniciado: ~{minutes} min")
    while loop.time() < end:
        if page.is_closed():
            logger.info("warmup: página fechada — encerrando")
            break
        if consecutive_fail >= 3:
            logger.info("warmup: 3 falhas seguidas (navegador instável) — encerrando")
            break
        url = sites[i % len(sites)]
        i += 1
        try:
            logger.info(f"warmup: visitando {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await human.delay(2.0, 4.0)
            await _accept_cookies(page)

            # Permanece e rola a página como um leitor
            await human.idle_browse(page, random.uniform(20, 55))

            # Às vezes aprofunda em um link interno
            if random.random() < 0.5 and loop.time() < end:
                await _click_internal_link(page)
                await human.delay(1.5, 3.0)
                await human.idle_browse(page, random.uniform(15, 40))

            visited += 1
            consecutive_fail = 0
        except Exception as e:
            # Um site problemático (crash de renderer, timeout, redirect) NÃO pode
            # derrubar o aquecimento inteiro. Registra e segue para o próximo.
            logger.info(f"warmup: pulando {url}: {e}")
            consecutive_fail += 1
            await asyncio.sleep(0.5)
            continue

    logger.info(f"profile warmup concluído: {visited} site(s) visitado(s)")
    return {"visited": visited, "minutes": minutes}
