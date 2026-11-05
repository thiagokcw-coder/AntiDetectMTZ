# -*- coding: utf-8 -*-
"""YouTube pela API oficial (Data API v3) — o único caminho que vale a pena aqui.

POR QUE A API, E SÓ PARA O YOUTUBE
----------------------------------
Todas as outras redes publicam pelo navegador, porque a API delas é paga (X),
exige app aprovado (TikTok, Facebook) ou não existe (Kwai). O YouTube é a
exceção: a Data API v3 é gratuita e a conta comum pode usá-la.

E para o Google isso não é só conveniência, é o conserto da causa. A sessão
dele é a mais difícil de transplantar de todas — vive em DOIS domínios
(`google.com` e `youtube.com`), é cheia de cookies `__Secure-`/`__Host-` com
regras próprias, e ele amarra a sessão ao dispositivo e ao IP com mais rigor
que a Meta. Pela API nada disso existe: não há sessão de navegador para manter
viva, não há incoerência de fingerprint, não há evento de "login de novo
dispositivo" — que é o que queimou conta neste projeto em 26/07.

SEM DEPENDÊNCIA NOVA
--------------------
Nada de `google-api-python-client`: ele arrasta uma árvore grande para o que
aqui são três requisições HTTP. Usa-se o `httpx`, que já vem com o FastAPI.

⚠ SEGREDOS: este módulo lida com `client_secret` e `refresh_token`. Nenhum
deles pode aparecer em log, em retorno de endpoint ou em mensagem de erro — a
mesma regra que vale para os cookies de sessão. `_sem_segredo` existe para isso.
"""
import json
from pathlib import Path

#: O mínimo para publicar, mais a leitura que permite CONFIRMAR em qual canal a
#: autorização caiu. Sem o readonly, o app saberia que recebeu um token e não
#: saberia de quem — e "autorizado" sem dizer de qual canal é rótulo vazio.
ESCOPOS = ("https://www.googleapis.com/auth/youtube.upload",
           "https://www.googleapis.com/auth/youtube.readonly")

AUTORIZACAO = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
UPLOAD = ("https://www.googleapis.com/upload/youtube/v3/videos"
          "?uploadType=resumable&part=snippet,status")
CANAL = "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true"

#: Quanto a cota padrão permite. `videos.insert` custa 1600 unidades e o projeto
#: nasce com 10.000 por dia — ou seja, **seis envios por dia**, não ilimitado.
#: Está aqui, e não só na documentação do Google, porque é um limite que muda o
#: plano de uso e ninguém descobre no meio de uma leva.
CUSTO_UPLOAD = 1600
COTA_PADRAO = 10000


def _sem_segredo(txt: str) -> str:
    """Corta o que não pode sair daqui. Resposta de erro do Google às vezes
    ecoa o que foi enviado, e o que foi enviado inclui o `client_secret`."""
    s = str(txt or "")
    for marca in ("client_secret", "refresh_token", "access_token", "code="):
        i = s.find(marca)
        if i >= 0:
            s = s[:i] + marca + "=<oculto>"
            break
    return s[:400]


def url_de_autorizacao(client_id: str, redirect_uri: str, state: str) -> str:
    """O endereço para onde o usuário vai dar o consentimento. Função PURA.

    `access_type=offline` + `prompt=consent` é o que faz o Google devolver um
    **refresh_token**. Sem os dois, a primeira autorização devolve só um token
    de uma hora e a segunda não devolve refresh nenhum — e aí a publicação
    funcionaria hoje e falharia amanhã, que é o pior jeito de falhar.
    """
    from urllib.parse import urlencode

    return AUTORIZACAO + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(ESCOPOS),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    })


def _cliente(http=None):
    if http is not None:
        return http
    import httpx
    return httpx.Client(timeout=60.0)


def _ok(r):
    """Levanta com a mensagem do Google, sem vazar o que foi enviado."""
    if r.status_code >= 400:
        raise RuntimeError("Google respondeu %s: %s"
                           % (r.status_code, _sem_segredo(getattr(r, "text", ""))))
    return r.json()


