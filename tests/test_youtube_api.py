# -*- coding: utf-8 -*-
"""YouTube pela API oficial: OAuth e upload (2026-11-05).

Escolhido depois de o YouTube nao logar por cookie. Para o Google a API nao e
conveniencia, e o conserto da causa: a sessao dele vive em DOIS dominios, e
cheia de cookies `__Secure-`/`__Host-` e amarra forte a dispositivo e IP.

O que este arquivo trava, e por que cada um importa:

  - `access_type=offline` + `prompt=consent` na URL. Sem os dois o Google
    devolve so um token de uma hora, e a publicacao funcionaria HOJE e
    falharia amanha -- o pior jeito de falhar.
  - **Nenhum segredo vaza.** `client_secret` e `refresh_token` nao podem
    aparecer em mensagem de erro; a resposta de erro do Google as vezes ecoa o
    que foi enviado.
  - **O upload nasce `private`.** Mesmo espirito do `simular` da leva: um envio
    errado que sai publico e publico, e desfazer nao desfaz quem ja viu.
  - Token sem `refresh_token` e recusado NA HORA, em vez de guardar meio
    token e quebrar na proxima publicacao.

Sem rede: o cliente HTTP e injetado. Rodar com:

    venv\\Scripts\\python.exe tests\\test_youtube_api.py
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import youtube_api as yt  # noqa: E402

ok = 0


def t(cond, msg):
    global ok
    assert cond, "FALHOU: " + msg
    ok += 1


class Resp:
    def __init__(self, status=200, corpo=None, headers=None, texto=""):
        self.status_code, self._corpo = status, corpo or {}
        self.headers, self.text = headers or {}, texto

    def json(self):
        return self._corpo


class Http:
    """Cliente duble. `respostas` e consumida na ordem das chamadas."""

    def __init__(self, respostas):
        self.respostas, self.chamadas = list(respostas), []

    def _proxima(self, verbo, url, **kw):
        self.chamadas.append({"verbo": verbo, "url": url, **kw})
        return self.respostas.pop(0)

    def post(self, url, **kw):
        return self._proxima("POST", url, **kw)

    def put(self, url, **kw):
        return self._proxima("PUT", url, **kw)

    def get(self, url, **kw):
        return self._proxima("GET", url, **kw)


# ══ A URL do consentimento ═══════════════════════════════════════════════
u = yt.url_de_autorizacao("ID.apps.googleusercontent.com",
                          "http://127.0.0.1:18754/youtube/oauth/callback", "conta1:xyz")
t("access_type=offline" in u,
  "pede acesso offline -- sem isso nao vem refresh_token")
t("prompt=consent" in u,
  "e forca o consentimento: a SEGUNDA autorizacao nao devolveria refresh sem isso")
t("youtube.upload" in u and "youtube.readonly" in u,
  "pede publicar E ler: sem o readonly nao da para dizer de QUAL canal e o token")
t("state=conta1" in u, "o state amarra a volta a esta conta")
t("127.0.0.1" in u, "volta para o proprio app")


# ══ Troca do codigo ══════════════════════════════════════════════════════
h = Http([Resp(200, {"refresh_token": "RT", "access_token": "AT", "expires_in": 3599})])
r = yt.trocar_codigo("cid", "segredo", "code123", "http://x/cb", http=h)
t(r["refresh_token"] == "RT" and r["access_token"] == "AT", "devolve os dois tokens")
t(h.chamadas[0]["data"]["grant_type"] == "authorization_code", "com o grant certo")

# sem refresh_token: recusa NA HORA, com o que fazer escrito
h = Http([Resp(200, {"access_token": "AT", "expires_in": 3599})])
try:
    yt.trocar_codigo("cid", "segredo", "c", "http://x/cb", http=h)
    t(False, "sem refresh_token deveria recusar")
except RuntimeError as e:
    t("myaccount.google.com/permissions" in str(e),
      "e diz COMO resolver (revogar e reautorizar): " + str(e)[:60])


# ══ NENHUM SEGREDO VAZA ══════════════════════════════════════════════════
# O Google as vezes ecoa o enviado na resposta de erro.
eco = "erro: client_secret=SEGREDO-REAL-AQUI&code=123"
h = Http([Resp(400, {}, texto=eco)])
try:
    yt.trocar_codigo("cid", "SEGREDO-REAL-AQUI", "c", "http://x/cb", http=h)
    t(False, "400 deveria levantar")
except RuntimeError as e:
    t("SEGREDO-REAL-AQUI" not in str(e),
      "o segredo NAO aparece no erro: " + str(e)[:70])
    t("400" in str(e), "mas o codigo de status aparece, senao nao da para diagnosticar")

t("RT-SECRETO" not in yt._sem_segredo("x refresh_token=RT-SECRETO y"),
  "refresh_token tambem e cortado")
t(yt._sem_segredo("falha comum sem segredo") == "falha comum sem segredo",
  "e texto sem segredo passa inteiro")


# ══ Renovar e identificar o canal ════════════════════════════════════════
h = Http([Resp(200, {"access_token": "AT2", "expires_in": 3599})])
t(yt.renovar("cid", "seg", "RT", http=h)["access_token"] == "AT2", "renova o acesso")
t(h.chamadas[0]["data"]["grant_type"] == "refresh_token", "pelo refresh")

h = Http([Resp(200, {"items": [{"id": "UC123",
                                "snippet": {"title": "Uncensored Brasil",
                                            "customUrl": "@uncensoredbrasil"}}]})])
c = yt.canal("AT", http=h)
t(c["titulo"] == "Uncensored Brasil" and c["id"] == "UC123",
  "diz de QUAL canal e o token -- 'autorizado' sem isso e rotulo vazio")
t("Bearer AT" in h.chamadas[0]["headers"]["Authorization"], "autenticado pelo token")

h = Http([Resp(200, {"items": []})])
t(yt.canal("AT", http=h)["aviso"], "token sem canal associado vira aviso, nao erro mudo")


# ══ O UPLOAD ═════════════════════════════════════════════════════════════
tmp = pathlib.Path(tempfile.mkdtemp())
video = tmp / "clipe.mp4"
video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 900)

h = Http([Resp(200, {}, headers={"location": "https://upload.googleapis.com/sessao/abc"}),
          Resp(200, {"id": "VID123"})])
r = yt.subir_video("AT", str(video), "Meu título", "descrição", http=h)
t(r["id"] == "VID123" and r["url"] == "https://youtu.be/VID123", "devolve id e link")
t(r["privacidade"] == "private",
  "e nasce PRIVADO: envio errado que sai publico nao se desfaz")
abre, manda = h.chamadas
t(abre["headers"]["X-Upload-Content-Length"] == str(video.stat().st_size),
  "declara o tamanho ao abrir a sessao resumivel")
t(json.loads(abre["content"])["status"]["privacyStatus"] == "private",
  "a privacidade vai no corpo, nao so no retorno")
t(manda["url"] == "https://upload.googleapis.com/sessao/abc",
  "e os bytes vao para onde o Google mandou, nunca para uma URL montada aqui")

# publico so quando pedido
h = Http([Resp(200, {}, headers={"Location": "https://u/g"}), Resp(200, {"id": "V2"})])
r = yt.subir_video("AT", str(video), "t", privacidade="public", http=h)
t(r["privacidade"] == "public", "public quando pedido explicitamente")

# privacidade invalida e recusada antes de qualquer requisicao
h = Http([])
try:
    yt.subir_video("AT", str(video), "t", privacidade="semi", http=h)
    t(False, "privacidade invalida deveria recusar")
except ValueError:
    t(not h.chamadas, "e sem gastar requisicao nenhuma")

# arquivo que nao existe idem
try:
    yt.subir_video("AT", str(tmp / "nao_existe.mp4"), "t", http=Http([]))
    t(False, "arquivo ausente deveria recusar")
except ValueError as e:
    t("não encontrado" in str(e), "dizendo que nao achou o arquivo")

# o Google aceita mas nao diz para onde enviar
h = Http([Resp(200, {}, headers={})])
try:
    yt.subir_video("AT", str(video), "t", http=h)
    t(False, "sem Location deveria levantar")
except RuntimeError as e:
    t("Location" in str(e), "e o erro nomeia o cabecalho que faltou")

# titulo e descricao sao cortados nos limites do YouTube
h = Http([Resp(200, {}, headers={"location": "u"}), Resp(200, {"id": "V"})])
yt.subir_video("AT", str(video), "T" * 300, "D" * 9000, http=h)
corpo = json.loads(h.chamadas[0]["content"])
t(len(corpo["snippet"]["title"]) == 100, "titulo cortado em 100")
t(len(corpo["snippet"]["description"]) == 5000, "descricao cortada em 5000")


# ══ A cota, que muda o plano de uso ══════════════════════════════════════
# ⚠ Este numero ja esteve ERRADO aqui (dizia 6, do modelo antigo de 1600
# unidades por insert). Conferido na documentacao em 05/11: `videos.insert` tem
# balde proprio de 100/dia, custo 1 por chamada. A diferenca entre 6 e 100 muda
# o plano de uso inteiro, entao o numero fica travado por assercao.
t(yt.UPLOADS_POR_DIA == 100,
  "a cota padrao da CEM envios por dia (balde proprio do videos.insert)")
t(yt.CUSTO_UPLOAD == 1, "cada envio custa 1 do balde dele, nao 1600")
t(yt.COTA_OUTROS_ENDPOINTS == 10000,
  "os demais endpoints dividem 10.000 unidades, num balde separado")

print("OK — %d assercoes" % ok)
