"""
Simulação de comportamento humano para Playwright.
Todas as funções são async e aceitam uma page do Playwright.
"""
import asyncio
import random


async def delay(min_s: float = 0.6, max_s: float = 1.8):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def micro(min_s: float = 0.05, max_s: float = 0.18):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def think(min_s: float = 1.5, max_s: float = 4.0):
    """Pausa mais longa, como se o usuário estivesse pensando."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _move_bezier(page, x1: float, y1: float, x2: float, y2: float, steps: int = 14):
    """Move o mouse em curva de Bézier quadrática."""
    cpx = (x1 + x2) / 2 + random.uniform(-40, 40)
    cpy = (y1 + y2) / 2 + random.uniform(-40, 40)
    for i in range(1, steps + 1):
        t = i / steps
        nx = (1 - t) ** 2 * x1 + 2 * (1 - t) * t * cpx + t ** 2 * x2
        ny = (1 - t) ** 2 * y1 + 2 * (1 - t) * t * cpy + t ** 2 * y2
        await page.mouse.move(nx, ny)
        await asyncio.sleep(random.uniform(0.007, 0.022))


async def move_to(page, x: float, y: float):
    try:
        pos = await page.evaluate("() => [window._hx || 0, window._hy || 0]")
        await _move_bezier(page, pos[0], pos[1], x, y)
        await page.evaluate(f"() => {{ window._hx = {x}; window._hy = {y}; }}")
    except Exception:
        # Navegação no meio do movimento ou página fechada — ignora
        pass


async def _in_viewport(page, box) -> bool:
    """True se o centro do box está dentro da área visível atual."""
    try:
        vp = page.viewport_size or await page.evaluate(
            "() => ({width: innerWidth, height: innerHeight})")
        cy = box["y"] + box["height"] / 2
        cx = box["x"] + box["width"] / 2
        return 0 <= cx <= vp["width"] and 0 <= cy <= vp["height"]
    except Exception:
        return True


async def click(page, selector: str, timeout: int = 8000):
    """Localiza o elemento, garante que está visível na viewport, move o mouse e clica."""
    loc = page.locator(selector).first
    await loc.wait_for(state="visible", timeout=timeout)
    # Rola o elemento para dentro da viewport ANTES de calcular coordenadas —
    # senão o clique cai fora da tela (campo abaixo da dobra nunca recebe foco).
    try:
        await loc.scroll_into_view_if_needed(timeout=4000)
        await micro(0.1, 0.3)
    except Exception:
        pass
    box = await loc.bounding_box()
    if box and await _in_viewport(page, box):
        x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
        y = box["y"] + box["height"] * random.uniform(0.25, 0.75)
        await move_to(page, x, y)
        await micro(0.06, 0.18)
        await page.mouse.click(x, y)
    else:
        # Fallback robusto: clique nativo do Playwright (auto-scroll, auto-wait)
        await loc.click()


async def click_locator(page, loc):
    """Click em um locator já resolvido."""
    try:
        await loc.scroll_into_view_if_needed(timeout=4000)
        await micro(0.1, 0.3)
    except Exception:
        pass
    box = await loc.bounding_box()
    if box and await _in_viewport(page, box):
        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        await move_to(page, x, y)
        await micro()
        await page.mouse.click(x, y)
    else:
        await loc.click()


async def type_text(page, selector: str, text: str, wpm: int = 40, clear: bool = True):
    """
    Digita texto com velocidade humana (~40 WPM), variação por tecla
    e ocasionais erros de digitação seguidos de correção.
    """
    loc = page.locator(selector).first
    await loc.wait_for(state="visible", timeout=8000)
    await click(page, selector)
    await micro(0.1, 0.3)

    if clear:
        await page.keyboard.press("Control+a")
        await micro()
        await page.keyboard.press("Backspace")
        await micro(0.1, 0.2)

    base_ms = (60 / (wpm * 5)) * 1000  # ms por caractere

    i = 0
    while i < len(text):
        ch = text[i]

        # Erro de digitação ocasional (~3%)
        if random.random() < 0.03 and ch.isalpha() and i < len(text) - 1:
            wrong_offset = random.choice([-1, 1])
            wrong = chr(max(ord('a'), min(ord('z'), ord(ch.lower()) + wrong_offset)))
            if ch.isupper():
                wrong = wrong.upper()
            await page.keyboard.type(wrong)
            await asyncio.sleep(random.uniform(0.08, 0.25))
            await page.keyboard.press("Backspace")
            await asyncio.sleep(random.uniform(0.05, 0.12))

        await page.keyboard.type(ch)

        # Pausa de reflexão (~4%)
        if random.random() < 0.04:
            await asyncio.sleep(random.uniform(0.35, 1.1))
        else:
            jitter = random.gauss(0, base_ms * 0.25) / 1000
            await asyncio.sleep(max(0.025, base_ms / 1000 + jitter))

        i += 1


async def scroll_down(page, px: int = 400):
    steps = random.randint(3, 8)
    per = px // steps
    for _ in range(steps):
        await page.mouse.wheel(0, per + random.randint(-15, 15))
        await asyncio.sleep(random.uniform(0.07, 0.19))


async def scroll_up(page, px: int = 300):
    await scroll_down(page, -px)


async def idle_browse(page, duration_s: float = 60.0):
    """
    Navega de forma ociosa: scroll, pausas e pequenos movimentos de mouse.
    Usado para aquecer a conta após a criação.
    """
    loop = asyncio.get_event_loop()
    end = loop.time() + duration_s
    vp = page.viewport_size or {"width": 1280, "height": 720}
    w, h = vp["width"], vp["height"]

    while loop.time() < end:
        if page.is_closed():
            return
        action = random.choices(
            ["scroll_down", "scroll_up", "pause", "micro_move", "long_pause"],
            weights=[5, 1, 3, 2, 1],
        )[0]

        try:
            if action == "scroll_down":
                await scroll_down(page, random.randint(150, 550))
                await asyncio.sleep(random.uniform(0.4, 1.2))
            elif action == "scroll_up":
                await scroll_up(page, random.randint(100, 350))
                await asyncio.sleep(random.uniform(0.3, 0.9))
            elif action == "pause":
                await asyncio.sleep(random.uniform(1.2, 3.5))
            elif action == "micro_move":
                x = random.uniform(w * 0.1, w * 0.9)
                y = random.uniform(h * 0.2, h * 0.8)
                await move_to(page, x, y)
                await asyncio.sleep(random.uniform(0.3, 0.8))
            elif action == "long_pause":
                await asyncio.sleep(random.uniform(4.0, 9.0))
        except Exception:
            # Navegação/redirect ou página instável durante a ação — segue em frente
            await asyncio.sleep(0.3)
