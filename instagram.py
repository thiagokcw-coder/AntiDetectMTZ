"""
Fluxo de criação e aquecimento de contas Instagram.

Etapas:
  1. Preenche o formulário de cadastro com comportamento humano
  2. Aguarda verificação de email/telefone (usuário faz manualmente no browser)
  3. Aquece a conta navegando pelo feed

Uso via API:
  POST /instagram/create-account   → inicia o fluxo
  GET  /instagram/status/{id}      → verifica etapa atual
  POST /instagram/warmup/{id}      → inicia aquecimento manual
"""

import asyncio
import datetime
import logging
import platform
import random
import re
import string
import sys
import time
import unicodedata
from typing import Optional

from pathlib import Path

import human
import accounts
import emails as email_box
from sms_activate import SmsActivate, SmsActivateError, COUNTRIES

try:
    from paths import LOGS_DIR
except Exception:  # fallback defensivo se paths mudar
    LOGS_DIR = Path(__file__).parent

logger = logging.getLogger(__name__)

# =============================================================================
# VERSIONAMENTO DA LÓGICA DE DETECÇÃO E CADASTRO (para fallback quando o IG muda)
# =============================================================================
#
# DETECTION_VERSION indica qual "versão" das regras de _resolve_fields e detect_status
# estamos usando atualmente. Quando o Instagram quebra o fluxo, subimos a versão e
# mantemos as regras antigas como fallback automático.
#
# "crie um fallback a partir daqui" — sistema implementado abaixo.
DETECTION_VERSION = "v2"

# Padrões da versão atual (v2) — mais completos e com fallbacks internos
_STATUS_PATTERNS_V2 = {
    "banned": [
        "account has been disabled", "conta foi desativada", "conta foi desabilitada",
        "tu cuenta fue inhabilitada", "we disabled your account", "account disabled"
    ],
    "suspended": [
        "we suspended your account", "suspendemos sua conta", "conta suspensa",
        "hemos suspendido tu cuenta", "account suspended", "suspended your account"
    ],
    "checkpoint": [
        "confirm it's you", "confirme que é você", "atividade incomum",
        "unusual activity", "help us confirm", "verifique sua conta",
        "we detected", "suspicious", "confirma que eres tú", "confirm your identity",
        "verificação de segurança", "security check"
    ],
}

# Padrões da versão anterior (v1) — mantidos como fallback de emergência
_STATUS_PATTERNS_V1 = {
    "banned": [
        "account has been disabled", "conta foi desativada", "conta foi desabilitada",
        "tu cuenta fue inhabilitada", "we disabled your account"
    ],
    "suspended": [
        "we suspended your account", "suspendemos sua conta", "conta suspensa",
        "hemos suspendido tu cuenta", "account suspended"
    ],
    "checkpoint": [
        "confirm it's you", "confirme que é você", "atividade incomum",
        "unusual activity", "help us confirm", "verifique sua conta",
        "we detected", "suspicious", "confirma que eres tú"
    ],
}

def _get_status_patterns(version: str = None):
    """Retorna os padrões de texto para uma versão específica."""
    v = version or DETECTION_VERSION
    if v == "v2":
        return _STATUS_PATTERNS_V2
    if v == "v1":
        return _STATUS_PATTERNS_V1
    return _STATUS_PATTERNS_V2  # default

# =============================================================================
# Estado dos fluxos ativos ──────────────────────────────────────────────────
_flows: dict[str, dict] = {}
# { profile_id: { status, username, email, step, error, started_at, account_id } }

stop_requested = False

def request_creation_stop():
    """Sinaliza para interromper criações de contas em andamento."""
    global stop_requested
    stop_requested = True

# Mapeia o status do FLUXO de criação para o status persistido da CONTA.
_ACCOUNT_STATUS = {
    "starting":                    ("creating", "Cadastro iniciado"),
    "created":                     ("created", "Cadastro concluído"),
    "completed":                   ("created", "Cadastro concluído"),
    "waiting_for_verification":    ("waiting_verification", "Aguardando verificação"),
    "waiting_for_email_code":      ("waiting_verification", "Aguardando código por email"),
    "code_filled_manually_needed": ("waiting_verification", "Código preenchido — confirme manualmente"),
    # Pediu telefone/WhatsApp JÁ NA CRIAÇÃO → conta nasce muito sinalizada (alto risco de ban).
    # Descartada de propósito. Status próprio "discarded" (NÃO conta como ban real).
    # OBS: verificação que aparece DEPOIS (pós-criação) é "checkpoint" — recuperável, tratada à parte.
    "flagged_phone":               ("discarded", "Descartada: pediu telefone/WhatsApp na criação"),
    "cancelled":                   ("error", "Cancelado"),
    "error":                       ("error", "Erro no cadastro"),
}


def flow_status(profile_id: str) -> Optional[dict]:
    return _flows.get(profile_id)


def _sync_account(profile_id: str, flow_status_value: str, detail: str = ""):
    """Reflete o status do fluxo na linha persistida da conta, se houver."""
    acc_id = (_flows.get(profile_id) or {}).get("account_id")
    if not acc_id:
        return
    mapped = _ACCOUNT_STATUS.get(flow_status_value)
    if not mapped:
        return
    status, default_detail = mapped
    try:
        accounts.set_status(acc_id, status, detail or default_detail)
    except Exception as e:
        logger.warning(f"falha ao sincronizar conta: {e}")


def _random_suffix(n: int = 4) -> str:
    return "".join(random.choices(string.digits, k=n))


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "")
    s = re.sub(r"[^a-z0-9]", "", s)
    return s[:20] or "user"


def _gen_username(full_name: str) -> str:
    """Gera um username plausível.
    Prefere nomes mais curtos e "humanos" (com 2-4 dígitos ou sufixo comum)
    para aumentar a chance de o IG aceitar ou oferecer sugestão boa.
    """
    base = _slugify(full_name)[:18] or "user"
    # 60% chance de usar poucos dígitos (mais realista)
    if random.random() < 0.6:
        suffix = _random_suffix(random.randint(2, 4))
        sep = "_" if random.random() < 0.25 else ""
        cand = f"{base}{sep}{suffix}"
    else:
        # adiciona um sufixo "BR" / comum para parecer conta real
        extras = ["br", "ofc", "real", "oficial", "lima", "santos", "silva", "rio"]
        extra = random.choice(extras)
        digits = _random_suffix(2) if random.random() < 0.5 else ""
        cand = f"{base}{extra}{digits}"
    return cand[:30]


# Geração automática de dados de conta (nomes BR realistas + senha forte)
_FIRST_NAMES = [
    "Lucas", "Ana", "Pedro", "Marina", "Gabriel", "Julia", "Mateus", "Beatriz",
    "Rafael", "Larissa", "Bruno", "Carolina", "Felipe", "Amanda", "Thiago",
    "Leticia", "Gustavo", "Camila", "Diego", "Sofia", "Vinicius", "Isabela",
    "Rodrigo", "Mariana", "Andre", "Fernanda", "Leonardo", "Juliana", "Daniel", "Patricia",
]
_LAST_NAMES = [
    "Silva", "Souza", "Oliveira", "Santos", "Pereira", "Almeida", "Ferreira",
    "Costa", "Rodrigues", "Gomes", "Martins", "Araujo", "Ribeiro", "Carvalho",
    "Lima", "Barbosa", "Rocha", "Dias", "Nascimento", "Moreira", "Cardoso", "Teixeira",
]
_PWD_WORDS = ["Sol", "Mar", "Rio", "Lua", "Vento", "Fogo", "Pedra", "Onda",
              "Forte", "Livre", "Azul", "Verde", "Norte", "Sul", "Praia"]


def gen_full_name(base: Optional[str] = None) -> str:
    """Gera nome completo. Se 'base' for passado (ex.: local do email ou identificador do usuário),
    tenta derivar um nome relacionado (ex.: 'joao.silva' -> 'João Silva'). Caso contrário, aleatório realista BR."""
    if base:
        # Deriva um primeiro nome a partir do base (email) SEM herdar dígitos.
        # Ex.: 'gabriel56255' -> 'Gabriel'. Mantém a coerência natural email↔nome,
        # mas remove os números colados que faziam o username parecer gerado por máquina
        # (ex. antigo ruim: 'gabriel56255san6289'). Agora vira algo como 'gabriel.santos8472'.
        clean = re.sub(r'[^a-zA-Z]', ' ', str(base)).strip()
        parts = [p for p in clean.split() if len(p) >= 2]
        if parts:
            first = parts[0].capitalize()
            last = parts[1].capitalize() if len(parts) > 1 else random.choice(_LAST_NAMES)
            return f"{first} {last}"
    return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"


def gen_password() -> str:
    """Senha forte sem acentos/espaços (compatível com formulários)."""
    word = random.choice(_PWD_WORDS)
    digits = "".join(random.choices(string.digits, k=4))
    symbol = random.choice("!@#$%")
    tail = "".join(random.choices(string.ascii_lowercase, k=2))
    return f"{word}{digits}{symbol}{tail}"


def gen_birthday(min_age: int = 18, max_age: int = 38) -> dict:
    """Gera {month, day, year} realista para contas (evita idades muito jovens ou antigas).
    Padrão 18-38 anos para parecer conta real criada por humano adulto.
    """
    today = datetime.date.today()
    age = random.randint(min_age, max_age)
    year = today.year - age
    month = random.randint(1, 12)
    if month in (1, 3, 5, 7, 8, 10, 12):
        day = random.randint(1, 31)
    elif month == 2:
        day = random.randint(1, 28)
    else:
        day = random.randint(1, 30)
    return {"month": month, "day": day, "year": year}


# ── Seletores ─────────────────────────────────────────────────────────────────
# O Instagram REMOVEU os atributos name= dos campos e passou a usar IDs dinâmicos
# (ex.: #_r_8_) gerados pelo React, que mudam a cada carga. Os labels são
# localizados (mudam com o idioma do geoip). O que é estável e independente de
# idioma é o TYPE do input:
#   - email      → 1º input[type="text"]
#   - full_name  → 2º input[type="text"]
#   - username   → input[type="search"]  (campo com checagem de disponibilidade)
#   - password   → input[type="password"]
# Resolvemos os IDs reais em runtime (estáveis durante o preenchimento). Data de
# nascimento e botão de envio são tratados por helpers dedicados (ver abaixo).


async def _goto_retry(page, url: str, attempts: int = 3) -> bool:
    """Navega com algumas tentativas — tolera hiccups de proxy/rede sem abortar tudo.
    
    Usa estratégia mais conservadora (sem wait_until rígido + espera explícita) 
    para reduzir crashes internos no Camoufox ao abrir páginas pesadas como Instagram.
    Esta solução é no nível de navegação, independente de injeção de JS, addons ou filler.
    """
    last = None
    for i in range(attempts):
        try:
            # Estratégia independente e mais estável para sites pesados como IG:
            # - Sem wait_until agressivo no goto (domcontentloaded pode disparar cedo demais
            #   e causar race com scripts pesados do IG + stealth patches do Camoufox).
            # - Depois do goto, esperamos "load" + um pouco de estabilização + elemento básico.
            await page.goto(url, timeout=60000)
            try:
                await page.wait_for_load_state("load", timeout=25000)
            except Exception:
                pass  # load pode demorar; continuamos com o que temos

            # Dá tempo pro IG carregar seus chunks pesados sem forçar nada
            await human.delay(1.8, 3.5)

            # Espera por algo concreto da página (melhor que depender só de estado de load)
            try:
                await page.locator('body, form, input').first.wait_for(state="visible", timeout=15000)
            except Exception:
                pass

            return True
        except Exception as e:
            last = e
            err = str(e).lower()
            logger.warning(f"goto {url} tentativa {i+1}/{attempts} falhou: {e}")
            if page.is_closed() or any(x in err for x in ["closed", "target", "context", "crashed", "disconnected", "browser"]):
                # Erro fatal de contexto — não vale a pena retry cego aqui
                break
            await human.delay(2.5, 5.0)
    if last:
        raise last
    return False


async def _type_and_verify(page, selector: str, text: str, label: str = "") -> bool:
    """Digita e CONFERE se o valor ficou no campo (evita cadastro com campo vazio).
    Tenta de novo uma vez se não colou."""
    for attempt in range(2):
        try:
            await human.type_text(page, selector, text)
        except Exception as e:
            logger.warning(f"digitar {label or selector}: {e}")
        await human.delay(0.3, 0.7)
        try:
            val = (await page.locator(selector).first.input_value()).strip()
        except Exception:
            val = ""
        if val:
            return True
        logger.warning(f"campo {label or selector} ficou vazio (tentativa {attempt+1}) — refazendo")
        await human.delay(0.4, 0.9)
    return False


