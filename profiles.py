import uuid
import shutil

from paths import PROFILES_DIR
from db import init_db, save_profile, get_profile, list_profiles, delete_profile
from fingerprint import generate


def create(
    name: str,
    os: str = "windows",
    proxy=None,
) -> dict:
    """
    proxy format:
    {
        "protocol": "http",   # http | socks5
        "host": "1.2.3.4",
        "port": 8080,
        "username": "user",   # optional
        "password": "pass",   # optional
    }
    """
    init_db()
    profile_id = str(uuid.uuid4())
    profile = {
        "id": profile_id,
        "name": name,
        "fingerprint": generate(os=os),
        "proxy": proxy,
        "user_data": str(PROFILES_DIR / profile_id),
    }
    save_profile(profile)
    print(f"Perfil criado: {name} ({profile_id})")
    return profile


def show(profile_id: str):
    init_db()
    p = get_profile(profile_id)
    if not p:
        print(f"Perfil não encontrado: {profile_id}")
        return
    fp = p["fingerprint"]
    print(f"\n{'='*50}")
    print(f"  Nome    : {p['name']}")
    print(f"  ID      : {p['id']}")
    print(f"  Criado  : {p['created_at']}")
    print(f"  OS      : {fp['platform']}")
    print(f"  UA      : {fp['userAgent'][:80]}...")
    print(f"  Tela    : {fp['screen']['width']}x{fp['screen']['height']}")
    print(f"  Idioma  : {fp['language']}")
    print(f"  Timezone: {fp['timezone']}")
    if p["proxy"]:
        px = p["proxy"]
        print(f"  Proxy   : {px['protocol']}://{px['host']}:{px['port']}")
    else:
        print(f"  Proxy   : nenhum")
    print(f"{'='*50}\n")


def ls():
    init_db()
    profiles = list_profiles()
    if not profiles:
        print("Nenhum perfil criado ainda.")
        return
    print(f"\n{'ID':<38} {'Nome':<20} {'OS':<12} {'Proxy':<20} {'Criado'}")
    print("-" * 110)
    for p in profiles:
        proxy_str = f"{p['proxy']['host']}:{p['proxy']['port']}" if p["proxy"] else "-"
        print(f"{p['id']:<38} {p['name']:<20} {p['fingerprint']['platform']:<12} {proxy_str:<20} {p['created_at']}")
    print()


def remove(profile_id: str):
    init_db()
    p = get_profile(profile_id)
    if not p:
        print(f"Perfil não encontrado: {profile_id}")
        return
    shutil.rmtree(p["user_data"], ignore_errors=True)
    delete_profile(profile_id)
    print(f"Perfil removido: {p['name']}")
