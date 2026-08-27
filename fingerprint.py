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


# Telas de celular comuns no Brasil (largura x altura em CSS px, retrato).
# Usadas só quando o perfil é ANDROID -- o gerador desktop nunca produz estas.
_TELAS_ANDROID = [
    (360, 800), (393, 873), (412, 915), (384, 854), (360, 780), (412, 892),
]


def _android() -> dict:
    """Fingerprint de celular Android.

    MOTIVO (2026-08-27): o Instagram só deixa POSTAR STORY na visão mobile. O
    Camoufox suporta `os="android"` nativamente e monta o conjunto coerente
    (UA `Android 16; Mobile`, platform `Linux armv81`, 5 pontos de toque), então
    não há gambiarra de user-agent aqui -- este dict só declara a intenção, e quem
    gera o fingerprint de verdade é o Camoufox em runtime.

    ⚠ Use em perfil NOVO. Trocar o SO de um perfil com sessão viva é o evento
    "login de novo dispositivo", que é justamente o que queima conta."""
    w, h = random.choice(_TELAS_ANDROID)
    return {
        "fp_seed": random.getrandbits(63),
        "platform": "Linux armv81",          # o que o _map_os lê para escolher android
        "screen": {"width": w, "height": h, "colorDepth": 24, "pixelDepth": 24},
        "userAgent": "Mozilla/5.0 (Android 16; Mobile; rv:150.0) Gecko/150.0 Firefox/150.0",
        "hardwareConcurrency": random.choice([4, 6, 8]),
        "deviceMemory": None,
        "language": "pt-BR",
        "timezone": "America/Sao_Paulo",
        "isMobile": True,
    }


def generate(os: str = "windows") -> dict:
    if str(os).lower() in ("android", "mobile"):
        return _android()
    os = _pick_os(os)
    # device='desktop': as demais opções são desktop (Camoufox é Firefox desktop).
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
        # Idioma/fuso de EXIBIÇÃO fixados em BR para casar com o geoip forçado
        # (session_manager._FORCE_GEOIP_IP = Porto Alegre). São só p/ o card; o
        # Camoufox aplica pt-BR/America-Sao_Paulo no runtime via geoip de qualquer jeito.
        # Trocar a cidade/país = trocar aqui E no _FORCE_GEOIP_IP.
        "language": "pt-BR",
        "timezone": "America/Sao_Paulo",
        "doNotTrack": random.choice(["1", None]),
    }