async def _resolve_fields(page) -> dict:
    """
    Mapeia os campos do cadastro de forma independente de idioma + com fallbacks.

    Estratégia:
    - Preferimos seletores por tipo + posição (mais estáveis que IDs do React).
    - Se o layout mudar (IG costuma adicionar/remover campos), tentamos heurísticas extras.
    - Retornamos seletores que o Playwright reavalia em tempo real.
    """
    try:
        await page.locator('input[type="password"]').first.wait_for(
            state="visible", timeout=15000
        )
    except Exception:
        # Fallback: às vezes o password aparece depois de preencher o primeiro campo
        await human.delay(1.5, 3.0)

    text_like = ('input:not([type="hidden"]):not([type="password"]):not([type="checkbox"])'
                 ':not([type="radio"]):not([type="submit"]):not([type="button"])')
    n_text   = await page.locator(text_like).count()
    n_search = await page.locator('input[type="search"]').count()

    result = {
        "email":     f'{text_like} >> nth=0',
        "full_name": f'{text_like} >> nth=1' if n_text > 1 else "",
        "username":  'input[type="search"] >> nth=0' if n_search
                     else f'{text_like} >> nth=2',
        "password":  'input[type="password"] >> nth=0',
    }

    # Fallback extra: se não achou search, tenta achar o campo de username por placeholder ou name
    if not n_search:
        try:
            # Muitas vezes o terceiro text ou um com aria-label/role específico é o username
            username_candidates = [
                'input[aria-label*="nome de usuário" i]',
                'input[aria-label*="username" i]',
                'input[name*="username" i]',
                'input[placeholder*="nome de usuário" i]',
            ]
            for sel in username_candidates:
                if await page.locator(sel).count() > 0:
                    result["username"] = sel
                    break
        except Exception:
            pass

    return result


async def _click_signup(page) -> bool:
    """
    Clica no botão de cadastro. O IG usa um <div>/<button> com texto localizado
    ("Sign up", "Inscreva-se", "Cadastre-se", "Criar conta", "Enviar"...).
    Tentativas múltiplas + seletores amplos para resistir a mudanças no React/IG.
    """
    good_text = re.compile(
        r"(enviar|submit|sign\s*up|inscrev|cadastre|cadastrar|criar\s*conta|"
        r"registrar|registrarse|next|avançar|avancar|continue|continuar|join|get\s*started)",
        re.I,
    )
    bad_text = re.compile(
        r"(entrar|log\s*in|login|already|já tenho|ja tenho|tenho uma conta|"
        r"voltar|back|cancel|cancelar|cookies?|privacy|privacidade|terms|termos|help|ajuda)",
        re.I,
    )

    async def usable(loc) -> bool:
        try:
            if not await loc.is_visible(timeout=900):
                return False
            if not await loc.is_enabled(timeout=900):
                return False
        except Exception:
            return False
        try:
            txt = ((await loc.inner_text(timeout=900)) or "").strip()
        except Exception:
            txt = ""
        if txt and bad_text.search(txt):
            return False
        return True

    async def strong_click(loc) -> bool:
        try:
            await loc.scroll_into_view_if_needed(timeout=2500)
        except Exception:
            pass
        for method in ("native", "human", "js"):
            try:
                if method == "native":
                    await loc.click(timeout=3500)
                elif method == "human":
                    await human.click_locator(page, loc)
                else:
                    await loc.evaluate("""el => {
                        el.scrollIntoView({block: 'center', inline: 'center'});
                        for (const type of ['pointerover','pointerenter','mouseover','mouseenter','pointerdown','mousedown','pointerup','mouseup','click']) {
                          el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                        }
                    }""", timeout=2500)
                return True
            except Exception:
                continue
        return False

    candidates = [
        'button[type="submit"]',
        'button:has-text("Sign up")',
        'div[role="button"]:has-text("Sign up")',
        'button:has-text("Inscreva-se")',
        'div[role="button"]:has-text("Inscreva-se")',
        'button:has-text("Criar conta")',
        'div[role="button"]:has-text("Criar conta")',
        'button:has-text("Cadastre-se")',
        'button:has-text("Cadastrar")',
        'div[role="button"]:has-text("Cadastrar")',
        'button:has-text("Join")',
        'button:has-text("Get started")',
        'button:has-text("Registrarse")',
        'div[role="button"]:has-text("Registrarse")',
        'button:has-text("Enviar")',
        'div[role="button"]:has-text("Enviar")',
        'button:has-text("Next")',
        'div[role="button"]:has-text("Next")',
        'button:has-text("Avançar")',
        'div[role="button"]:has-text("Avançar")',
        'button:has-text("Continuar")',
        'div[role="button"]:has-text("Continuar")',
        # Fallback controlado: botões dentro do form, sem clicar em login/links legais.
        'form button',
        'button[class*="blue"], button[class*="primary"], [role="button"][class*="blue"]',
    ]
    for sel in candidates:
        try:
            locs = page.locator(sel)
            count = min(await locs.count(), 8)
            for i in range(count):
                loc = locs.nth(i)
                if not await usable(loc):
                    continue
                if await strong_click(loc):
                    logger.info(f"submit: clique via seletor {sel} (idx={i})")
                    return True
        except Exception:
            pass

    # Último recurso: tentar clicar no primeiro botão visível que pareça de ação primária
    try:
        locs = page.locator('button, div[role="button"], input[type="submit"]')
        count = min(await locs.count(), 20)
        for i in range(count):
            loc = locs.nth(i)
            if not await usable(loc):
                continue
            try:
                txt = ((await loc.inner_text(timeout=700)) or "").strip()
            except Exception:
                txt = ""
            if txt and good_text.search(txt) and await strong_click(loc):
                logger.info(f"submit: clique por texto primário '{txt[:40]}'")
                return True
    except Exception:
        pass

    # Fallback DOM: quando Playwright acha o botão mas o clique não dispara o React.
    try:
        res = await page.evaluate(r"""() => {
            const norm = t => (t || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().trim();
            const good = /(enviar|submit|sign\s*up|inscrev|cadastre|cadastrar|criar\s*conta|registrar|registrarse|next|avancar|continue|continuar|join|get\s*started)/i;
            const bad = /(entrar|log\s*in|login|already|ja tenho|tenho uma conta|voltar|back|cancel|cookies?|privacy|privacidade|terms|termos|help|ajuda)/i;
            const visible = el => {
              const r = el.getBoundingClientRect();
              const cs = getComputedStyle(el);
              return r.width > 5 && r.height > 5 && cs.visibility !== 'hidden' && cs.display !== 'none' && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
            };
            const inputs = [...document.querySelectorAll('input')].filter(visible);
            const form = inputs[0] ? inputs[0].closest('form') : null;
            const candidates = [...document.querySelectorAll('button, [role="button"], input[type="submit"]')]
              .filter(visible)
              .map(el => {
                const text = norm(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '');
                let score = 0;
                if (form && form.contains(el)) score += 40;
                if (el.tagName === 'BUTTON') score += 8;
                if ((el.getAttribute('type') || '').toLowerCase() === 'submit') score += 25;
                if (good.test(text)) score += 60;
                if (bad.test(text)) score -= 120;
                const r = el.getBoundingClientRect();
                score += Math.min(20, Math.max(0, r.top / 40));
                return {el, text, score};
              })
              .filter(x => x.score > 20)
              .sort((a, b) => b.score - a.score);
            const pick = candidates[0];
            if (!pick) return {ok: false};
            pick.el.scrollIntoView({block: 'center', inline: 'center'});
            for (const type of ['pointerover','pointerenter','mouseover','mouseenter','pointerdown','mousedown','pointerup','mouseup','click']) {
              pick.el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
            }
            try { pick.el.click(); } catch (_) {}
            return {ok: true, text: pick.text, score: pick.score};
        }""")
        if res and res.get("ok"):
            logger.info(f"submit: clique fallback DOM text='{res.get('text')}' score={res.get('score')}")
            return True
    except Exception:
        pass

    return False


async def _flush_form_events(page):
    """Garante que React/IG reconheça os valores antes do submit."""
    try:
        await page.evaluate(r"""() => {
            for (const el of document.querySelectorAll('input, textarea, select')) {
              for (const type of ['input', 'change']) {
                el.dispatchEvent(new Event(type, {bubbles: true, cancelable: true}));
              }
            }
            if (document.activeElement && document.activeElement.blur) {
              document.activeElement.blur();
            }
        }""")
    except Exception:
        pass


async def _signup_visible_problem(page) -> str:
    """Captura mensagens visíveis de validação quando o submit não avança."""
    try:
        return await page.evaluate(r"""() => {
            const norm = t => (t || '').replace(/\s+/g, ' ').trim();
            const needles = /(obrigat|required|invalid|inválid|invalido|erro|error|dispon|available|taken|senha|password|email|nascimento|birthday|tente|try)/i;
            const nodes = [...document.querySelectorAll('[role="alert"], [aria-live], span, div, p')]
              .filter(el => {
                const r = el.getBoundingClientRect();
                const cs = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
              })
              .map(el => norm(el.innerText || el.textContent || ''))
              .filter(t => t && t.length < 220 && needles.test(t));
            return [...new Set(nodes)].slice(0, 3).join(' | ');
        }""") or ""
    except Exception:
        return ""


async def _submit_trusted(page) -> bool:
    """Clica no botão de envio do cadastro com clique REAL (trusted) do Playwright.

    O IG costuma ignorar clique disparado por JS (isTrusted=false), o que fazia o
    formulário "enviar mas não avançar". Aqui localizamos o melhor botão de submit
    no contexto da página e clicamos com o mouse real do Playwright (ElementHandle.click),
    que dispara um gesto confiável aceito pelo Instagram.
    """
    try:
        handle = await page.evaluate_handle(r"""() => {
            const norm = t => (t || '').replace(/\s+/g, ' ').trim().toLowerCase();
            const good = /(submit|enviar|sign\s*up|inscrev|cadastre|cadastrar|criar\s*conta|registrar|registrarse|next|avancar|avançar|continue|continuar|join|get\s*started|concluir)/i;
            const bad = /(entrar|log\s*in|login|already|ja tenho|tenho uma conta|voltar|back|cancel|cookies?|privacy|privacidade|terms|termos|help|ajuda)/i;
            const visible = el => {
                const r = el.getBoundingClientRect();
                const cs = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
            };
            const cands = [...document.querySelectorAll('button, [role="button"], input[type="submit"]')].filter(visible);
            let best = null, bestScore = 20;
            for (const el of cands) {
                const text = norm(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '');
                let score = 0;
                if (el.tagName === 'BUTTON') score += 10;
                if (norm(el.getAttribute('type')) === 'submit') score += 35;
                if (good.test(text)) score += 70;
                if (bad.test(text)) score -= 160;
                if (el.disabled || el.getAttribute('aria-disabled') === 'true') score -= 200;
                if (score > bestScore) { bestScore = score; best = el; }
            }
            return best;
        }""")
        element = handle.as_element() if handle else None
        if not element:
            logger.warning("_submit_trusted: botão de envio não encontrado")
            return False
        try:
            await element.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        await human.delay(0.1, 0.3)
        # Clique real do Playwright (gesto confiável). force=False mantém checagem de actionability.
        await element.click(timeout=6000)
        return True
    except Exception as e:
        logger.warning(f"_submit_trusted falhou: {e}")
        return False


async def _signup_field_handle(page, role: str):
    """ElementHandle do campo do cadastro (contact|password|fullName), com a MESMA lógica
    de detecção do _page_fill — para digitarmos com teclado real em vez de injetar valor."""
    try:
        handle = await page.evaluate_handle(r"""(role) => {
            const norm = t => (t||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase().trim();
            const visible = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
              return r.width>0 && r.height>0 && cs.visibility!=='hidden' && cs.display!=='none'; };
            const byPos = (a,b) => { const ar=a.getBoundingClientRect(), br=b.getBoundingClientRect(); return ar.top-br.top || ar.left-br.left; };
            const meta = el => norm([el.type, el.name, el.id, el.autocomplete, el.inputMode, el.getAttribute('aria-label'), el.getAttribute('placeholder')].join(' '));
            const looksUser = el => norm(el.type)==='search' || /user|usuario|utilisateur|benutzer|username|handle|login/.test(meta(el));
            const texts = [...document.querySelectorAll('input, textarea')].filter(visible)
              .filter(el => !['hidden','password','checkbox','radio','submit','button'].includes(norm(el.getAttribute('type')||'text')))
              .sort(byPos);
            if (role === 'password') {
              return [...document.querySelectorAll('input[type="password"]')].filter(visible).sort(byPos)[0] || null;
            }
            const contact = texts.find(i=>!looksUser(i) && /(email|mail|phone|tel|mobile|celular|telefone|fono|telefono)/.test(meta(i))) || texts.find(i=>!looksUser(i)) || texts[0] || null;
            if (role === 'contact') return contact;
            if (role === 'fullName') {
              return texts.find(i=>i!==contact && !looksUser(i) && /name|nome|nombre|nom|vorname|nachname/.test(meta(i)))
                || texts.find(i=>i!==contact && !looksUser(i)) || null;
            }
            return null;
        }""", role)
        return handle.as_element() if handle else None
    except Exception as e:
        logger.warning(f"_signup_field_handle({role}) falhou: {e}")
        return None


async def _type_like_human(page, handle, text: str) -> bool:
    """Foca o campo (clique real), limpa e digita com TECLADO real + ritmo humano.
    Remove o sinal de 'campo mudou de valor sem ninguém digitar' (injeção JS)."""
    if not handle or not text:
        return False
    try:
        await handle.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        await handle.click(timeout=4000)
    except Exception:
        try:
            await handle.focus()
        except Exception:
            return False
    try:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
    except Exception:
        pass
    for ch in text:
        try:
            await page.keyboard.type(ch)
        except Exception:
            return False
        await asyncio.sleep(random.uniform(0.05, 0.17))
        if random.random() < 0.04:          # pausa de "reflexão" ocasional
            await asyncio.sleep(random.uniform(0.3, 0.9))
    try:
        await handle.evaluate("el => { el.dispatchEvent(new Event('change', {bubbles:true})); if (el.blur) el.blur(); }")
    except Exception:
        pass
    return True


