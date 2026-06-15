import random
from browserforge.fingerprints import FingerprintGenerator, Screen

# Gerador FIREFOX — o app roda Camoufox, que É Firefox. Antes isto gerava um
# fingerprint de CHROME (UA "Chrome/…", vendor "Google Inc.", WebGL "Google Inc."),
# que não batia em nada com o navegador real e era exibido errado na UI.
#
# IMPORTANTE: o Camoufox gera o PRÓPRIO fingerprint coerente em tempo de execução.
# O que ele realmente consome deste dict é o `platform` (→ SO) e o `screen`. Os
# demais campos são INDICATIVOS (para exibição); o idioma/fuso são definidos pelo
# geoip em runtime conforme o IP do proxy.
_generator = FingerprintGenerator(
    browser="firefox",
    os=("windows", "macos", "linux"),
    screen=Screen(min_width=1280, max_width=1920, min_height=720, max_height=1080),
)

TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Los_Angeles",
    "America/Sao_Paulo", "Europe/London", "Europe/Berlin",
    "Europe/Paris", "Asia/Tokyo", "Asia/Singapore",
]

LOCALES = ["en-US", "en-GB", "pt-BR", "de-DE", "fr-FR", "ja-JP"]

# Distribuição realista de SOs desktop (Windows domina o tráfego real)
_OS_WEIGHTS = (["windows"] * 6) + (["macos"] * 3) + (["linux"] * 1)


def _pick_os(os: str) -> str:
    if os in ("random", "auto", "", None):
        return random.choice(_OS_WEIGHTS)
    return os


def generate(os: str = "windows") -> dict:
    os = _pick_os(os)
    # device='desktop': o projeto é desktop-only (Camoufox é Firefox desktop).
    fp = _generator.generate(os=os, device="desktop")

    hw = getattr(fp.navigator, "hardwareConcurrency", None) or random.choice([4, 8, 12, 16])

    return {
        # Semente ESTÁVEL do perfil: fixa GPU/fingerprint entre sessões no
        # Camoufox (session_manager._profile_seed / _stable_webgl). Sem ela o
        # mesmo perfil "trocaria de aparelho" a cada reabertura.
        "fp_seed": random.getrandbits(63),
        # Usados pelo Camoufox:
        "platform": fp.navigator.platform,
        "screen": {
            "width": fp.screen.width,
            "height": fp.screen.height,
            "colorDepth": fp.screen.colorDepth,
            "pixelDepth": fp.screen.pixelDepth,
        },
        # Indicativos (exibição) — Firefox-coerentes:
        "userAgent": fp.navigator.userAgent,
        "hardwareConcurrency": hw,
        "deviceMemory": None,            # Firefox NÃO expõe navigator.deviceMemory
        "language": random.choice(LOCALES),     # geoip define o real em runtime
        "timezone": random.choice(TIMEZONES),   # geoip define o real em runtime
        "doNotTrack": random.choice(["1", None]),
    }