def trocar_codigo(client_id: str, client_secret: str, code: str,
                  redirect_uri: str, http=None) -> dict:
    """Troca o `code` do consentimento pelos tokens. Devolve SEM o segredo."""
    r = _cliente(http).post(TOKEN, data={
        "client_id": client_id, "client_secret": client_secret,
        "code": code, "grant_type": "authorization_code",
        "redirect_uri": redirect_uri})
    d = _ok(r)
    if not d.get("refresh_token"):
        # Acontece quando a conta já autorizou antes e o `prompt=consent` não
        # foi enviado. Dizer isto agora é melhor que guardar meio token e
        # descobrir na primeira publicação de amanhã.
        raise RuntimeError(
            "o Google não devolveu refresh_token — revogue o acesso do app em "
            "myaccount.google.com/permissions e autorize de novo")
    return {"refresh_token": d["refresh_token"],
            "access_token": d.get("access_token"),
            "expira_em": int(d.get("expires_in") or 0)}


def renovar(client_id: str, client_secret: str, refresh_token: str,
            http=None) -> dict:
    """Troca o refresh_token por um access_token novo (vale ~1h)."""
    r = _cliente(http).post(TOKEN, data={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token"})
    d = _ok(r)
    return {"access_token": d.get("access_token"),
            "expira_em": int(d.get("expires_in") or 0)}


def canal(access_token: str, http=None) -> dict:
    """Qual canal este token representa. É o que transforma 'autorizado' em
    'autorizado COMO quem', e é a única confirmação honesta que existe aqui."""
    r = _cliente(http).get(CANAL, headers={"Authorization": "Bearer " + access_token})
    d = _ok(r)
    itens = d.get("items") or []
    if not itens:
        return {"id": "", "titulo": "", "aviso": "o token não tem canal associado"}
    s = itens[0].get("snippet") or {}
    return {"id": itens[0].get("id", ""), "titulo": s.get("title", ""),
            "handle": (s.get("customUrl") or "")}


def subir_video(access_token: str, caminho: str, titulo: str,
                descricao: str = "", privacidade: str = "private",
                tags=None, http=None) -> dict:
    """Envia o vídeo. Duas requisições: abre a sessão de upload e manda os bytes.

    ⚠ `privacidade` nasce **"private"** de propósito. É o mesmo espírito do
    `simular` da leva: motor novo estreia sem poder estragar nada. Um upload
    errado que sai público é público — e desfazer não desfaz quem já viu.
    Quem quer "public" diz.
    """
    p = Path(caminho)
    if not p.exists():
        raise ValueError("arquivo não encontrado: %s" % caminho)
    if privacidade not in ("private", "unlisted", "public"):
        raise ValueError("privacidade deve ser private, unlisted ou public")

    corpo = {"snippet": {"title": (titulo or "")[:100],
                         "description": (descricao or "")[:5000],
                         "tags": list(tags or [])},
             "status": {"privacyStatus": privacidade,
                        "selfDeclaredMadeForKids": False}}
    tamanho = p.stat().st_size
    cli = _cliente(http)

    # 1) Abre a sessão resumível. O Google devolve o endereço no cabeçalho.
    r = cli.post(UPLOAD, headers={
        "Authorization": "Bearer " + access_token,
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/*",
        "X-Upload-Content-Length": str(tamanho),
    }, content=json.dumps(corpo))
    if r.status_code >= 400:
        raise RuntimeError("Google recusou abrir o upload (%s): %s"
                           % (r.status_code, _sem_segredo(getattr(r, "text", ""))))
    destino = (r.headers or {}).get("location") or (r.headers or {}).get("Location")
    if not destino:
        raise RuntimeError("o Google aceitou mas não disse para onde enviar "
                           "(sem cabeçalho Location)")

    # 2) Manda os bytes de uma vez. Vídeo de leva é curto; fatiar só faria
    #    sentido para retomar upload interrompido, que ainda não temos.
    with p.open("rb") as f:
        r2 = cli.put(destino, headers={
            "Authorization": "Bearer " + access_token,
            "Content-Type": "video/*",
            "Content-Length": str(tamanho),
        }, content=f.read())
    d = _ok(r2)
    vid = d.get("id") or ""
    return {"id": vid, "url": ("https://youtu.be/" + vid) if vid else "",
            "privacidade": privacidade, "titulo": corpo["snippet"]["title"]}