async def _fill_text_fields_trusted(page, contact: str, password: str, full_name: str) -> int:
    """Preenche email/senha/nome com digitação REAL (teclado). Retorna quantos preencheu."""
    filled = 0
    for role, value in (("contact", contact), ("password", password), ("fullName", full_name)):
        if not value:
            continue
        handle = await _signup_field_handle(page, role)
        if await _type_like_human(page, handle, value):
            filled += 1
        else:
            logger.warning(f"digitação real: não consegui preencher '{role}'")
        await asyncio.sleep(random.uniform(0.2, 0.6))
    return filled


def _play_alert_sound():
    """Toca um bipe de alerta. Best-effort por SO; silencioso se nada funcionar.
    Windows: winsound. macOS: afplay (som do sistema). Linux: bell do terminal."""
    system = platform.system()
    try:
        if system == "Windows":
            import winsound
            for _ in range(3):
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                time.sleep(0.25)
        elif system == "Darwin":
            import subprocess
            for _ in range(3):
                subprocess.run(
                    ["afplay", "/System/Library/Sounds/Glass.aiff"],
                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                time.sleep(0.1)
        else:
            for _ in range(3):
                sys.stdout.write("\a")
                sys.stdout.flush()
                time.sleep(0.25)
    except Exception:
        pass


def _win_focus_browser():
    """Windows: restaura (se minimizada) e traz pra frente a janela do Camoufox
    (classe 'MozillaWindowClass'). Best-effort. Silencioso fora do Windows."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32  # AttributeError fora do Windows → ignorado
        SW_RESTORE = 9
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                buf = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, buf, 64)
                if buf.value == "MozillaWindowClass":
                    found.append(hwnd)
            except Exception:
                pass
            return True

        user32.EnumWindows(_cb, 0)
        for hwnd in found:
            try:
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
    except Exception:
        pass


def _mac_focus_browser():
    """macOS: traz a janela do Camoufox/Firefox pra frente via AppleScript.
    Best-effort — tenta nomes de processo conhecidos. Silencioso em falha."""
    try:
        import subprocess
        # O processo do Camoufox pode aparecer como 'camoufox' ou 'firefox'.
        script = (
            'tell application "System Events"\n'
            '  repeat with pname in {"camoufox", "Camoufox", "firefox", "Firefox"}\n'
            '    repeat with p in (every process whose name contains pname)\n'
            '      try\n'
            '        set frontmost of p to true\n'
            '      end try\n'
            '    end repeat\n'
            '  end repeat\n'
            'end tell'
        )
        subprocess.run(
            ["osascript", "-e", script],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _focus_browser_window():
    """Restaura/foca a janela do navegador antidetect conforme o SO. Best-effort."""
    system = platform.system()
    if system == "Windows":
        _win_focus_browser()
    elif system == "Darwin":
        _mac_focus_browser()


async def _alert_captcha(page, profile_id: str, bring_front: bool = True):
    """Chama a atenção do usuário para resolver o captcha: traz a janela pra frente + som.
    Pensado para o usuário trabalhar em outra coisa e só ser interrompido aqui."""
    if bring_front:
        try:
            await page.bring_to_front()
        except Exception:
            pass
        try:
            # Restaura/foca a janela do Camoufox conforme o SO (Windows/macOS).
            await asyncio.get_event_loop().run_in_executor(None, _focus_browser_window)
        except Exception:
            pass
    try:
        await asyncio.get_event_loop().run_in_executor(None, _play_alert_sound)
    except Exception:
        pass
    logger.warning(f"signup [{profile_id}]: >>> AÇÃO MANUAL NECESSÁRIA — resolva o reCAPTCHA na janela <<<")


async def _is_captcha_screen(page) -> bool:
    """Detecta a tela de reCAPTCHA / 'Help us confirm it's you' (checkpoint anti-bot)."""
    try:
        for sel in ['iframe[src*="recaptcha"]', 'iframe[title*="recaptcha" i]',
                    'iframe[src*="captcha"]', 'iframe[title*="challenge" i]']:
            try:
                if await page.locator(sel).count():
                    return True
            except Exception:
                pass
        txt = (await page.inner_text("body", timeout=1500) or "").lower()
    except Exception:
        return False
    needles = [
        "not a robot", "i'm not a robot", "im not a robot",
        "help us confirm", "confirm it's you", "confirme que é você", "confirme que e voce",
        "recaptcha", "não sou um robô", "nao sou um robo", "sou humano",
        "complete the confirmation", "confirmation step",
    ]
    return any(n in txt for n in needles)


async def _wait_manual_captcha(page, profile_id: str, timeout: float = 420.0) -> bool:
    """Se houver reCAPTCHA/checkpoint, PAUSA o fluxo e espera o usuário resolver MANUALMENTE
    na janela aberta do Camoufox. Retorna True quando resolvido (captcha some e a tela avança),
    False no timeout. A janela permanece visível porque a criação roda com headless=False."""
    try:
        if not await _is_captcha_screen(page):
            return True
    except Exception:
        return True

    logger.warning(f"signup [{profile_id}]: reCAPTCHA/checkpoint detectado — aguardando resolução MANUAL na janela")
    _set(profile_id, status="waiting_captcha", step="manual_captcha",
         message="⚠️ Resolva o reCAPTCHA na janela do navegador para continuar. Aguardando você...")

    # Alerta inicial: traz a janela pra frente + som (o usuário pode estar em outra tarefa).
    await _alert_captcha(page, profile_id, bring_front=True)

    start = time.time()
    last_beep = time.time()
    while time.time() - start < timeout:
        await human.delay(2.5, 4.0)
        try:
            if await _email_code_screen(page):
                logger.info(f"signup [{profile_id}]: captcha resolvido — tela de código de email")
                _set(profile_id, status="creating", step="captcha_solved",
                     message="Captcha resolvido — seguindo para verificação.")
                return True
            if not await _is_captcha_screen(page):
                await human.delay(1.5, 2.5)  # deixa a próxima tela montar
                if not await _is_captcha_screen(page):
                    logger.info(f"signup [{profile_id}]: captcha resolvido — seguindo o fluxo")
                    _set(profile_id, status="creating", step="captcha_solved",
                         message="Captcha resolvido — seguindo.")
                    return True
            # Relembra com som a cada ~20s (sem roubar foco), caso o usuário esteja distraído.
            if time.time() - last_beep > 20:
                last_beep = time.time()
                await _alert_captcha(page, profile_id, bring_front=False)
        except Exception:
            pass

    logger.warning(f"signup [{profile_id}]: timeout aguardando resolução manual do captcha")
    _set(profile_id, status="error", step="captcha_timeout",
         error="reCAPTCHA não resolvido a tempo. Resolva na janela do navegador e tente novamente.")
    return False


def _set(profile_id: str, **kwargs):
    if profile_id not in _flows:
        _flows[profile_id] = {}
    _flows[profile_id].update(kwargs)
    # Reflete mudanças relevantes na conta persistida
    if "status" in kwargs:
        detail = kwargs.get("error") or kwargs.get("message") or ""
        _sync_account(profile_id, kwargs["status"], detail)
    if "username" in kwargs and "status" not in kwargs:
        acc_id = _flows[profile_id].get("account_id")
        if acc_id:
            try:
                accounts.set_username(acc_id, kwargs["username"])
            except Exception:
                pass


# ── Helpers de página ─────────────────────────────────────────────────────────

async def _visible(page, selector: str, timeout: int = 5000) -> bool:
    try:
        await page.locator(selector).first.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


async def _dismiss_cookies(page):
    """Fecha o banner de cookies se aparecer."""
    patterns = [
        "text=Allow all cookies",
        "text=Accept All",
        "text=Aceitar todos",
        "text=Aceitar tudo",
        '[data-testid="cookie-policy-manage-dialog-accept-button"]',
    ]
    for pat in patterns:
        try:
            loc = page.locator(pat).first
            if await loc.is_visible(timeout=2000):
                await human.click_locator(page, loc)
                await human.delay(0.8, 1.5)
                return
        except Exception:
            pass


async def _dismiss_overlays(page):
    """Fecha banners/avisos que possam COBRIR os controles (cookies, notas de
    privacidade, 'OK'/'Got it'/'Dismiss'). Clica APENAS em botões — nunca em links —
    para não navegar acidentalmente para páginas de ajuda/legal e se perder no fluxo.
    """
    await _dismiss_cookies(page)
    # Apenas botões de FECHAR aviso — nada que avance/aceite algo (ex.: "Continue",
    # "Next", "Agree"), para não pular etapas nem aceitar termos sem querer.
    labels = ["Got it", "Got It", "Entendi", "Dismiss", "Fechar", "Close"]
    for txt in labels:
        try:
            loc = page.get_by_role("button", name=txt, exact=False).first
            if await loc.is_visible(timeout=600):
                await loc.click(timeout=1500)
                await human.micro(0.2, 0.5)
        except Exception:
            pass


async def _find_next_button(page) -> bool:
    """Clica no botão Next/Avançar/Próximo de forma segura.

    Antes fecha sobreposições e prefere o clique NATIVO do Playwright (que garante
    clicar no botão real e não num banner por cima). Evita seletores de link (<a>),
    que foram a causa de "entrar num link errado" na tela de aniversário.
    """
    await _dismiss_overlays(page)
    candidates = [
        'div[role="dialog"] button:has-text("Next")',
        'div[role="dialog"] button:has-text("Avançar")',
        'button[type="submit"]',
        'button:has-text("Next")',
        'button:has-text("Avançar")',
        'button:has-text("Próximo")',
        'button:has-text("Continue")',
        'button:has-text("Continuar")',
        'div[role="button"]:has-text("Next")',
        'div[role="button"]:has-text("Avançar")',
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                try:
                    # Clique nativo: o Playwright recusa clicar se o alvo estiver
                    # coberto por um overlay, em vez de clicar no lugar errado.
                    await loc.click(timeout=4000)
                except Exception:
                    await human.click_locator(page, loc)   # fallback humanizado
                return True
        except Exception:
            pass
    return await _click_signup(page)


async def _birthday_screen(page, timeout: int = 7000) -> bool:
    """Detecta a tela de aniversário sem preencher data pelo Python."""
    if await _date_controls_ready(page):
        return True
    return await _visible(page, "select", timeout=timeout)


async def _date_controls_ready(page) -> bool:
    """Detecta controles reais de data antes de tentar mexer em dia/mes/ano."""
    try:
        if await page.locator("select").count() >= 3:
            return True
    except Exception:
        pass
    try:
        return bool(await page.evaluate(r"""() => {
            const norm = text => String(text || "")
              .normalize("NFD")
              .replace(/[\u0300-\u036f]/g, "")
              .toLowerCase();
            const visible = el => {
              const r = el.getBoundingClientRect();
              const cs = getComputedStyle(el);
              return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none";
            };
            const dateWords = /(birthday|birth date|date of birth|nascimento|aniversario|fecha de nacimiento|cumpleanos|date de naissance|anniversaire|geburtstag|data urodzenia|nascita|dogum)/i;
            const body = norm(document.body?.innerText || "");
            if (dateWords.test(body)) return true;
            const controls = [...document.querySelectorAll("select, [role='combobox'], [aria-haspopup], [aria-expanded]")]
              .filter(visible);
            if (controls.length < 2) return false;
            const labels = controls.map(el => norm([
              el.textContent,
              el.getAttribute("aria-label"),
              el.getAttribute("name"),
              el.getAttribute("id"),
              el.getAttribute("placeholder")
            ].join(" "))).join(" ");
            return dateWords.test(labels) || /(day|dia|jour|tag|month|mes|mois|monat|year|ano|annee|jahr)/i.test(labels);
        }"""))
    except Exception:
        return False


def _norm(s) -> str:
    """Minúsculas sem acentos (casa rótulos/opções em pt/en/es)."""
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


async def _check_username_taken(page) -> bool:
    """Retorna True se o Instagram indicar que o username está indisponível.

    Usa duas estratégias para ser robusto contra mudanças no DOM do IG:
    - Elementos com role=alert (normalizado com _norm para acentos).
    - Varredura no body.textContent (igual à lógica do filler JS da extensão),
      normalizado. Inclui "sugest" só como sinal fraco de que sugestões foram
      oferecidas (normalmente indica que o nome digitado está ruim).
    """
    taken_phrases = [
        # Português (BR/PT)
        "indisponivel", "indisponível", "não está disponível", "nao esta disponivel",
        "já está em uso", "ja esta em uso", "este nome de usuario", "nome de usuario nao esta",
        "tente outro",

        # English (comum em muitos perfis e no screenshot atual)
        "taken", "not available", "isn't available", "is not available",
        "is taken", "already taken", "unavailable", "try another",
        "the username is not available", "username is not available",
        "the username", "username is not",

        # Español
        "indisponible", "no está disponible", "no esta disponible",
        "ya está en uso", "ya esta en uso", "este nombre de usuario",
        "prueba otro", "intenta otro",

        # Outros comuns (FR, IT, etc. - IG é multilíngue)
        "n'est pas disponible", "non è disponibile", "ist nicht verfügbar",
        "nicht verfügbar", "déjà utilisé", "gia in uso"
    ]
    weak_suggestion_signals = ["sugest", "sugestoes", "suggestions"]

    try:
        # 1) Alertas explícitos (mais confiáveis quando presentes)
        for sel in ['[role="alert"]']:
            loc = page.locator(sel)
            cnt = await loc.count()
            for i in range(min(cnt, 6)):
                try:
                    txt = (await loc.nth(i).inner_text()) or ""
                    ntxt = _norm(txt)
                    if any(p in ntxt for p in taken_phrases):
                        return True
                except Exception:
                    continue

        # 2) Scan amplo no body (cobre casos em que a mensagem não usa role=alert
        #    ou está em spans/divs comuns). Mesma filosofia do content.js do filler.
        try:
            js = r"""() => {
                const norm = t => (t||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase();
                const phrases = [
                    // PT
                    "indisponivel", "nao esta disponivel", "ja esta em uso", "tente outro",
                    "este nome de usuario", "nome de usuario nao esta",
                    // EN
                    "not available", "is taken", "already taken", "try another", "unavailable",
                    "the username is not available", "username is not available", "the username",
                    // ES
                    "indisponible", "no esta disponible", "ya esta en uso", "prueba otro",
                    "este nombre de usuario",
                    // Outros
                    "n'est pas disponible", "non e disponibile", "ist nicht verfugbar"
                ];
                const txt = norm(document.body.textContent || '');
                return phrases.some(p => txt.includes(p));
            }"""
            if await page.evaluate(js):
                return True
        except Exception:
            pass

        # 3) Sinal fraco: se aparecer "sugestões" logo após digitar um nome,
        #    o IG geralmente está oferecendo alternativas porque o atual está em uso.
        #    Usamos só como reforço (não como único gatilho sozinho para evitar falsos positivos).
        try:
            body_norm = _norm(await page.inner_text("body"))
            has_weak = any(s in body_norm for s in weak_suggestion_signals)
            if has_weak:
                # Só considera taken se também houver algum indício de erro ou se o input atual
                # parece ter sido rejeitado (não confiamos só no rótulo "Sugestões").
                for sel in ['[role="alert"]']:
                    loc = page.locator(sel)
                    for i in range(min(await loc.count(), 3)):
                        t = _norm((await loc.nth(i).inner_text()) or "")
                        if any(p in t for p in taken_phrases):
                            return True
                # Se só o "sugest" sem taken explícito, não marcamos taken aqui (deixamos o caller decidir).
        except Exception:
            pass

    except Exception:
        pass
    return False


async def _pick_username_suggestion(page) -> "str|None":
    """
    Aceita a sugestão de username do Instagram (portado da extensão do usuário).
    Acha o primeiro elemento abaixo do campo cujo texto parece um username, clica
    nele e devolve o username escolhido. Retorna None se não houver sugestão.
    """
    js = r"""() => {
      const norm = t => (t||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase().trim();
      const looksUser = t => { const v=norm(t).replace(/^@/,''); return /^[a-z0-9._]{3,30}$/.test(v) && /[a-z]/.test(v); };
      const unsafe = t => ['avancar','next','criar','create','cadastrar','sign up','inscrever','enviar','submit','entrar','login','already','tenho uma conta','politica','privacy','terms','ajuda','help','voltar'].some(w=>t.includes(w));
      const input = [...document.querySelectorAll('input[type=search], input')].find(i=>{
        const a = norm(i.getAttribute('aria-label'))+' '+norm(i.name)+' '+norm(i.getAttribute('placeholder'));
        return a.includes('user') || a.includes('usuario');
      });
      const irect = input ? input.getBoundingClientRect() : null;

      // Busca agressiva por sugestões de username do IG (pílulas, botões, itens de lista abaixo do campo)
      const selectors = [
        "[role=option]", "[role=radio]", "[role=button]", "button",
        "li", "div[tabindex]", "div[role=listbox] *", "[aria-label*='sugest']",
        "[data-testid*='suggestion']", "span", "a",
        "div[class*='suggestion']", "div[class*='pill']", "button[class*='suggestion']"
      ].join(", ");
      let els = [...document.querySelectorAll(selectors)];

      // Se não achou nada com os seletores, pega todos os elementos visíveis na página como fallback
      if (els.length < 3) {
        els = [...document.querySelectorAll("button, div, span, li, a")];
      }

      const seen = [];
      for (const el of els) {
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height) continue;
        // Mais tolerante com posição: abaixo ou próximo verticalmente ao campo
        if (irect) {
          const below = r.top >= irect.bottom - 20;
          const nearVert = Math.abs(r.top - irect.bottom) < 180;
          if (!below && !nearVert) continue;
          const tooFarLeft = r.right < irect.left - 100;
          const tooFarRight = r.left > irect.right + 200;
          if (tooFarLeft || tooFarRight) continue;
        }
        const raw = (el.getAttribute('aria-label') || el.textContent || el.innerText || '').trim();
        const t = norm(raw);
        if (!t || t.length > 45 || unsafe(t)) continue;
        // Extrai o primeiro pedaço que parece username limpo
        let cand = t.replace(/^@/,'').split(/\s+/).find(looksUser) || '';
        if (!cand && looksUser(t)) cand = t.replace(/^@/,'');
        // Só aceita se for username "bonito" (não só números)
        if (cand && looksUser(cand) && /[a-z]/.test(cand)) {
          seen.push({el, cand, top:r.top, left:r.left, area: r.width * r.height});
        }
      }
      // Prefere os que estão mais acima (mais próximos do campo) e com tamanho razoável
      seen.sort((a,b) => a.top - b.top || (b.area - a.area));
      for (const s of seen.slice(0, 4)) {
        try {
          s.el.scrollIntoView({block: 'center'});
          s.el.click();
          // pequeno tempo para o IG popular o input
          return s.cand;
        } catch(e){}
      }
      return '';
    }"""
    try:
        val = await page.evaluate(js)
        return (val or "").strip() or None
    except Exception:
        return None


async def _email_code_screen(page) -> bool:
    """
    Detecta a tela de confirmação por EMAIL — o fluxo PADRÃO do cadastro por
    email do Instagram. Aparece logo após o aniversário, antes de qualquer
    verificação por telefone (que só surge se a conta for sinalizada).
    """
    sels = [
        'input[name="email_confirmation_code"]',
        'input[name="confirmationCode"]',
        'input[aria-label*="confirmation code"]',
        'input[aria-label*="código de confirmação"]',
    ]
    for sel in sels:
        if await _visible(page, sel, timeout=2500):
            return True
    # Texto típico da tela de código por email
    for txt in ["Enter the confirmation code", "Insira o código de confirmação",
                "We sent a code to", "Enviamos um código para",
                "código de 6 dígitos", "6-digit code"]:
        try:
            if await page.get_by_text(txt, exact=False).first.is_visible(timeout=1500):
                return True
        except Exception:
            pass
    return False


async def _phone_verification_screen(page) -> bool:
    """Detecta tela que pede número de telefone / verificação por SMS ou WhatsApp."""
    for txt in ["WhatsApp", "phone number", "número de telefone",
                "Confirm your phone", "Confirme seu telefone", "mobile number"]:
        try:
            if await page.get_by_text(txt, exact=False).first.is_visible(timeout=1500):
                return True
        except Exception:
            pass
    if await _visible(page, 'input[name="phoneNumber"]', timeout=1500):
        return True
    return False


async def _click_whatsapp_option(page) -> bool:
    """Localiza e clica na opção 'Verificar via WhatsApp' na tela de verificação."""
    candidates = [
        'button:has-text("WhatsApp")',
        'div[role="button"]:has-text("WhatsApp")',
        'a:has-text("WhatsApp")',
        'span:has-text("WhatsApp")',
        ':has-text("Enviar código pelo WhatsApp")',
        ':has-text("Send code via WhatsApp")',
        ':has-text("Use WhatsApp")',
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=3000):
                await human.click_locator(page, loc)
                await human.delay(1.5, 3.0)
                logger.info("Opção WhatsApp selecionada")
                return True
        except Exception:
            pass
    logger.warning("Botão WhatsApp não encontrado — verificação manual necessária")
    return False


async def _fill_verification_code(page, code: str) -> bool:
    """Digita o código de verificação recebido."""
    code_sels = [
        'input[name="confirmationCode"]',
        'input[aria-label*="confirmation"]',
        'input[aria-label*="code"]',
        'input[aria-label*="código"]',
        'input[placeholder*="code"]',
        'input[placeholder*="código"]',
        'input[type="tel"]',
        'input[type="number"]',
        'input[inputmode="numeric"]',
    ]
    for sel in code_sels:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=3000):
                await human.type_text(page, sel, code, wpm=60)
                await human.delay(0.5, 1.2)
                # Tenta confirmar
                confirm_sels = [
                    'button[type="submit"]',
                    'button:has-text("Confirm")',
                    'button:has-text("Confirmar")',
                    'button:has-text("Next")',
                    'button:has-text("Continue")',
                ]
                for csel in confirm_sels:
                    try:
                        cloc = page.locator(csel).first
                        if await cloc.is_visible(timeout=2000):
                            await human.click_locator(page, cloc)
                            return True
                    except Exception:
                        pass
                await page.keyboard.press("Enter")
                return True
        except Exception:
            pass
    return False


async def _confirm_login_success(page, timeout: int = 50) -> bool:
    """Após preencher código de verificação, espera até termos sinais claros de que
    estamos logados no app principal do Instagram (feed/home). Mantém a janela aberta
    durante esse tempo crítico. Só depois disso consideramos a criação 'realmente finalizada'.
    """
    start = time.time()
    active_sels = [
        'svg[aria-label="Home"]', 'svg[aria-label="Página inicial"]',
        'a[href="/explore/"]', 'a[href*="/direct/"]',
        'svg[aria-label="New post"]', 'svg[aria-label="Nova publicação"]',
        'svg[aria-label="Reels"]'
    ]
    while time.time() - start < timeout:
        try:
            await _dismiss_overlays(page)
            try:
                await _dismiss_cookies(page)
            except Exception:
                pass

            for sel in active_sels:
                if await _visible(page, sel, timeout=1200):
                    try:
                        u = page.url
                    except Exception:
                        u = ""
                    logger.info(f"[{u}] confirm_login_success: elemento de feed detectado")
                    return True

            url = (page.url or "").lower()
            if "/accounts/login" not in url and "/challenge" not in url and "emailsignup" not in url:
                # Possivelmente já no app — checa texto do body
                try:
                    body = (await page.inner_text("body", timeout=1500) or "").lower()
                    if any(k in body for k in ["para você", "for you", "seguindo", "following", "reels", "explorar"]):
                        return True
                except Exception:
                    pass

        except Exception:
            pass

        await human.delay(2.2, 3.8)

    logger.warning("confirm_login_success: timeout sem detectar login completo")
    return False


async def _verify_whatsapp(page, profile_id: str, api_key: str, country: int) -> dict:
    """
    Fluxo completo de verificação automática via WhatsApp:
    1. Compra número no sms-activate
    2. Clica em "Verificar via WhatsApp" no Instagram
    3. Digita o número comprado
    4. Aguarda o código chegar via polling
    5. Preenche o código e confirma
    """
    client = SmsActivate(api_key)
    activation = None

    try:
        # Verifica saldo antes
        try:
            bal = await client.balance()
            logger.info(f"sms-activate saldo: ${bal:.2f}")
            if bal < 0.50:
                _set(profile_id, status="error",
                     error=f"Saldo insuficiente no sms-activate: ${bal:.2f}")
                return _flows[profile_id]
        except Exception as e:
            logger.warning(f"Não foi possível verificar saldo: {e}")

        # Compra número WhatsApp
        _set(profile_id, step="buying_number")
        activation = await client.get_number(country=country, service="wa")
        phone = activation.phone
        logger.info(f"Número comprado: +{phone} (id={activation.id})")
        _set(profile_id, step="phone_acquired", phone=f"+{phone}")
        # Registra o número WhatsApp na conta monitorada
        acc_id = (_flows.get(profile_id) or {}).get("account_id")
        if acc_id:
            try:
                accounts.set_whatsapp(acc_id, f"+{phone}")
            except Exception:
                pass

        # Seleciona opção WhatsApp na tela de verificação
        await _click_whatsapp_option(page)
        await human.delay(1.5, 3.0)

        # Preenche o número no campo (Instagram pode pedir o número do WhatsApp)
        phone_sels = [
            'input[name="phoneNumber"]',
            'input[type="tel"]',
            'input[aria-label*="phone"]',
            'input[aria-label*="telefone"]',
            'input[placeholder*="phone"]',
            'input[placeholder*="telefone"]',
        ]
        for sel in phone_sels:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=3000):
                    await human.type_text(page, sel, f"+{phone}", wpm=50)
                    await human.delay(0.8, 1.5)
                    # Confirma envio
                    for bsel in ['button[type="submit"]', 'button:has-text("Send")',
                                 'button:has-text("Enviar")', 'button:has-text("Next")']:
                        try:
                            bloc = page.locator(bsel).first
                            if await bloc.is_visible(timeout=2000):
                                await human.click_locator(page, bloc)
                                break
                        except Exception:
                            pass
                    break
            except Exception:
                pass

        await human.delay(2.0, 4.0)

        # Aguarda código do WhatsApp (polling)
        _set(profile_id, step="waiting_whatsapp_code")
        code = await client.wait_code(activation.id, timeout=180, poll_interval=5)
        logger.info(f"Código WhatsApp recebido: {code}")

        # Preenche o código
        _set(profile_id, step="filling_code")
        ok = await _fill_verification_code(page, code)

        await asyncio.sleep(3)
        await client.set_done(activation.id)

        if ok:
            _set(profile_id, status="created", step="code_filled")
            # Manter a janela aberta e confirmar login real antes de considerar finalizado
            await human.delay(2.5, 4.5)
            _set(profile_id, step="confirming_login")
            logged_in = await _confirm_login_success(page, timeout=50)
            if logged_in:
                _set(profile_id, status="created", step="logged_in",
                     message="Conta criada e logada com sucesso no Instagram (WhatsApp).")
            else:
                _set(profile_id, status="created", step="post_verification",
                     message="Código WhatsApp preenchido. Verifique se logou na janela.")
        else:
            _set(profile_id, status="code_filled_manually_needed", step="verify_code",
                 code=code)

        return _flows[profile_id]

    except SmsActivateError as e:
        logger.error(f"sms-activate error: {e}")
        if activation:
            try:
                await client.set_cancel(activation.id)
            except Exception:
                pass
        _set(profile_id, status="error", error=str(e))
        return _flows[profile_id]
    except Exception as e:
        logger.exception("_verify_whatsapp error")
        if activation:
            try:
                await client.set_cancel(activation.id)
            except Exception:
                pass
        _set(profile_id, status="error", error=str(e))
        return _flows[profile_id]


