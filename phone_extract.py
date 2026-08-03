"""
phone_extract.py — extração de seguidores DIRETO do app do Instagram no celular,
via ADB/UI Automator (SEM root, SEM login novo, SEM tirar a sessão do app).

Ideia (decisão do usuário 2026-07-26): a conta app-locked (só funciona no app) é
dirigida DENTRO do celular — o PC automatiza a UI do app já logado, lê os @ da tela
e organiza o resultado (dedupe/CSV/monitorados). Contorna a parede do root/pinning:
não precisa da sessão do app, porque a extração acontece no próprio app.

Este arquivo começa pelo ANALISADOR do dump de UI (o teste que decide se os @ são
legíveis na árvore). A automação de rolagem vem depois que o dump provar que dá.

Dump da tela atual (built-in do Android, nada a instalar além do adb):
    adb shell uiautomator dump /sdcard/ui.xml
    adb pull /sdcard/ui.xml
"""
import re
import xml.etree.ElementTree as ET

# @handle do Instagram: 1–30 chars, letras/números/._ (não termina em ., sem __)
_HANDLE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._]{0,28}[A-Za-z0-9])?$")
# Palavras da UI que NÃO são usernames (aparecem como texto na tela de seguidores)
_UI_NOISE = {
    "seguir", "follow", "following", "seguindo", "remove", "remover",
    "seguidores", "followers", "search", "pesquisar", "buscar", "message",
    "mensagem", "verificado", "verified", "close friend", "amigo próximo",
}


def _looks_like_handle(s: str) -> bool:
    s = (s or "").strip()
    if not s or " " in s or "." * 2 in s or s.startswith(".") or s.endswith("."):
        return False
    if s.lower() in _UI_NOISE:
        return False
    return bool(_HANDLE.match(s))


def analyze_uidump(xml_text: str) -> dict:
    """DIAGNÓSTICO: dado o XML do `uiautomator dump`, mostra o que a árvore expõe —
    resource-ids presentes, textos, content-descs — e um palpite dos usernames.
    Serve pra identificar ONDE os @ moram antes de escrever o extrator definitivo."""
    root = ET.fromstring(xml_text)
    res_ids, texts, descs = {}, [], []
    for node in root.iter("node"):
        rid = node.get("resource-id") or ""
        txt = node.get("text") or ""
        desc = node.get("content-desc") or ""
        if rid:
            res_ids[rid] = res_ids.get(rid, 0) + 1
        if txt.strip():
            texts.append(txt.strip())
        if desc.strip():
            descs.append(desc.strip())
    # Usernames candidatos: dos textos E dos content-descs (o IG às vezes põe o @ no
    # content-desc de "row_user_..."). Dedupe preservando ordem.
    cand, seen = [], set()
    for v in texts + descs:
        if _looks_like_handle(v) and v.lower() not in seen:
            seen.add(v.lower())
            cand.append(v)
    ig_ids = sorted(r for r in res_ids if "instagram" in r.lower())
    return {
        "nodes": sum(1 for _ in root.iter("node")),
        "resource_ids_instagram": ig_ids[:40],
        "texts_sample": texts[:40],
        "content_desc_sample": descs[:40],
        "username_candidates": cand,
        "candidate_count": len(cand),
    }


def extract_usernames(xml_text: str, id_hint: str = "") -> list:
    """Extrai os @ de um dump. id_hint = resource-id da célula de usuário (descoberto
    pelo analyze_uidump); se vazio, cai na heurística de 'parece um @'."""
    root = ET.fromstring(xml_text)
    out, seen = [], set()
    for node in root.iter("node"):
        rid = node.get("resource-id") or ""
        if id_hint and id_hint not in rid:
            continue
        for v in (node.get("text"), node.get("content-desc")):
            v = (v or "").strip()
            if _looks_like_handle(v) and v.lower() not in seen:
                seen.add(v.lower())
                out.append(v)
    return out


if __name__ == "__main__":
    import json
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "ui.xml"
    with open(path, "r", encoding="utf-8") as f:
        xml = f.read()
    print(json.dumps(analyze_uidump(xml), ensure_ascii=False, indent=2))
