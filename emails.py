"""
emails.py — emails via catch-all + IMAP (Hostinger).

Estratégia: você liga o "catch-all" num domínio do Hostinger, então QUALQUER
endereço @seudominio.com cai numa caixa única. O app:
  - INVENTA endereços (não precisa criar caixa por caixa);
  - LÊ o código de verificação do Instagram automaticamente via IMAP nessa caixa.

Config (em settings): email_domain, imap_host, imap_port, imap_user, imap_pass.
"""
import asyncio
import email as _email
import imaplib
import random
import re
import string
import uuid
from email.header import decode_header

import db


# ── Configuração ─────────────────────────────────────────────────────────────

_CONFIG_KEYS = ["email_domain", "imap_host", "imap_port", "imap_user", "imap_pass"]


def get_config(reveal: bool = False) -> dict:
    cfg = {
        "email_domain": db.get_setting("email_domain", ""),
        "imap_host":    db.get_setting("imap_host", "imap.hostinger.com"),
        "imap_port":    int(db.get_setting("imap_port", "993") or 993),
        "imap_user":    db.get_setting("imap_user", ""),
        "imap_pass":    db.get_setting("imap_pass", "") if reveal else
                        ("••••••" if db.get_setting("imap_pass", "") else ""),
    }
    cfg["configured"] = bool(cfg["email_domain"] and cfg["imap_host"] and cfg["imap_user"])
    return cfg


def set_config(**kwargs):
    for k in _CONFIG_KEYS:
        if k in kwargs and kwargs[k] is not None:
            v = kwargs[k]
            if isinstance(v, str):
                v = v.strip()  # remove espaços/quebras invisíveis do copiar-colar
            # não sobrescreve a senha com a máscara
            if k == "imap_pass" and v in ("", "••••••"):
                continue
            db.set_setting(k, v)
    return get_config()


# ── Geração de endereços ─────────────────────────────────────────────────────

_FIRST = ["ana", "lucas", "marina", "pedro", "julia", "rafa", "bruno", "carol",
          "diego", "leticia", "gabriel", "sofia", "mateus", "bia", "thiago"]


def _slug(base: str) -> str:
    s = re.sub(r"[^a-z0-9]", "", (base or "").lower())
    return s[:16] or random.choice(_FIRST)


def generate_address(base: str = "") -> str:
    domain = db.get_setting("email_domain", "")
    if not domain:
        raise ValueError("Configure o domínio de email primeiro")
    local = _slug(base) + "".join(random.choices(string.digits, k=random.randint(3, 5)))
    return f"{local}@{domain}"


def create_email(base: str = "", profile_id: str = None) -> dict:
    address = generate_address(base)
    eid = str(uuid.uuid4())
    db.save_email({"id": eid, "address": address, "profile_id": profile_id, "status": "free"})
    return db.get_email(eid)


def list_emails() -> list:
    return db.list_emails()


def claim_free_email(profile_id: str = None) -> "dict|None":
    """Reserva um e-mail LIVRE já cadastrado (comprado de terceiros, sem perfil
    vinculado) para este perfil — usado como SUGESTÃO na criação manual. Liga o
    e-mail ao perfil e o devolve. Retorna None se não houver e-mail livre."""
    for em in db.list_emails():
        if (em.get("status") or "free") == "free" and not em.get("profile_id"):
            if profile_id:
                db.update_email(em["id"], profile_id=profile_id)
            return db.get_email(em["id"])
    return None


def get_email(email_id: str):
    return db.get_email(email_id)


def mark_used(email_id: str, account_id: str = None):
    fields = {"status": "used"}
    if account_id:
        fields["account_id"] = account_id
    db.update_email(email_id, **fields)


def mark_used_by_address(address: str, account_id: str = None) -> bool:
    """Marca como 'usado' o e-mail com este endereço, se existir no registro.
    Retorna True se encontrou. No-op se o e-mail não está cadastrado (ex.: criado
    fora, no celular)."""
    addr = (address or "").strip().lower()
    if not addr:
        return False
    for em in list_emails():
        if (em.get("address") or "").strip().lower() == addr:
            if (em.get("status") or "free") != "used":
                mark_used(em["id"], account_id)
            return True
    return False


def delete_email(email_id: str):
    db.delete_email(email_id)


# ── Leitura de código via IMAP ───────────────────────────────────────────────

_CODE_PATTERNS = [
    re.compile(r"\b(\d{6})\b"),
]


def _decode(s) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="ignore")
        else:
            out += text
    return out


def _body_text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(errors="ignore")
                except Exception:
                    pass
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_payload(decode=True).decode(errors="ignore")
                except Exception:
                    pass
        return ""
    try:
        return msg.get_payload(decode=True).decode(errors="ignore")
    except Exception:
        return str(msg.get_payload())