async def _page_fill(page, payload: dict):
    """Preenche e opcionalmente envia no contexto da página, sem injetar extensão."""
    try:
        return await page.evaluate(r"""async (data) => {
          const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
          const norm = text => String(text || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim();
          const visible = el => {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none";
          };
          const dispatch = el => {
            for (const type of ["input", "change"]) el.dispatchEvent(new Event(type, {bubbles: true, cancelable: true}));
          };
          const setValue = (el, value) => {
            const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value");
            desc && desc.set ? desc.set.call(el, value) : (el.value = value);
            dispatch(el);
          };
          const activate = el => {
            el.scrollIntoView({block: "center", inline: "center"});
            try { el.focus(); } catch (_) {}
            for (const type of ["pointerover","pointerenter","mouseover","mouseenter","pointerdown","mousedown","pointerup","mouseup","click"]) {
              el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
            }
          };
          const ordered = selector => [...document.querySelectorAll(selector)].filter(visible).sort((a, b) => {
            const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
            return ar.top - br.top || ar.left - br.left;
          });
          const meta = el => norm([el.type, el.name, el.id, el.autocomplete, el.inputMode, el.getAttribute("aria-label"), el.getAttribute("placeholder")].join(" "));
          const password = () => ordered('input[type="password"]')[0] || null;
          const scope = () => {
            const p = password();
            return p?.closest("form") || p?.closest("main, section, article") || document;
          };
          const looksUsername = input => {
            const m = meta(input);
            return norm(input.type) === "search" || /user|usuario|utilisateur|benutzer|username|handle|login/.test(m);
          };
          const textInputs = () => ordered("input, textarea")
            .filter(input => {
              const t = norm(input.getAttribute("type") || "text");
              return !["hidden", "password", "checkbox", "radio", "submit", "button"].includes(t);
            })
            .filter(input => scope() === document || scope().contains(input));
          const out = {filled: 0, missing: [], submit: null};
          const used = new Set();
          const texts = textInputs();

          if (data.contact) {
            const contact = texts.find(input => !looksUsername(input) && /(email|mail|phone|tel|mobile|celular|telefone|fono|telefono|e-mail)/.test(meta(input)))
              || texts.find(input => !looksUsername(input)) || texts[0];
            if (contact) { setValue(contact, data.contact); used.add(contact); out.filled += 1; }
            else out.missing.push("contato");
          }
          if (data.password) {
            const p = password();
            if (p) { setValue(p, data.password); used.add(p); out.filled += 1; }
            else out.missing.push("senha");
          }
          if (data.fullName) {
            const fullName = texts.find(input => !used.has(input) && !looksUsername(input) && /name|nome|nombre|nom|vorname|nachname|fullname|full name/.test(meta(input)))
              || texts.find(input => !used.has(input) && !looksUsername(input));
            if (fullName) { setValue(fullName, data.fullName); used.add(fullName); out.filled += 1; }
            else out.missing.push("nome");
          }

          const monthNames = {
            1:["january","janeiro","enero","janvier","januar","gennaio","ocak","januari","jan"],
            2:["february","fevereiro","febrero","fevrier","februar","febbraio","subat","februari","feb","fev"],
            3:["march","marco","marzo","mars","marz","maerz","mart","maret","mar"],
            4:["april","abril","avril","aprile","nisan","apr"],
            5:["may","maio","mayo","mai","maggio","mayis","mei"],
            6:["june","junho","junio","juin","juni","giugno","haziran","jun"],
            7:["july","julho","julio","juillet","juli","luglio","temmuz","jul"],
            8:["august","agosto","aout","augustus","agust","ago","aug"],
            9:["september","setembro","septiembre","septembre","settembre","eylul","sep","set"],
            10:["october","outubro","octubre","octobre","oktober","ottobre","ekim","oct","okt","out"],
            11:["november","novembro","noviembre","novembre","kasim","nov"],
            12:["december","dezembro","diciembre","decembre","dezember","dicembre","aralik","desember","dec","dez"]
          };
          const cands = (value, aliases = []) => [...new Set([String(value), String(Number(value)), ...aliases].map(norm).filter(Boolean))];
          const optionMatches = (el, candidates) => {
            const t = norm([el.getAttribute("aria-label"), el.getAttribute("value"), el.textContent].join(" "));
            return candidates.some(c => /^\d+$/.test(c) ? t.split(/\s+/).includes(c) : (t === c || t.includes(c)));
          };
          const fillSelect = (value, aliases, index) => {
            if (!value) return false;
            const select = ordered("select")[index];
            if (!select) return false;
            const opt = [...select.options].find(item => optionMatches(item, cands(value, aliases)));
            if (!opt) return false;
            setValue(select, opt.value);
            return true;
          };
          const dropdowns = () => ordered("button, [role='button'], [role='combobox'], [aria-haspopup], [aria-expanded], [tabindex]").filter(el => {
            const text = norm(el.textContent || el.getAttribute("aria-label") || "");
            return !/(submit|enviar|next|avancar|continue|sign|cadastrar|criar|login|entrar|cancel|voltar|back)/.test(text);
          });
          const clickDropdown = async (value, aliases, kind, index) => {
            if (!value) return false;
            const trigger = dropdowns()[index];
            if (!trigger) return false;
            const candidates = cands(value, aliases);
            activate(trigger);
            await wait(100);
            const findOptions = () => ordered("[role='option'], [role='menuitem'], [aria-selected], li, button, [tabindex]")
              .filter(el => optionMatches(el, candidates))
              .sort((a, b) => norm(a.textContent).length - norm(b.textContent).length);
            let opt = findOptions()[0];
            if (!opt) {
              for (const box of ordered("body, [role='listbox'], [role='menu'], ul, div").filter(el => el.scrollHeight > el.clientHeight + 8).slice(0, 8)) {
                const max = box.scrollHeight - box.clientHeight;
                const step = Math.max(48, Math.floor(box.clientHeight * 0.75));
                for (let top = 0; top <= max; top += step) {
                  box.scrollTop = top;
                  box.dispatchEvent(new Event("scroll", {bubbles: true}));
                  await wait(35);
                  opt = findOptions()[0];
                  if (opt) break;
                }
                if (opt) break;
              }
            }
            if (opt) { activate(opt); return true; }
            const n = Number(value);
            if (Number.isFinite(n)) {
              const steps = kind === "year" ? Math.max(0, new Date().getFullYear() - n) : n - 1;
              trigger.dispatchEvent(new KeyboardEvent("keydown", {key: "Home", bubbles: true, cancelable: true}));
              for (let i = 0; i < steps && i < 140; i++) trigger.dispatchEvent(new KeyboardEvent("keydown", {key: "ArrowDown", bubbles: true, cancelable: true}));
              trigger.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true}));
              dispatch(trigger);
              return true;
            }
            return false;
          };
          const fillDate = async (value, kind, index, aliases = []) => {
            if (!value) return false;
            return fillSelect(value, aliases, index) || await clickDropdown(value, aliases, kind, index);
          };
          const dateControlsReady = () => {
            if (ordered("select").length >= 3) return true;
            const dateWords = /(birthday|birth date|date of birth|nascimento|aniversario|fecha de nacimiento|cumpleanos|date de naissance|anniversaire|geburtstag|data urodzenia|nascita|dogum)/i;
            const body = norm(document.body?.innerText || "");
            if (dateWords.test(body)) return true;
            const controls = ordered("select, [role='combobox'], [aria-haspopup], [aria-expanded]");
            if (controls.length < 2) return false;
            const labels = controls.map(el => norm([
              el.textContent,
              el.getAttribute("aria-label"),
              el.getAttribute("name"),
              el.getAttribute("id"),
              el.getAttribute("placeholder")
            ].join(" "))).join(" ");
            return dateWords.test(labels) || /(day|dia|jour|tag|month|mes|mois|monat|year|ano|annee|jahr)/i.test(labels);
          };
          const shouldFillDate = data.allowDate !== false && (data.day || data.month || data.year) && dateControlsReady();
          if (shouldFillDate) {
            if (await fillDate(data.day, "day", 0)) out.filled += 1;
            else if (data.day) out.missing.push("dia");
            if (await fillDate(data.month, "month", 1, monthNames[Number(data.month)] || [])) out.filled += 1;
            else if (data.month) out.missing.push("mes");
            if (await fillDate(data.year, "year", 2)) out.filled += 1;
            else if (data.year) out.missing.push("ano");
          } else if (data.day || data.month || data.year) {
            out.dateSkipped = true;
          }

          const flush = () => {
            for (const el of document.querySelectorAll("input, textarea, select")) {
              dispatch(el);
              el.dispatchEvent(new Event("blur", {bubbles: true}));
            }
            if (document.activeElement?.blur) document.activeElement.blur();
          };
          if (data.submit) {
            flush();
            await wait(120);
            const good = /(submit|enviar|sign\s*up|inscrev|cadastre|cadastrar|criar\s*conta|registrar|registrarse|next|avancar|continue|continuar|join|get\s*started)/i;
            const bad = /(entrar|log\s*in|login|already|ja tenho|tenho uma conta|voltar|back|cancel|cookies?|privacy|privacidade|terms|termos|help|ajuda)/i;
            const picked = ordered("button, [role='button'], input[type='submit']").map(el => {
              const text = norm(el.innerText || el.textContent || el.value || el.getAttribute("aria-label") || "");
              let score = 0;
              if (scope().contains(el)) score += 50;
              if (el.tagName === "BUTTON") score += 10;
              if (norm(el.getAttribute("type")) === "submit") score += 35;
              if (good.test(text)) score += 70;
              if (bad.test(text)) score -= 160;
              if (el.disabled || el.getAttribute("aria-disabled") === "true") score -= 200;
              return {el, text, score};
            }).filter(x => x.score > 20).sort((a, b) => b.score - a.score)[0];
            if (picked?.el) {
              activate(picked.el);
              try { picked.el.click(); } catch (_) {}
              out.submit = {ok: true, method: "page-evaluate-click", text: picked.text};
            } else {
              const form = scope()?.closest?.("form") || scope()?.querySelector?.("form");
              if (form?.requestSubmit) {
                form.requestSubmit();
                out.submit = {ok: true, method: "requestSubmit"};
              } else {
                out.submit = {ok: false, reason: "submit control not found"};
              }
            }
            await wait(180);
            flush();
          }
          return out;
        }""", payload)
    except Exception as e:
        logger.warning(f"page_fill falhou: {e}")
        return {"error": str(e), "filled": 0, "missing": []}


async def _read_username(page) -> str:
    """Lê o username que está no campo agora (genérico — funciona tanto para o
    preenchimento automático quanto pela sugestão do Instagram)."""
    sels = [
        'input[type="search"]',
        'input[aria-label*="usuário" i]',
        'input[aria-label*="usuario" i]',
        'input[aria-label*="username" i]',
        'input[name*="user" i]',
    ]
    for sel in sels:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                v = (await loc.input_value()).strip().lstrip("@")
                if v:
                    return v
        except Exception:
            pass
    return ""


async def _capture_username(page, timeout: float = 12.0) -> str:
    """
    Captura o username que o Instagram preencheu/sugeriu automaticamente.
    Não digita nada. Apenas espera o campo aparecer e ter um valor.
    Usado no fluxo automático agora que deixamos o IG escolher o user.
    """
    start = time.time()
    last_value = ""

    sels = [
        'input[type="search"]',
        'input[aria-label*="usuário" i]',
        'input[aria-label*="usuario" i]',
        'input[aria-label*="username" i]',
        'input[name*="user" i]',
    ]

    while time.time() - start < timeout:
        for sel in sels:
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    val = (await loc.input_value()).strip().lstrip("@")
                    if val and val != last_value:
                        last_value = val
                        # dá um pouco mais de tempo pro IG estabilizar
                        await human.delay(0.6, 1.0)
                        val2 = (await loc.input_value()).strip().lstrip("@")
                        if val2:
                            return val2
            except Exception:
                pass

        await human.delay(0.5, 0.9)

        # também checa se já avançamos para tela que mostra o username escolhido
        try:
            # alguns fluxos mostram o username escolhido em texto (não input)
            for txt_sel in ["[data-testid*='username']", "text=@" ]:
                el = page.locator(txt_sel).first
                if await el.count():
                    t = (await el.inner_text()).strip().lstrip("@")
                    if t and len(t) > 2 and len(t) < 35:
                        return t
        except Exception:
            pass

    # fallback: retorna o último valor não-vazio que vimos
    return last_value


def _username_input_selector(n_text_inputs: int = 0) -> str:
    """Seletor do campo de username no cadastro do IG."""
    return ('input[type="search"], input[aria-label*="usuário" i], '
            'input[aria-label*="usuario" i], input[aria-label*="username" i], '
            'input[name*="user" i]')