def _extract_code(subject: str, body: str) -> "str|None":
    # O IG normalmente coloca o código no ASSUNTO ("123456 is your Instagram code").
    # Exigimos uma palavra-chave de contexto para não pegar 6 dígitos aleatórios
    # (ex.: um email promocional que caiu no catch-all).
    for source in (subject, body):
        low = (source or "").lower()
        if not any(w in low for w in ("instagram", "code", "código", "codigo", "confirm", "verif")):
            continue
        m = re.search(r"\b(\d{6})\b", source)
        if m:
            return m.group(1)
    return None


def _imap_fetch_code_sync(address: str) -> "str|None":
    cfg = get_config(reveal=True)
    if not (cfg["imap_host"] and cfg["imap_user"] and cfg["imap_pass"]):
        return None
    box = imaplib.IMAP4_SSL(cfg["imap_host"].strip(), cfg["imap_port"])
    try:
        box.login(cfg["imap_user"].strip(), cfg["imap_pass"].strip())
        box.select("INBOX")
        # procura mensagens destinadas a este endereço (catch-all entrega tudo na caixa)
        typ, data = box.search(None, "TO", address)
        ids = data[0].split() if data and data[0] else []
        if not ids:
            # alguns provedores não indexam TO no catch-all — varre as mais recentes
            typ, data = box.search(None, "ALL")
            ids = data[0].split() if data and data[0] else []
        for num in reversed(ids[-15:]):  # mais recentes primeiro
            typ, msg_data = box.fetch(num, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            msg = _email.message_from_bytes(msg_data[0][1])
            to_addr = (_decode(msg.get("To")) + " " + _decode(msg.get("Delivered-To"))).lower()
            subject = _decode(msg.get("Subject"))
            sender = _decode(msg.get("From")).lower()
            if address.lower() not in to_addr and "instagram" not in sender:
                continue
            code = _extract_code(subject, _body_text(msg))
            if code:
                return code
        return None
    finally:
        try:
            box.logout()
        except Exception:
            pass


async def fetch_code(address: str, timeout: int = 120, poll: int = 6) -> "str|None":
    """Aguarda (polling) o código de verificação do IG chegar na caixa catch-all."""
    loop = asyncio.get_event_loop()
    elapsed = 0
    while elapsed < timeout:
        try:
            code = await loop.run_in_executor(None, _imap_fetch_code_sync, address)
            if code:
                return code
        except Exception:
            pass
        await asyncio.sleep(poll)
        elapsed += poll
    return None


def _imap_list_recent_sync(limit: int = 10) -> list:
    cfg = get_config(reveal=True)
    host = (cfg["imap_host"] or "").strip()
    user = (cfg["imap_user"] or "").strip()
    pwd = (cfg["imap_pass"] or "").strip()
    if not (host and user and pwd):
        return []
    box = imaplib.IMAP4_SSL(host, cfg["imap_port"])
    out = []
    try:
        box.login(user, pwd)
        box.select("INBOX")
        typ, data = box.search(None, "ALL")
        ids = data[0].split() if data and data[0] else []
        for num in reversed(ids[-limit:]):
            typ, msg_data = box.fetch(num, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            msg = _email.message_from_bytes(msg_data[0][1])
            subject = _decode(msg.get("Subject"))
            body = _body_text(msg)
            out.append({
                "from":    _decode(msg.get("From")),
                "to":      _decode(msg.get("To")) or _decode(msg.get("Delivered-To")),
                "subject": subject,
                "date":    _decode(msg.get("Date")),
                "code":    _extract_code(subject, body),
            })
        return out
    finally:
        try:
            box.logout()
        except Exception:
            pass


async def list_recent(limit: int = 10) -> list:
    """Últimas mensagens da caixa catch-all (para testar recebimento + extração)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _imap_list_recent_sync, limit)


def test_imap() -> dict:
    """Verifica login IMAP. Retorna {ok, error, host, port, user, pass_len}."""
    cfg = get_config(reveal=True)
    host = (cfg["imap_host"] or "").strip()
    user = (cfg["imap_user"] or "").strip()
    pwd  = (cfg["imap_pass"] or "").strip()
    info = {"host": host, "port": cfg["imap_port"], "user": user, "pass_len": len(pwd)}
    if not (host and user and pwd):
        return {"ok": False, "error": "Falta host/usuário/senha IMAP", **info}
    try:
        box = imaplib.IMAP4_SSL(host, cfg["imap_port"])
        box.login(user, pwd)
        box.select("INBOX")
        box.logout()
        return {"ok": True, **info}
    except Exception as e:
        return {"ok": False, "error": str(e), **info}