async def _ensure_username(page, desired: str, full_name: str = "") -> str:
    """Preenche o username e RESOLVE a indisponibilidade pegando a sugestão do IG.

    Conforme o comportamento do IG: digita o usuário, ESPERA ~2s para o IG validar e,
    se o proposto já estiver em uso, ele oferece sugestões — então clicamos na
    sugestão (em vez de inventar um texto aleatório). Só geramos uma variação como
    último recurso, se nenhuma sugestão aparecer.

    Melhorias:
    - Re-verifica taken após aceitar sugestão (evita aceitar uma "sugestão" que o IG ainda marca como ruim).
    - Sempre re-check após mudar o valor do campo antes de decidir que está bom.
    - Mais waits e polling para dar tempo ao IG atualizar o estado visual/texto.
    """
    sel = None
    for s in ['input[type="search"]', 'input[aria-label*="usuário" i]',
              'input[aria-label*="usuario" i]', 'input[aria-label*="username" i]',
              'input[name*="user" i]']:
        try:
            if await page.locator(s).count():
                sel = s
                break
        except Exception:
            pass
    if not sel:
        logger.warning("username: campo não encontrado")
        return desired

    username = desired
    try:
        await human.type_text(page, sel, username, clear=True)
    except Exception:
        pass
    await human.delay(5.0, 5.5)   # 5 segundos para o IG validar o nome inicial / base

    max_attempts = 6
    for attempt in range(max_attempts):
        # 5 segundos de settle + polling — para ter CERTEZA de que o nome atual está disponível
        # antes de decidir continuar gerando variantes. O IG é lento para atualizar o estado.
        await human.delay(4.8, 5.2)
        good = False
        for poll in range(3):
            if not await _check_username_taken(page):
                logger.info(f"username: '{username}' parece disponível (top check poll {poll+1}) — parando aqui")
                good = True
                break
            await human.delay(0.7, 1.0)
        if good:
            break

        logger.debug(f"username: '{username}' marcado como taken (tentativa {attempt+1})")

        # Tenta pegar a sugestão do IG (prioridade: não inventar)
        sug = await _pick_username_suggestion(page)
        if not sug:
            # Dá tempo extra (5s) pro IG mostrar sugestões antes de desistir e gerar variação aleatória
            await human.delay(5.0, 5.5)
            sug = await _pick_username_suggestion(page)

        if sug:
            username = sug
            logger.info(f"username: aceitou sugestão do IG -> {username}")
            await human.delay(0.9, 1.6)
            # Garante que o input realmente recebeu o valor da sugestão
            cur = await _read_username(page)
            if _norm(cur) != _norm(sug):
                try:
                    await human.type_text(page, sel, sug, clear=True)
                    await human.delay(5.0, 5.5)  # 5s após forçar sugestão
                except Exception:
                    pass

            # IMPORTANTE: re-verifica se a sugestão foi aceita como válida (com polling para ter certeza)
            await human.delay(4.8, 5.2)
            for poll in range(3):
                if not await _check_username_taken(page):
                    logger.info(f"username: sugestão '{username}' agora está disponível após re-check — parando")
                    break
                await human.delay(0.8, 1.2)
            else:
                # todas polls ainda taken
                logger.info(f"username: sugestão '{username}' ainda parece taken após polling — continuando")
                continue
            break  # se saiu do for de poll sem taken, paramos aqui

        # Último recurso: gera variação local e tenta de novo
        # 5 SEGUNDOS para cada variação — tempo necessário pro Instagram validar o nome
        # e atualizar o DOM (remover mensagem de indisponível ou não).
        # Depois do delay fazemos polling para ter CERTEZA de que está disponível antes de parar.
        username = _gen_username(full_name or desired)
        logger.info(f"username: sem sugestão clicável; tentando variação -> {username}")
        try:
            await human.type_text(page, sel, username, clear=True)
        except Exception:
            pass
        await human.delay(5.0, 5.5)   # 5 segundos para cada variação

        # Re-check com polling para ter certeza que vai parar quando o user estiver disponível
        good = False
        for poll in range(4):
            if not await _check_username_taken(page):
                logger.info(f"username: variação '{username}' parece disponível (poll {poll+1}) — parando o loop")
                good = True
                break
            await human.delay(0.7, 1.0)
        if good:
            break

    # Fonte da verdade: o que ficou no campo (pode ter sido ajustado pelo IG depois)
    final_user = await _read_username(page)
    result = (final_user or username).lstrip("@")

    # Verificação final forte: espera 5s + múltiplos polls para ter CERTEZA
    # de que paramos com um username realmente disponível (ou o melhor que conseguimos).
    await human.delay(5.0, 5.5)
    for poll in range(3):
        if not await _check_username_taken(page):
            logger.info(f"username: confirmação final — '{result}' está disponível (poll {poll+1})")
            break
        await human.delay(0.8, 1.2)
        # re-ler em caso de mudança tardia do IG
        final_user = await _read_username(page)
        result = (final_user or result).lstrip("@")
    else:
        logger.warning(f"username: após todas as tentativas, o nome final '{result}' ainda parece taken no check. "
                       "Deixando assim mesmo (IG pode ter aceitado visualmente).")

    logger.info(f"username: resultado final no campo = {result}")
    return result


# ── Fluxo principal ───────────────────────────────────────────────────────────

_MONTH_LABELS = {
    1: ["January", "Janeiro", "Enero", "Janvier"],
    2: ["February", "Fevereiro", "Febrero", "Fevrier"],
    3: ["March", "Marco", "Marzo", "Mars"],
    4: ["April", "Abril", "Avril"],
    5: ["May", "Maio", "Mayo", "Mai"],
    6: ["June", "Junho", "Junio", "Juin"],
    7: ["July", "Julho", "Julio", "Juillet"],
    8: ["August", "Agosto", "Aout"],
    9: ["September", "Setembro", "Septiembre", "Septembre"],
    10: ["October", "Outubro", "Octubre", "Octobre"],
    11: ["November", "Novembro", "Noviembre", "Novembre"],
    12: ["December", "Dezembro", "Diciembre", "Decembre"],
}


async def _select_option_safe(locator, candidates) -> bool:
    """Tenta select_option por value e por label, sem levantar exceção."""
    for c in candidates:
        try:
            await locator.select_option(value=str(c), timeout=1500)
            return True
        except Exception:
            pass
    for c in candidates:
        try:
            await locator.select_option(label=str(c), timeout=1500)
            return True
        except Exception:
            pass
    return False


async def _pick_custom_dropdown(page, label_words, option_texts) -> bool:
    """Abre um dropdown customizado (combobox/botão) pelo NOME e clica na opção pelo TEXTO,
    tudo com clique REAL do Playwright. Exclui links/<a> e ícones de ajuda ('?'), que era o
    que abria abas de ajuda e desalinhava o índice dos dropdowns de data."""
    try:
        trigger = await page.evaluate_handle(r"""(words) => {
            const norm = t => (t||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase().trim();
            const visible = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
              return r.width>0 && r.height>0 && cs.visibility!=='hidden' && cs.display!=='none'; };
            const bad = /(help|ajuda|why|por ?que|learn|saiba|feedback|cookie|privac|terms|termos)/;
            const cands = [...document.querySelectorAll("select, [role='combobox'], [aria-haspopup], [aria-expanded], button, [role='button'], [tabindex]")]
              .filter(visible)
              .filter(el => el.tagName !== 'A' && !el.closest('a'))
              .filter(el => { const t = norm((el.getAttribute('aria-label')||'') + ' ' + (el.textContent||'')); return t !== '?' && !bad.test(t); });
            const match = cands.find(el => {
              const t = norm((el.getAttribute('aria-label')||'') + ' ' + (el.getAttribute('title')||'') + ' ' + (el.textContent||''));
              const tokens = t.split(/[^a-z]+/);
              return words.some(w => tokens.includes(w) || t === w || t.startsWith(w + ' '));
            });
            return match || null;
        }""", [w.lower() for w in label_words])
        el = trigger.as_element() if trigger else None
        if not el:
            return False
        try:
            await el.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        await el.click(timeout=4000)
        await human.delay(0.3, 0.7)

        opt = await page.evaluate_handle(r"""(texts) => {
            const norm = t => (t||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase().trim();
            const visible = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
              return r.width>0 && r.height>0 && cs.visibility!=='hidden' && cs.display!=='none'; };
            const wanted = texts.map(norm);
            const cands = [...document.querySelectorAll("[role='option'], [role='menuitemradio'], [role='menuitem'], option, li, div, span, button")]
              .filter(visible)
              .filter(el => el.tagName !== 'A' && !el.closest('a'));
            const match = cands.find(el => wanted.includes(norm(el.textContent)));
            return match || null;
        }""", [str(t) for t in option_texts])
        oe = opt.as_element() if opt else None
        if not oe:
            return False
        try:
            await oe.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        await oe.click(timeout=4000)
        await human.delay(0.2, 0.5)
        return True
    except Exception as e:
        logger.warning(f"_pick_custom_dropdown({label_words}) falhou: {e}")
        return False


async def _fill_birthday_trusted(page, bd: dict) -> bool:
    """Preenche dia/mês/ano com interações confiáveis (Playwright). Trata <select> nativo
    (select_option) e dropdowns customizados (clique real no gatilho + opção). Não clica em
    links/ajuda — por isso não abre abas como o preenchimento via JS fazia."""
    try:
        month = int(bd.get("month") or 0)
        day = int(bd.get("day") or 0)
        year = int(bd.get("year") or 0)
    except Exception:
        return False
    if not (month and day and year):
        return False

    # 1) <select> nativo (mais confiável)
    try:
        selects = page.locator("select")
        n = await selects.count()
        if n >= 3:
            metas = [(i, await selects.nth(i).locator("option").count()) for i in range(n)]

            def pick(pred):
                for i, c in metas:
                    if pred(c):
                        return selects.nth(i)
                return None

            sm = pick(lambda c: 11 <= c <= 14)
            sd = pick(lambda c: 28 <= c <= 33)
            sy = pick(lambda c: c >= 40)
            if sm is not None and sd is not None and sy is not None:
                rm = await _select_option_safe(sm, [str(month)] + _MONTH_LABELS[month])
                rd = await _select_option_safe(sd, [str(day)])
                ry = await _select_option_safe(sy, [str(year)])
                if rm and rd and ry:
                    logger.info("birthday: preenchido via <select> nativo")
                    return True
    except Exception as e:
        logger.warning(f"birthday(select) falhou: {e}")

    # 2) Dropdowns customizados
    okm = await _pick_custom_dropdown(page, ["month", "mes"], _MONTH_LABELS[month] + [str(month)])
    okd = await _pick_custom_dropdown(page, ["day", "dia"], [str(day)])
    oky = await _pick_custom_dropdown(page, ["year", "ano", "ano"], [str(year)])
    ok = okm and okd and oky
    logger.info(f"birthday(custom): mes={okm} dia={okd} ano={oky}")
    return ok


def _gen_username_strong(base: str) -> str:
    """Username de alta entropia: base do nome + 4-5 dígitos. Colisão quase nula, então
    quase nunca precisa regenerar — evita toda a fragilidade de detectar 'taken'/sugestões."""
    slug = _slugify(base)[:15] or "user"
    digits = "".join(random.choices(string.digits, k=random.randint(4, 5)))
    sep = "_" if random.random() < 0.2 else ""
    return f"{slug}{sep}{digits}"[:30]


async def _username_taken_for(page, candidate: str) -> bool:
    """True só se o IG mostrar que ESTE candidato específico está indisponível."""
    cand = (candidate or "").lower().lstrip("@")
    if not cand:
        return False
    try:
        txt = (await page.inner_text("body", timeout=1500) or "").lower()
    except Exception:
        return False
    if cand not in txt:
        return False
    return any(w in txt for w in [
        "not available", "isn't available", "is not available",
        "nao esta disponivel", "não está disponível", "indisponivel", "indisponível",
        "is taken", "already taken", "tente outro", "try another",
    ])


async def _fill_username_trusted(page, full_name: str, fallback: str = "", max_attempts: int = 5) -> str:
    """Digita um username de alta entropia com digitação REAL. Só regenera se o IG marcar
    AQUELE nome como indisponível. NÃO lê sugestões da página (evita pegar lixo tipo 'terms'/'jobs').
    Se o IG já tiver preenchido o campo sozinho com um valor válido, mantém esse."""
    sel = None
    for s in ['input[type="search"]', 'input[aria-label*="usuario" i]',
              'input[aria-label*="usuário" i]', 'input[aria-label*="username" i]',
              'input[name*="user" i]']:
        try:
            if await page.locator(s).count():
                sel = s
                break
        except Exception:
            pass
    if not sel:
        logger.warning("username: campo não encontrado")
        return fallback

    # Se o IG já preencheu um username válido sozinho, respeita.
    try:
        current = (await page.locator(sel).first.input_value()).strip().lstrip("@")
        if current and not await _username_taken_for(page, current):
            logger.info(f"username: IG já preencheu '{current}' — mantendo")
            return current
    except Exception:
        pass

    candidate = _gen_username_strong(full_name or fallback or "user")
    for attempt in range(max_attempts):
        try:
            await human.type_text(page, sel, candidate, clear=True)
        except Exception as e:
            logger.warning(f"username: falha ao digitar '{candidate}': {e}")
        await human.delay(2.0, 2.8)  # deixa o IG validar
        if not await _username_taken_for(page, candidate):
            logger.info(f"username: '{candidate}' aceito (tentativa {attempt + 1})")
            return candidate
        logger.info(f"username: '{candidate}' indisponível — gerando outro")
        candidate = _gen_username_strong(full_name or fallback or "user")
    logger.warning(f"username: mantendo '{candidate}' após {max_attempts} tentativas")
    return candidate


async def create_account(page, profile_id: str, data: dict) -> dict:
    """
    Preenche o formulário de cadastro do Instagram.

    IMPORTANTE (atualizado):
    - Não forçamos mais username durante o preenchimento.
    - O Instagram costuma sugerir/preencher o username automaticamente
      assim que email + full_name são preenchidos.
    - Nós apenas capturamos o username que o IG escolheu no final
      (usando _capture_username) e salvamos.
    - Birthday vem do caller ou é gerado com gen_birthday().
      O preenchimento roda por um filler interno no contexto da página; depois
      o Python apenas observa se o submit/enviar avançou.

    data = {
        "email":     str,
        "password":  str,
        "full_name": str,
        "username":  str | None,    # opcional — usado apenas como referência/fallback
        "birthday":  {"month": int, "day": int, "year": int} | None
    }
    """
    global stop_requested
    # NOTE: global must be declared before any use of the name in the function body.

    email     = data["email"]
    password  = data["password"]
    full_name = data["full_name"]

    # Birthday: usa o que veio do caller ou gera realista (18-38 anos).
    # A data é preenchida pelo filler interno no contexto da página.
    bd = data.get("birthday") or gen_birthday()

    # Não forçamos mais o username no preenchimento (IG sugere sozinho).
    # Geramos apenas como referência inicial / fallback.
    username = data.get("username") or _gen_username(full_name)

    _set(profile_id,
         status="starting", email=email, username=username,
         step="navigating", started_at=time.time(), error=None)

    # Registra com o valor de referência (será atualizado depois com o que o IG realmente usar)
    try:
        account_id = accounts.record_new_account(profile_id, {
            "email": email, "password": password, "username": username,
        })
        _set(profile_id, account_id=account_id)
    except Exception as e:
        logger.warning(f"não foi possível registrar conta no monitor: {e}")

    try:
        if stop_requested:
            _set(profile_id, status="cancelled", error="Cancelado pelo usuário")
            stop_requested = False
            return {"status": "cancelled"}

        # ── Passo 1: Abrir cadastro (navegação robusta e isolada) ─────────────
        # Solução independente do método de preenchimento/addon:
        # - Preferimos criar uma página *nova* dedicada para o fluxo de signup.
        #   Reutilizar a página inicial (que veio de about:blank) pode deixar o contexto
        #   em estado frágil após launch com stealth + proxy + geoip.
        # - Usamos a versão melhorada de _goto_retry que evita waits rígidos de domcontentloaded
        #   (principal fonte de crashes internos no Camoufox ao abrir IG).
        try:
            ctx = getattr(page, "context", None)
            if ctx is not None:
                # Cria página limpa para o fluxo de criação (mais estável)
                ig_page = await ctx.new_page()
                # Fecha a aba about:blank original — senão ficam DUAS abas abertas
                # (a inicial + a do cadastro), o que confunde e é leve sinal de bot.
                try:
                    if page is not ig_page and not page.is_closed():
                        await page.close()
                except Exception:
                    pass
            else:
                ig_page = page
        except Exception:
            ig_page = page

        await _goto_retry(ig_page, "https://www.instagram.com/accounts/emailsignup/")
        logger.info(f"signup [{profile_id}] abriu: {ig_page.url}")

        # Atualiza a referência na sessão se possível (para que outros códigos vejam a página certa)
        try:
            import session_manager as sm
            if profile_id in sm._sessions:
                sm._sessions[profile_id]["page"] = ig_page
        except Exception:
            pass

        page = ig_page  # a partir daqui usamos a página dedicada

        await human.delay(2.5, 5.0)
        await _dismiss_overlays(page)   # cookies + qualquer banner/aviso sobreposto
        await human.delay(1.0, 2.0)

        _set(profile_id, step="filling_form")

        # ── Preenchimento com DIGITAÇÃO REAL (teclado), sem extensão ─────────────
        # Não injetamos valor via JS (sinal detectável de "campo mudou sem digitar").
        # Email/senha/nome são digitados letra a letra; data e username via clique/typing reais.
        used_page_filler = False
        try:
            on_signup_form = await page.locator('input[type="text"], input[type="password"]').count() > 0
        except Exception:
            on_signup_form = False

        if on_signup_form:
            n = await _fill_text_fields_trusted(page, email, password, full_name)
            logger.info(f"signup [{profile_id}] digitação real (pre-fill): {n} campo(s)")
            used_page_filler = n >= 2
            if not used_page_filler:
                logger.warning(f"signup [{profile_id}] digitação real não preencheu o essencial ({n} campo(s))")

        if not used_page_filler:
            _set(profile_id, status="error",
                 error="O filler interno não conseguiu preencher o formulário.")
            return _flows[profile_id]

        if stop_requested:
            _set(profile_id, status="cancelled", error="Cancelado pelo usuário")
            stop_requested = False
            return {"status": "cancelled"}

        # ── Data de nascimento (caso esteja na mesma tela) ───────────────────
        # DOB é preenchido pelo filler interno, sem injetar extensão.
        bd_done = False  # evita re-clicar a data no submit (era clicada 2x)
        try:
            if await _date_controls_ready(page):
                _set(profile_id, step="filling_birthday")
                ok_bd = await _fill_birthday_trusted(page, bd)
                bd_done = bool(ok_bd)
                logger.info(f"signup [{profile_id}] aniversário (clique real): ok={ok_bd}")
                await human.delay(0.4, 1.0)
        except Exception:
            logger.warning(f"[{profile_id}] aniversário (tela inicial) falhou — tentará após enviar")

        # ── Captura o username que o Instagram sugeriu automaticamente ──
        # IG costuma preencher/sugerir o username sozinho assim que email + nome são preenchidos.
        # Não forçamos mais nenhum username (removido _ensure_username e toda a lógica de variantes).
        # Apenas lemos o que o IG colocou no campo e guardamos.
        # Username: o IG NÃO preenche sozinho neste formulário (campo obrigatório/vazio).
        # Digitamos um username de alta entropia (base do nome + dígitos) com digitação REAL.
        # Quase nunca colide, então não dependemos de detectar 'taken' nem de sugestões da página.
        _set(profile_id, step="filling_username")
        typed_user = await _fill_username_trusted(page, full_name, fallback=username)
        if typed_user:
            username = typed_user
            _set(profile_id, username=username)
            logger.info(f"signup [{profile_id}] username definido: {username}")
            acc_id = _flows.get(profile_id, {}).get("account_id")
            if acc_id:
                try:
                    accounts.set_username(acc_id, username)
                except Exception:
                    pass
        else:
            logger.warning(f"signup [{profile_id}] não foi possível definir username — mantendo: {username}")

        # Pequena pausa para o formulário React estabilizar antes do submit.
        await human.delay(1.8, 3.2)

        # ── Passo 6: filler interno finaliza preenchimento + envia ───────────
        _set(profile_id, step="submitting")

        # O filler interno recebe todos os campos novamente e clica em submit no mesmo
        # ciclo JS. O Python não move mouse nem clica aqui; apenas observa avanço.
        submitted = False
        advanced_after_submit = False
        # Uma vez que o botão Enviar foi clicado com sucesso, NÃO reenviar. Reenviar
        # (re-digitar + clicar de novo) numa tela em transição é double-submit = sinal
        # forte de bot — era o que sinalizava as contas (e ainda fazia o aniversário
        # falhar, pois a tela já tinha mudado). Depois de enviar, um humano só ESPERA.
        submit_ok_once = False
        for attempt in range(5):
            # GUARDA: se já avançou, NÃO refazer o preenchimento. Senão o fill escreveria
            # o EMAIL no primeiro input da próxima tela (ex.: campo do CÓDIGO).
            # Um captcha/checkpoint aqui NÃO é falha: o _wait_manual_captcha logo após o
            # loop assume — então saímos do loop em vez de re-preencher por cima dele.
            try:
                if await _is_captcha_screen(page):
                    advanced_after_submit = True
                    submitted = True
                    logger.info(f"signup [{profile_id}]: captcha/checkpoint detectado — deixa o handler manual assumir")
                    break
                if await _email_code_screen(page):
                    advanced_after_submit = True
                    submitted = True
                    logger.info(f"signup [{profile_id}]: tela de código detectada — encerra submit sem refazer fill")
                    break
                pwd_here = await _visible(page, 'input[type="password"]', timeout=600)
                if not pwd_here and "emailsignup" not in (page.url or "").lower():
                    advanced_after_submit = True
                    submitted = True
                    logger.info(f"signup [{profile_id}]: já fora da tela de cadastro — encerra submit sem refazer fill")
                    break
            except Exception:
                pass

            # Só PREENCHE+ENVIA enquanto o submit ainda não foi aceito. Tudo trusted —
            # clique REAL do Playwright, sem injeção JS de valor.
            if not submit_ok_once:
                try:
                    # Só re-garante a data se ainda NÃO foi setada (evita o clique
                    # duplo no caso normal) OU numa re-tentativa (attempt>0), quando
                    # a tela pode ter mudado/resetado.
                    if (not bd_done or attempt > 0) and await _date_controls_ready(page):
                        bd_done = await _fill_birthday_trusted(page, bd)
                except Exception:
                    pass
                # Só re-digita os campos a partir da 2ª tentativa (o pre-fill já preencheu na 1ª).
                if attempt > 0:
                    try:
                        n = await _fill_text_fields_trusted(page, email, password, full_name)
                        logger.info(f"signup [{profile_id}]: digitação real (re-fill) tentativa {attempt+1}: {n} campo(s)")
                    except Exception as e:
                        logger.warning(f"signup [{profile_id}]: digitação real tentativa {attempt+1} falhou: {e}")
                await human.delay(0.5, 1.1)
                try:
                    clicked = await _submit_trusted(page)
                    submitted = submitted or clicked
                    if clicked:
                        submit_ok_once = True
                    logger.info(f"signup [{profile_id}]: submit (clique real) tentativa {attempt+1}: ok={clicked}")
                except Exception as e:
                    logger.warning(f"signup [{profile_id}]: submit (clique real) falhou tentativa {attempt+1}: {e}")
            else:
                logger.info(f"signup [{profile_id}]: submit já aceito — aguardando avanço sem reenviar (tentativa {attempt+1})")

            # Espera ativa por avanço. Mais paciente quando já enviamos (o IG pode levar
            # alguns segundos ou exibir captcha). Detecta captcha também aqui.
            advanced = False
            for wait_i in range(10 if submit_ok_once else 6):
                await human.delay(1.0, 1.8)
                try:
                    # Captcha ou tela de código → segue (o captcha é tratado após o loop)
                    if await _is_captcha_screen(page) or await _email_code_screen(page):
                        advanced = True
                        break
                    # Ou sumiu o campo de senha / controles de data da tela inicial, ou URL mudou
                    pwd_visible = await _visible(page, 'input[type="password"]', timeout=800)
                    dob_visible = await _date_controls_ready(page)
                    url = (page.url or "").lower()
                    if not pwd_visible and not dob_visible and "emailsignup" not in url:
                        advanced = True
                        break
                except Exception:
                    pass

            if advanced:
                advanced_after_submit = True
                logger.info(f"signup [{profile_id}]: submit parece ter avançado (tentativa {attempt+1})")
                break

            logger.info(f"signup [{profile_id}]: ainda não avançou (tentativa {attempt+1}); "
                        + ("aguardando" if submit_ok_once else "repetindo fill+submit"))
            await human.delay(0.8, 1.5)

        if not submitted:
            logger.warning(f"signup [{profile_id}]: filler interno não conseguiu acionar submit")
        if not advanced_after_submit:
            detail = await _signup_visible_problem(page)
            # Diagnóstico: por que o IG não avança? Salva screenshot + texto completo + URL.
            # (A mensagem curta de _signup_visible_problem costuma vir vazia em telas de
            #  captcha/anti-bot; o screenshot e o body completo revelam a causa real.)
            shot_path = ""
            try:
                shot_path = str(LOGS_DIR / f"signup_fail_{profile_id}_{int(time.time())}.png")
                await page.screenshot(path=shot_path, full_page=True)
            except Exception as _e:
                logger.warning(f"signup [{profile_id}]: falha ao salvar screenshot: {_e}")
            try:
                body_txt = (await page.inner_text("body", timeout=2000) or "")[:1500]
            except Exception:
                body_txt = ""
            try:
                cur_url = page.url
            except Exception:
                cur_url = "?"
            logger.warning(
                f"signup [{profile_id}]: submit sem avanço. url={cur_url} visivel='{detail}' "
                f"screenshot={shot_path}\n--- texto da tela ---\n{body_txt}\n--- fim ---"
            )
            msg = ("O botão Enviar não avançou após várias tentativas. "
                   "Verifique a janela do navegador.")
            if detail:
                msg += f" Mensagem visível: {detail}"
            _set(profile_id, status="error", step="submit_failed", error=msg)
            return _flows[profile_id]

        await human.delay(2.0, 3.5)

        # ── reCAPTCHA / checkpoint: pausa para o usuário resolver MANUALMENTE ──
        # Após o submit avançar, o IG costuma exibir "Help us confirm it's you" (reCAPTCHA).
        # Não tentamos resolver automaticamente: pausamos e esperamos o usuário resolver na janela.
        if not await _wait_manual_captcha(page, profile_id):
            return _flows[profile_id]

        # ── Passo 7: Data de nascimento ───────────────────────────────────────
        # Só tenta se ainda não avançamos para a tela de verificação por email.
        # (no fluxo "tudo em uma tela" o submit bem sucedido vai direto para o código)
        if await _email_code_screen(page):
            # Já estamos na tela de código → pula o bloco de aniversário
            pass
        else:
            await human.delay(1.0, 2.0)
            if await _birthday_screen(page):
                _set(profile_id, step="birthday")
                # Tenta preencher + avançar; se um banner desviar para uma página
                # fora do fluxo (ajuda/legal/privacidade), volta e tenta de novo.
                _OFFTRACK = ("/legal", "/privacy", "/help/", "/about/", "policies", "/terms")
                for bday_attempt in range(2):
                    # Data de nascimento e submit via clique REAL do Playwright (sem JS, sem abrir abas).
                    ok_bd = await _fill_birthday_trusted(page, bd)
                    logger.info(f"birthday [{profile_id}] (clique real) ok={ok_bd}")
                    await human.delay(0.6, 1.2)
                    try:
                        clicked = await _submit_trusted(page)
                        logger.info(f"birthday [{profile_id}] submit (clique real): ok={clicked}")
                    except Exception as e:
                        logger.warning(f"birthday [{profile_id}] submit (clique real) falhou: {e}")
                    await human.delay(2.0, 4.0)
                    if not any(x in page.url.lower() for x in _OFFTRACK):
                        break  # seguiu no fluxo correto
                    logger.warning(f"birthday: desviou para fora do fluxo ({page.url}) — voltando e refazendo")
                    try:
                        await page.go_back(wait_until="domcontentloaded")
                        await human.delay(1.5, 3.0)
                    except Exception:
                        break
                    if not await _birthday_screen(page):
                        break  # não voltou para a tela de aniversário — segue

        # ── Passo 8: Verificação ──────────────────────────────────────────────
        # O fluxo padrão do IG por email pede um CÓDIGO ENVIADO AO EMAIL primeiro.
        # Verificação por telefone/WhatsApp só aparece se a conta for sinalizada.
        sms_key     = data.get("sms_activate_key")
        sms_country = data.get("sms_country", 73)  # 73 = Brasil

        await human.delay(1.5, 3.0)

        # Captcha pode aparecer também após o aniversário, antes do código. Pausa de novo se preciso.
        if not await _wait_manual_captcha(page, profile_id):
            return _flows[profile_id]

        # Caso 1: tela de código por EMAIL (o caminho mais comum)
        if await _email_code_screen(page):
            email_code = data.get("email_code")
            # Sem código manual → tenta ler automaticamente via IMAP (catch-all)
            if not email_code and email_box.get_config().get("configured"):
                _set(profile_id, step="reading_email_code")
                try:
                    email_code = await email_box.fetch_code(email, timeout=120, poll=6)
                except Exception:
                    email_code = None
            if email_code:
                _set(profile_id, step="filling_email_code")
                ok = await _fill_verification_code(page, str(email_code))
                if ok:
                    _set(profile_id, status="created", step="code_filled", code=str(email_code))
                    # NÃO retorne ainda: espere a transição real para o app logado.
                    # Mantém a janela aberta até confirmarmos que estamos dentro do Instagram.
                    await human.delay(2.5, 4.5)
                    _set(profile_id, step="confirming_login")
                    logged_in = await _confirm_login_success(page, timeout=55)
                    if logged_in:
                        _set(profile_id, status="created", step="logged_in",
                             message="Conta criada e logada com sucesso no Instagram.")
                    else:
                        _set(profile_id, status="created", step="post_verification",
                             message="Código de email preenchido. Aguarde ou verifique manualmente se logou.")
                else:
                    _set(profile_id,
                         status="waiting_for_email_code",
                         step="verify_email",
                         username=username,
                         code=str(email_code),
                         message="Preenchimento do código falhou — complete manualmente na janela.")
            else:
                # Sem código automático: usuário deve digitar no navegador (janela mantida aberta)
                _set(profile_id,
                     status="waiting_for_email_code",
                     step="verify_email",
                     username=username,
                     message="Instagram enviou um código ao EMAIL. "
                             "Digite-o na janela do navegador (ou configure o IMAP catch-all).")
            return _flows[profile_id]

        # Caso 2: tela de verificação por telefone/WhatsApp (conta FORTEMENTE sinalizada)
        if await _phone_verification_screen(page):
            if sms_key:
                return await _verify_whatsapp(page, profile_id, sms_key, sms_country)
            # Sem sms-activate: NÃO ficamos presos nem pagamos. A conta foi sinalizada pelo IG
            # (pediu telefone no cadastro) → descartamos e seguimos para a próxima. A janela é
            # fechada pelo chamador (status 'flagged_phone'). Foco: reduzir os sinais que levam aqui.
            _set(profile_id,
                 status="flagged_phone",
                 step="verify_phone",
                 username=username,
                 message="Conta sinalizada: o Instagram exigiu verificação por telefone/WhatsApp "
                         "no cadastro. Descartada (foco em reduzir detecção: proxy/warmup/ritmo).")
            logger.warning(f"signup [{profile_id}]: descartada — IG exigiu telefone/WhatsApp (conta sinalizada)")
            return _flows[profile_id]

        # Caso 3: nenhuma tela de verificação detectada — possivelmente já criada
        # Mesmo assim, tente confirmar se já estamos logados (pode ter pulado verificação).
        _set(profile_id, step="confirming_login")
        logged_in = await _confirm_login_success(page, timeout=30)
        if logged_in:
            _set(profile_id, status="created", step="logged_in",
                 username=username,
                 message="Cadastro aparentemente concluído sem verificação extra — logado.")
        else:
            _set(profile_id,
                 status="waiting_for_verification",
                 step="verification",
                 username=username,
                 message="Verifique a janela do navegador para concluir o cadastro.")
        return _flows[profile_id]

    except asyncio.CancelledError:
        _set(profile_id, status="cancelled")
        return _flows[profile_id]
    except Exception as e:
        logger.exception("create_account error")
        _set(profile_id, status="error", error=str(e))
        return _flows[profile_id]


# ── Verificação de status (automática) ─────────────────────────────────────────
# Os padrões de texto vivem em _STATUS_PATTERNS_V2 / _STATUS_PATTERNS_V1 (topo do
# arquivo) e são resolvidos por _get_status_patterns(). Adicione novos termos lá.

async def detect_status(page) -> dict:
    """
    Navega para o Instagram e detecta o estado da conta.

    Suporta versionamento da lógica de detecção + fallback automático entre versões.

    Ordem de tentativa:
    1. Versão atual (DETECTION_VERSION) com todos os fallbacks internos
    2. Se ainda der "unknown", tenta a versão anterior (v1) como fallback de emergência

    Isso permite que a gente atualize as regras (subindo DETECTION_VERSION) sem
    quebrar completamente o app quando o Instagram mudar algo inesperado.
    """
    # Tenta primeiro com a versão atual do app
    result = await _detect_status_with_version(page, DETECTION_VERSION)

    if result.get("status") != "unknown":
        result["detection_version"] = DETECTION_VERSION
        return result

    # Fallback para versão anterior se a atual falhou completamente
    logger.warning(f"detect_status retornou unknown na versão {DETECTION_VERSION}. Tentando fallback para v1...")
    fallback_result = await _detect_status_with_version(page, "v1")
    fallback_result["detection_version"] = "v1 (fallback)"
    fallback_result["detail"] = fallback_result.get("detail", "") + " [usando fallback v1]"
    return fallback_result


async def _detect_status_with_version(page, version: str) -> dict:
    """Executa a detecção usando os padrões de uma versão específica."""
    patterns = _get_status_patterns(version)

    try:
        await _goto_retry(page, "https://www.instagram.com/")
    except Exception as e:
        return {"status": "unknown", "detail": f"falha ao carregar: {e}"}

    await human.delay(3.0, 5.0)
    await _dismiss_cookies(page)

    url = page.url.lower()
    try:
        body = (await page.inner_text("body", timeout=4000)).lower()
    except Exception:
        body = ""

    def has(*terms):
        return any(t in body for t in terms)

    # Banida
    if has(*patterns.get("banned", [])):
        return {"status": "banned", "detail": "Conta desativada pelo Instagram"}

    # Suspensa
    if has(*patterns.get("suspended", [])):
        return {"status": "suspended", "detail": "Conta suspensa"}

    # Checkpoint
    if "/challenge/" in url or "/accounts/suspended" in url or has(*patterns.get("checkpoint", [])):
        return {"status": "checkpoint", "detail": "Verificação/desafio pendente"}

    # Login required
    if "/accounts/login" in url or (has("entrar", "log in", "iniciar sesión", "phone number, username") and "feed" not in body):
        if await _visible(page, 'input[name="username"], input[name="password"]', timeout=2500):
            return {"status": "login_required", "detail": "Sessão expirada — precisa logar"}

    # Ativa (elementos do feed)
    active_selectors = [
        'svg[aria-label="Home"]', 'svg[aria-label="Página inicial"]',
        'a[href="/explore/"]', 'a[href*="/direct/"]',
        'svg[aria-label="New post"]', 'svg[aria-label="Nova publicação"]',
        'svg[aria-label="Reels"]'
    ]
    for sel in active_selectors:
        if await _visible(page, sel, timeout=2000):
            return {"status": "active", "detail": "Sessão ativa e logada"}

    return {"status": "unknown", "detail": f"Estado indeterminado (url={page.url})"}


# ── Aquecimento da conta ──────────────────────────────────────────────────────

async def _warmup_browse(page, minutes: float = 8, account_age_days: int = 0):
    """
    Núcleo do aquecimento de CONTA com mais variedade.

    Comportamento varia conforme a "idade" da conta (account_age_days):
    - 0-3 dias   → Comportamento conservador (muito feed + explore, poucos follows)
    - 4-14 dias  → Mistura + alguns Reels, buscas, mais follows
    - 15+ dias   → Comportamento mais "maduro": Reels, Stories (simulado), buscas, navegação mais lenta,
                   visita a perfis sugeridos, menos follows novos (contas maduras seguem menos)

    Isso ajuda muito a reduzir detecção em contas com 7-15+ dias de vida.
    """
    total = minutes * 60
    loop = asyncio.get_event_loop()
    start = loop.time()

    # Fase 1: Feed principal (sempre)
    try:
        await _goto_retry(page, "https://www.instagram.com/")
        await human.delay(2.5, 5.0)
        await _dismiss_cookies(page)
        await human.idle_browse(page, random.uniform(40, 80))
    except Exception:
        pass

    # Fase 2: Explore ou Reels (varia por idade)
    try:
        if account_age_days >= 7 and random.random() < 0.7:
            # Contas mais velhas passam mais tempo em Reels
            await _goto_retry(page, "https://www.instagram.com/reels/")
            await human.delay(3, 6)
            await human.idle_browse(page, random.uniform(60, 110))
        else:
            await _goto_retry(page, "https://www.instagram.com/explore/")
            await human.delay(2.5, 5.0)
            await human.idle_browse(page, random.uniform(50, 90))
    except Exception:
        pass

    # Fase 3: Interações sociais (follows / visitas)
    try:
        if account_age_days < 15 or random.random() < 0.6:
            # Contas novas seguem mais. Contas velhas seguem com moderação.
            follow_btns = page.get_by_role("button", name=re.compile(r"^Follow$", re.I))
            max_follows = random.randint(1, 2) if account_age_days >= 15 else random.randint(2, 4)
            count = min(await follow_btns.count(), max_follows)
            for i in range(count):
                await human.delay(5.0, 12.0)
                await human.click_locator(page, follow_btns.nth(i))
                # Às vezes visita o perfil do seguido (comportamento natural)
                if random.random() < 0.4:
                    await human.delay(8, 18)
                    # Volta para o feed ou explore
                    if random.random() < 0.5:
                        await _goto_retry(page, "https://www.instagram.com/")
                    else:
                        await _goto_retry(page, "https://www.instagram.com/explore/")
    except Exception:
        pass

    # Fase 4: Busca (muito comum em contas reais, especialmente mais velhas)
    if account_age_days >= 5 and random.random() < 0.65:
        try:
            search_selectors = [
                'svg[aria-label="Pesquisar"]',
                'svg[aria-label="Search"]',
                'a[href="/explore/"]'  # fallback
            ]
            for sel in search_selectors:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2000):
                    await human.click_locator(page, loc)
                    await human.delay(1.5, 3.5)
                    # Digita algo genérico
                    await page.keyboard.type(random.choice(["nature", "travel", "food", "fitness", "brasil", "memes"]))
                    await human.delay(4, 9)
                    await human.idle_browse(page, random.uniform(30, 70))
                    break
        except Exception:
            pass

    # Fase final: Volta para o feed e preenche o tempo restante com idle + scroll variado
    try:
        await _goto_retry(page, "https://www.instagram.com/")
        await human.delay(1.5, 3.5)
        remaining = total - (loop.time() - start)
        if remaining > 8:
            await human.idle_browse(page, remaining)
    except Exception:
        pass


async def warmup(page, profile_id: str, minutes: int = 8, account_age_days: int = 0) -> dict:
    """Aquece a conta (aba Instagram). Mantém o estado de fluxo via _flows."""
    _set(profile_id, status="warming_up", step="warmup")
    try:
        await _warmup_browse(page, minutes, account_age_days=account_age_days)
        _set(profile_id, status="ready", step="done")
        try:
            accounts.warmed_by_profile(profile_id)
        except Exception:
            pass

    except asyncio.CancelledError:
        _set(profile_id, status="warmup_cancelled")
    except Exception as e:
        logger.exception("warmup error")
        _set(profile_id, status="warmup_error", error=str(e))

    return _flows[profile_id]
