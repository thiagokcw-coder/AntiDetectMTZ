"""
comportamento.py -- o que a conta faz ALEM de seguir.

MOTIVO (medido em 2026-08-21 no nosso proprio banco, nao suposto):

  * 6.894 acoes no historico e **um verbo so**: `friendships/create`. Zero
    curtidas, zero unfollows, zero buscas, zero stories.
  * 6.816 intervalos entre follows, 99,9% deles entre 7 e 30 segundos. Nove
    passaram de 30s. Gente real tem cauda: segue tres seguidas, some por uns
    minutos, volta.
  * A conta trabalhava as 02h no mesmo ritmo das 15h -- 35 horas ininterruptas.
  * E a pior: o corpo do follow declara `container_module=profile` e um referer
    da pagina do perfil -- uma tela que a sessao **nunca carregou**. A Meta tem
    os dois lados (o que a sessao leu e o que ela escreveu).

Este modulo NAO muda como se segue -- isso esta provado e funcionando desde o fix
do `fb_dtsg`. Ele muda o que acontece em volta:

  ritmo    intervalo trimodal com cauda longa e MEDIA preservada, bloco de turno
           de tamanho variavel, janela de sono opcional
  leitura  a sessao de fato le o perfil antes de seguir, passa pelo feed, pelo
           explorar, faz uma busca
  escrita  curtida ocasional, feita CLICANDO na interface -- nunca por uma rota
           montada na mao (foi rota adivinhada que custou dias no 404 do follow)

Regra de ouro do modulo: **nada aqui pode derrubar a campanha**. Toda rotina que
toca a pagina e best-effort e engole a propria excecao. Ruido de comportamento e
acessorio; seguir e o trabalho.
"""
import asyncio
import hashlib
import random
import time
from urllib.parse import quote

import human
import spider

IG = spider.IG


# ══════════════════════════════ RITMO ══════════════════════════════

# (nome, peso, multiplicador minimo, multiplicador maximo) sobre o intervalo base.
# O modo "sumiu" e o que cria a cauda que a nossa distribuicao nao tinha.
_MODOS = (
    ("varredura", 0.60, 0.30, 0.70),    # engatilhado, um atras do outro
    ("leitura",   0.27, 0.80, 1.50),    # parou para olhar o perfil
    ("distraido", 0.10, 1.60, 3.50),    # foi ver outra coisa
    ("sumiu",     0.03, 3.00, 10.00),   # levantou da cadeira
)


def _media_dos_modos() -> float:
    return sum(peso * (a + b) / 2 for _, peso, a, b in _MODOS)


# Normaliza para a MEDIA continuar sendo o intervalo configurado. Sem isto a cauda
# longa cobraria throughput; a escolha do usuario foi "so o necessario", ou seja:
# o padrao muda, o volume nao. Se alguem mexer na tabela acima, isto se reajusta.
_NORMALIZA = 1.0 / _media_dos_modos()


def intervalo_humano(base_ms, rng=None):
    """Segundos ate o proximo follow, e o nome do modo sorteado (para o log).

    Substitui o jitter uniforme de +-50%, que produzia a caixa de 7-30s medida no
    banco. A media continua sendo `base_ms`."""
    r = rng or random
    base = max(0.3, float(base_ms or 18000) / 1000.0)
    x = r.random()
    acumulado = 0.0
    escolhido = _MODOS[0]
    for modo in _MODOS:
        acumulado += modo[1]
        if x < acumulado:
            escolhido = modo
            break
    nome, _peso, a, b = escolhido
    fator = a + r.random() * (b - a)
    return round(base * fator * _NORMALIZA, 2), nome


# ── Estrutura de SESSAO: rajada curta + pausa ──────────────────────────────
# Gente nao segue a cada 18 segundos a noite inteira: segue dois ou tres seguidos,
# para um tempo, volta. A rajada e CURTA de proposito -- sequencia longa e sinal de
# bot (decisao do usuario, 2026-08-21).
#
# As constantes abaixo PRESERVAM A MEDIA. Com rajada media de 3,5 follows, gap
# interno medio de 0,40x e pausa media de 2,5x o intervalo base:
#     ((3,5 - 1) * 0,40 + 2,5) / 3,5  =  (1,0 + 2,5) / 3,5  =  1,0
# ou seja, o tempo por follow continua sendo exatamente o intervalo configurado --
# o que muda e a FORMA da chegada, nao o volume.
_GAP_MIN, _GAP_MAX = 0.25, 0.55     # multiplicador do base DENTRO da rajada
_PAUSA_FATOR = 2.5                  # multiplicador medio da pausa entre rajadas
_PAUSA_NORMAL = (0.50, 1.20)        # variacao comum da pausa
_PAUSA_LONGA = (3.0, 8.0)           # "largou o celular" -- raro
_PAUSA_LONGA_PCT = 0.05

# Normaliza a pausa para a media do multiplicador ser 1 (senao as pausas longas
# puxariam o intervalo medio para cima e o volume cairia sem ninguem pedir).
_PAUSA_NORM = 1.0 / ((1 - _PAUSA_LONGA_PCT) * sum(_PAUSA_NORMAL) / 2
                     + _PAUSA_LONGA_PCT * sum(_PAUSA_LONGA) / 2)


def tamanho_da_rajada(minimo=2, maximo=5, rng=None) -> int:
    """Quantos follows seguidos antes da proxima pausa."""
    r = rng or random
    a, b = int(minimo or 2), int(maximo or 5)
    if a > b:
        a, b = b, a
    return r.randint(max(1, a), max(1, b))


def gap_da_rajada(base_ms, rng=None) -> float:
    """Intervalo curto entre follows da MESMA rajada."""
    r = rng or random
    base = max(0.3, float(base_ms or 18000) / 1000.0)
    return round(base * r.uniform(_GAP_MIN, _GAP_MAX), 2)


def pausa_entre_rajadas(base_ms, rng=None):
    """Pausa depois de fechar uma rajada. Devolve (segundos, longa?)."""
    r = rng or random
    base = max(0.3, float(base_ms or 18000) / 1000.0)
    longa = r.random() < _PAUSA_LONGA_PCT
    faixa = _PAUSA_LONGA if longa else _PAUSA_NORMAL
    m = r.uniform(*faixa) * _PAUSA_NORM
    return round(base * _PAUSA_FATOR * m, 2), longa


# ── RAMPA: teto diario que cresce com a IDADE da conta ─────────────────────
#
# MEDIDO em 2026-08-22, cruzando quem caiu com quem sobreviveu. O que separa os dois
# grupos nao e o volume TOTAL -- e o volume no PRIMEIRO dia de vida da conta:
#
#   caiu     10122.cap.martim.rs  dia1 243   ·  10122.capmartim  dia1 279 (banida)
#   viva     cap.martim2026       dia1  58   ·  cap.martim.rs.2026  dia1 100
#
# Das quatro contas que estrearam com ~300, duas cairam. As duas que comecaram com
# 58 e 100 estao vivas -- e a que fez MAIS follows de todas (1.697, pico de 786/dia)
# tambem esta, porque so nao estreou correndo. Volume total nao explica; estreia sim.
#
# Onde a evidencia esta e onde nao esta, para os numeros nao parecerem mais firmes do
# que sao:
#   dia 1  -- e AQUI que o dado separa. Seguro observado <= 100 (58 e 100 vivas);
#             zona de queda observada >= 243. Entre 100 e 243 ninguem mediu. O degrau
#             fica em 100, o maior valor com sobrevivencia observada.
#   dia 2+ -- NAO separa: os dois grupos fizeram 336-611 no dia 2, e dos que fizeram
#             isso alguns cairam e outros nao. Entao os degraus seguintes sao margem
#             de seguranca, nao medicao -- por isso sobem rapido e somem no dia 4.
#
# n pequeno: 2 quedas. E correlacao com mecanismo plausivel (conta recem-nascida e
# vazia despejando volume), nao prova.
#
# (dias_de_vida_ate, teto_do_dia). Depois do ultimo degrau, sem teto -- o freio volta
# a ser o intervalo.
RAMPA = ((1, 100), (2, 250), (3, 450))


def teto_por_idade(dias, rampa=None):
    """Teto de follows do dia para uma conta com `dias` de vida (1 = nasceu hoje).

    Devolve 0 quando nao ha teto (conta madura). `dias` desconhecido (None) e
    tratado como conta NOVA de proposito: errar para o lado do teto baixo custa
    volume, errar para o outro custa a conta."""
    degraus = rampa or RAMPA
    if dias is None:
        return degraus[0][1]
    try:
        d = int(dias)
    except (TypeError, ValueError):
        return degraus[0][1]
    if d < 1:
        d = 1
    for ate, teto in degraus:
        if d <= ate:
            return teto
    return 0


def descrever_rampa(dias, teto) -> str:
    """Linha para o log explicar por que esta conta tem (ou nao tem) teto hoje."""
    if not teto:
        return f"conta com {dias} dia(s) — fora da rampa, sem teto diário"
    return f"conta com {dias} dia(s) de vida — rampa limita a {teto} follows hoje"


def tamanho_do_bloco(centro, variacao_pct=25, rng=None) -> int:
    """O turno de cada conta deixa de terminar sempre no mesmo numero redondo.

    Com centro 100 e variacao 25%, o turno vira algo entre 75 e 125. `centro` 0
    (sem rodizio) continua 0."""
    if not centro or int(centro) <= 0:
        return 0
    r = rng or random
    p = max(0.0, min(0.9, float(variacao_pct or 0) / 100.0))
    n = int(round(int(centro) * r.uniform(1.0 - p, 1.0 + p)))
    return max(1, n)


def desvio_da_conta(chave, max_min=70) -> int:
    """Minutos de desvio no horario de dormir, ESTAVEIS por conta.

    Sem isto a frota inteira dormiria e acordaria no mesmo minuto -- trocariamos
    uma assinatura (ninguem dorme) por outra (todos dormem juntos). Deriva do nome
    da conta, entao a mesma conta tem sempre o mesmo horario."""
    if not chave:
        return 0
    d = hashlib.sha1(str(chave).encode("utf-8")).digest()
    return int.from_bytes(d[:4], "big") % (max_min * 2 + 1) - max_min


def _hora_fracionada(agora=None) -> float:
    t = time.localtime(agora if agora is not None else time.time())
    return t.tm_hour + t.tm_min / 60.0 + t.tm_sec / 3600.0


def dentro_do_sono(inicio_h, fim_h, agora=None, desvio_min=0) -> bool:
    """True se AGORA esta dentro da janela de sono. Cobre janela que cruza a
    meia-noite (23h -> 7h)."""
    try:
        ini = (float(inicio_h) + float(desvio_min) / 60.0) % 24.0
        fim = (float(fim_h) + float(desvio_min) / 60.0) % 24.0
    except (TypeError, ValueError):
        return False
    if abs(ini - fim) < 1e-9:
        return False                       # janela vazia = desligada
    h = _hora_fracionada(agora)
    if ini < fim:
        return ini <= h < fim
    return h >= ini or h < fim             # cruza a meia-noite


def segundos_ate_acordar(inicio_h, fim_h, agora=None, desvio_min=0) -> float:
    """Quanto falta para o fim da janela de sono. 0 se nao esta dormindo."""
    if not dentro_do_sono(inicio_h, fim_h, agora, desvio_min):
        return 0.0
    fim = (float(fim_h) + float(desvio_min) / 60.0) % 24.0
    h = _hora_fracionada(agora)
    falta = fim - h
    if falta <= 0:
        falta += 24.0
    return round(falta * 3600.0, 1)


# ══════════════════════════════ LEITURAS ══════════════════════════════
# Tudo aqui e GET. Nenhuma leitura muda estado na conta.

async def ver_perfil_completo(page, username, *, rng=None) -> dict:
    """Carrega DE VERDADE a pagina do perfil.

    E o que torna verdadeira a declaracao `container_module=profile` que o nosso
    POST ja faz -- e, de quebra, o fetch do follow passa a sair de dentro da
    pagina certa, com o referer real batendo com o declarado."""
    r = rng or random
    t0 = time.monotonic()
    try:
        await page.goto(IG + "/" + username + "/", wait_until="domcontentloaded",
                        timeout=30000)
    except Exception as e:
        return {"ok": False, "tipo": "completo", "erro": str(e),
                "segundos": round(time.monotonic() - t0, 2)}
    try:
        await human.idle_browse(page, r.uniform(2.0, 6.5))
    except Exception:
        pass
    return {"ok": True, "tipo": "completo", "segundos": round(time.monotonic() - t0, 2)}


async def ver_perfil_leve(page, username) -> dict:
    """Le o perfil pela mesma requisicao que a pagina dispara (`web_profile_info`),
    sem baixar a pagina inteira.

    Existe porque a versao completa custa alguns MB por follow -- num chip 4G isso
    vira gigabytes por dia. Esta aqui pesa dezenas de KB e produz a correlacao que
    importa: a sessao LEU aquele perfil antes de escrever nele.

    Usa `_resolve_via_web_profile_info` DIRETO, e nao o `_resolve_user`: aquele tem
    cadeia de fallback que, no ultimo recurso, **navega para a pagina inteira** (~3MB)
    e dorme 2s de verdade. Para resolver um alvo de extracao isso vale a pena; para um
    ruido de contexto, nao -- transformaria a leitura "leve" numa pesada sem avisar,
    justamente o que o orcamento de 30 GB/mes nao permite. Falhou, deixa pra la:
    isto e acessorio, o follow acontece de qualquer jeito."""
    t0 = time.monotonic()
    try:
        uid, info = await spider._resolve_via_web_profile_info(page, username, None)
    except Exception as e:
        return {"ok": False, "tipo": "leve", "erro": str(e),
                "segundos": round(time.monotonic() - t0, 2)}
    return {"ok": bool(uid), "tipo": "leve", "id": uid, "info": info,
            "segundos": round(time.monotonic() - t0, 2)}


async def espiar_perfil(page, username) -> dict:
    """Abre o perfil de alguem e NAO segue.

    Existe por causa de uma assinatura medida em 2026-08-21: **100% dos perfis que a
    conta lia, ela seguia**. Conversao perfeita leitura->follow e padrao por si so --
    gente real abre muito perfil e segue poucos. Esta acao quebra exatamente isso, e
    custa o mesmo que a leitura leve (~50KB), entao cabe no plano de 30 GB."""
    t0 = time.monotonic()
    if not username:
        return {"ok": False, "tipo": "espiar", "erro": "sem perfil para espiar",
                "segundos": 0.0}
    try:
        uid, _info = await spider._resolve_via_web_profile_info(page, username, None)
    except Exception as e:
        return {"ok": False, "tipo": "espiar", "usuario": username, "erro": str(e),
                "segundos": round(time.monotonic() - t0, 2)}
    return {"ok": bool(uid), "tipo": "espiar", "usuario": username,
            "segundos": round(time.monotonic() - t0, 2)}


async def passar_pelo_feed(page, *, rng=None) -> dict:
    """Abre o feed e rola um pouco. E a tela que qualquer pessoa abre, e carrega-la
    faz a sessao emitir o trafego normal do Instagram (bandeja de stories, timeline)
    sem que a gente precise adivinhar nenhuma rota."""
    r = rng or random
    t0 = time.monotonic()
    try:
        await page.goto(IG + "/", wait_until="domcontentloaded", timeout=30000)
        await human.idle_browse(page, r.uniform(4.0, 12.0))
    except Exception as e:
        return {"ok": False, "tipo": "feed", "erro": str(e),
                "segundos": round(time.monotonic() - t0, 2)}
    return {"ok": True, "tipo": "feed", "segundos": round(time.monotonic() - t0, 2)}


async def explorar(page, *, rng=None) -> dict:
    """Abre o Explorar e rola. Outra tela banal, outro padrao de requisicao."""
    r = rng or random
    t0 = time.monotonic()
    try:
        await page.goto(IG + "/explore/", wait_until="domcontentloaded", timeout=30000)
        await human.idle_browse(page, r.uniform(3.0, 10.0))
    except Exception as e:
        return {"ok": False, "tipo": "explorar", "erro": str(e),
                "segundos": round(time.monotonic() - t0, 2)}
    return {"ok": True, "tipo": "explorar", "segundos": round(time.monotonic() - t0, 2)}


# Termos de busca coerentes com a persona (Porto Alegre / RS) -- o geoip dos perfis
# esta fixado la, entao buscar "brooklyn pizza" seria a incoerencia oposta.
TERMOS_BUSCA = [
    "porto alegre", "gremio", "inter", "poa", "rio grande do sul", "canoas",
    "gramado", "churrasco", "praia", "viagem", "musica", "receita", "treino",
    "cachorro", "cafe", "fim de semana", "show", "futebol", "moda", "carros",
]

# Quantos termos recentes ficam PROIBIDOS antes de poder repetir. Sorteio uniforme
# repete por acaso (aniversario), e repetir a mesma busca e o tipo de padrao que o
# usuario quer evitar -- ele pediu justamente para conseguir ver e prevenir isso.
_TERMOS_SEM_REPETIR = 6


async def buscar(page, termo=None, *, rng=None) -> dict:
    """Faz uma busca. Usa a rota `topsearch`, que o Spider ja usa para resolver
    perfis -- rota estabelecida nas nossas sessoes, nao um chute."""
    r = rng or random
    q = termo or r.choice(TERMOS_BUSCA)
    t0 = time.monotonic()
    try:
        await spider._api_get(
            page, IG + "/web/search/topsearch/?context=blended&query=" + quote(q),
            IG + "/")
    except Exception as e:
        return {"ok": False, "tipo": "busca", "erro": str(e),
                "segundos": round(time.monotonic() - t0, 2)}
    return {"ok": True, "tipo": "busca", "termo": q,
            "segundos": round(time.monotonic() - t0, 2)}


# Quanto cada acao pesa no sorteio da pausa.
#
# Os pesos sao decisao de ORCAMENTO tanto quanto de comportamento: espiar um perfil e
# buscar custam ~50KB cada, enquanto o feed custa ~2,5MB e o explorar ~4MB (grade de
# imagens). Num plano de 30 GB/mes compartilhado com o PC, encher a pausa de conteudo
# pesado estoura o mes; encher de acoes BARATAS nao.
#
# `espiar` leva o maior peso porque resolve a assinatura mais grave que a medicao de
# 2026-08-21 revelou: a conta seguia 100% dos perfis que lia.
_LEITURAS = (("espiar", 0.50), ("busca", 0.35), ("feed", 0.12), ("explorar", 0.03))

# Acoes BARATAS (~50KB). A ULTIMA coisa antes de um follow sai sempre daqui: ela
# acontece em toda pausa, entao nao pode custar dados de verdade.
_BARATAS = ("espiar", "busca")

# Segundos reservados no FIM da pausa para a volta. O silencio vem antes, a atividade
# depois. Sem isto a acao acontecia no INICIO da pausa e o que colava no proximo
# follow eram dezenas de segundos de silencio -- ou seja "seguir, parar, seguir", que
# o usuario identificou como padrao obvio (2026-08-21).
_RESERVA_VOLTA = (9.0, 18.0)

# Quantos follows da rajada "pedem" uma acao a mais na volta.
#
# Medido em 2026-08-21: amarrar as acoes as PAUSAS fazia a densidade depender do
# tamanho da rajada -- com 2-5 saia 1 acao a cada 2,4 follows, com 6-11 a cada 5,7.
# O usuario corrigiu que o incomodo era "a falta de acoes apos", nao o tamanho da
# rajada. Entao a volta passa a escalar com o trabalho que a precedeu: rajada longa,
# volta mais rica -- e a densidade deixa de depender de como a rajada esta configurada.
_FOLLOWS_POR_ACAO_EXTRA = 3

# Custo estimado de cada leitura, em MB. Usado so para a previsao na tela -- o
# numero que vale e o do MedidorDeDados, que conta os bytes de verdade.
CUSTO_MB = {"leve": 0.05, "pagina": 3.0, "feed": 2.5, "explorar": 4.0, "busca": 0.05}


def sortear_leitura(rng=None, so_baratas=False) -> str:
    """Sorteia a acao da pausa. `so_baratas=True` restringe as opcoes as de ~50KB --
    usado na ULTIMA acao antes do follow, que acontece toda vez."""
    r = rng or random
    opcoes = [(n, p) for n, p in _LEITURAS if (not so_baratas or n in _BARATAS)]
    total = sum(p for _, p in opcoes) or 1.0
    x = r.random() * total
    acumulado = 0.0
    for nome, peso in opcoes:
        acumulado += peso
        if x < acumulado:
            return nome
    return opcoes[0][0]


async def fazer_leitura(page, nome, *, rng=None, termo=None, usuario=None) -> dict:
    if nome == "feed":
        return await passar_pelo_feed(page, rng=rng)
    if nome == "explorar":
        return await explorar(page, rng=rng)
    if nome == "busca":
        return await buscar(page, termo, rng=rng)
    if nome == "espiar":
        return await espiar_perfil(page, usuario)
    return {"ok": False, "tipo": nome, "erro": "leitura desconhecida", "segundos": 0.0}


# ══════════════════════════════ NARRACAO ══════════════════════════════
# O usuario pediu para "saber o que esta sendo feito para prevenir comportamento
# repetitivo". Entao o log deixa de ser so narracao e vira INSTRUMENTO: cada pausa
# diz o que aconteceu nela -- inclusive quando nao aconteceu nada, que e o caso mais
# comum e era exatamente o silencio que fazia o terminal parecer morto.

def descrever_leitura_de_perfil(ctx) -> str:
    """Frase curta do que precedeu o follow. Vai INLINE na linha do follow, para nao
    dobrar o volume do log."""
    c = ctx or {}
    tipo, ok = c.get("tipo"), c.get("ok")
    if tipo == "completo":
        return "abriu a página" if ok else "tentou abrir a página, falhou"
    if tipo == "leve":
        if c.get("reaproveitada"):
            return "perfil já lido pelos filtros"
        return "leu o perfil" if ok else "não conseguiu ler o perfil"
    if tipo == "sorteio":
        return "sem leitura antes (sorteio)"
    return ""


def frase_da_acao(a) -> str:
    """Uma acao de pausa em palavras."""
    a = a or {}
    tipo, dur = a.get("tipo"), float(a.get("segundos") or 0)
    if tipo == "espiar":
        return "espiou o perfil de @" + str(a.get("usuario")) + " sem seguir"
    if tipo == "busca":
        return 'buscou "' + str(a.get("termo")) + '"'
    if tipo == "feed":
        return f"abriu o feed e rolou por {dur:.0f}s"
    if tipo == "explorar":
        return f"foi ao explorar e rolou por {dur:.0f}s"
    return str(tipo or "?")


def descrever_pausa(segundos, acoes=None) -> str:
    """O que a conta fez na pausa. Lista vazia = ficou parada, e isso PRECISA
    aparecer: sem narrar a ociosidade o log fica mudo justamente no tempo em que mais
    tempo passa.

    O rotulo diz "voltou" de proposito: as acoes acontecem no FIM da pausa, entao a
    linha descreve a RETOMADA, nao o comeco do descanso."""
    cab = f"⏸ pausa de {int(round(float(segundos or 0)))}s — "
    tentadas = [a for a in (acoes or []) if a]
    feitas = [a for a in tentadas if a.get("ok")]
    if feitas:
        return cab + "voltou: " + " · depois ".join(frase_da_acao(a) for a in feitas)
    if tentadas:
        # Tentou e nao conseguiu -- dizer "parada" aqui seria mentira, e mentira no
        # log ja custou caro neste projeto.
        return cab + "voltou, mas as ações não completaram"
    return cab + "parada, sem atividade"


# ══════════════════════════════ ESCRITA ══════════════════════════════

# Rotulos do botao de curtir em pt-BR e en. A pagina roda em pt-BR (o geoip esta
# fixado em Porto Alegre), mas o IG as vezes serve em ingles.
_ALVOS_CURTIR = (
    'section svg[aria-label="Curtir"]',
    'section svg[aria-label="Like"]',
    'article svg[aria-label="Curtir"]',
    'article svg[aria-label="Like"]',
)


# Botao "Seguir" na pagina do perfil. `:text-is` e casamento EXATO -- com
# `:has-text` o seletor pegaria tambem "Seguindo"/"Solicitado" e clicaria para
# DESFAZER o follow, que seria um estrago silencioso.
_ALVOS_SEGUIR = (
    'button:text-is("Seguir")',
    'button:text-is("Follow")',
    'button:text-is("Seguir de volta")',
    'button:text-is("Follow Back")',
)
# Como o Instagram confirma que deu certo (o botao troca de rotulo).
_SEGUINDO = ('button:text-is("Seguindo")', 'button:text-is("Following")')
_SOLICITADO = ('button:text-is("Solicitado")', 'button:text-is("Requested")')


async def _rotulo_visivel(page, seletores, timeout=1200) -> bool:
    for sel in seletores:
        try:
            if await page.locator(sel).first.is_visible(timeout=timeout):
                return True
        except Exception:
            continue
    return False


async def seguir_clicando(page, username=None, *, rng=None) -> dict:
    """Segue CLICANDO no botao da interface, na pagina do perfil ja aberta.

    Por que isto importa: quando o clique e real, quem monta a requisicao e o
    proprio Instagram -- com `nav_chain`, telemetria de ponteiro, `fb_dtsg` e todo
    o contexto que a nossa versao por `fetch` nunca tem. E o unico caminho que
    produz um follow indistinguivel de um humano.

    Nao e o caminho padrao porque exige a pagina inteira (~3MB) e o plano e de
    30 GB/mes. Mas a sessao JA carrega a pagina em parte dos follows
    (`verPerfilCompletoPct`) -- nesses, clicar sai **de graca**.

    Falha e sempre tolerada: devolve {ok: False, motivo} e o motor cai no fetch."""
    if await _rotulo_visivel(page, _SEGUINDO + _SOLICITADO, timeout=600):
        return {"ok": False, "motivo": "ja seguimos esta conta"}
    alvo = None
    for sel in _ALVOS_SEGUIR:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1200):
                alvo = loc
                break
        except Exception:
            continue
    if alvo is None:
        return {"ok": False, "motivo": "botao Seguir nao encontrado na pagina"}
    try:
        await human.click_locator(page, alvo)
    except Exception as e:
        return {"ok": False, "motivo": "falha ao clicar (" + str(e) + ")"}
    # Confirma pelo proprio rotulo do botao: e a leitura mais honesta que existe,
    # porque e a mesma que um humano faria olhando a tela.
    await human.delay(1.0, 2.4)
    if await _rotulo_visivel(page, _SOLICITADO):
        return {"ok": True, "requested": True, "via": "clique"}
    if await _rotulo_visivel(page, _SEGUINDO):
        return {"ok": True, "requested": False, "via": "clique"}
    return {"ok": False, "motivo": "cliquei, mas o botao nao confirmou"}


# Pontos por movimento de mouse. O `human._move_bezier` usa 14 e ainda faz dois
# `page.evaluate` para guardar a posicao do cursor -- 16 idas ao navegador por
# movimento. Isso e barato numa pagina estatica (e por isso serve no aquecimento),
# mas o Instagram e React pesado: CADA evento de ponteiro dispara handlers, e o
# round-trip fica caro.
#
# MEDIDO em 2026-08-22: com 1-3 movimentos + scroll dava ate 56 idas por follow e
# levava o trabalho de ~2s para ~20s -- o dobro do intervalo configurado virando
# tempo morto. Com 4 pontos e no maximo 2 movimentos, o sinal continua existindo
# (ha eventos de ponteiro de verdade, em curva) e o custo cai uma ordem de grandeza.
_PONTOS_MOUSE = 4


async def mexer_na_pagina(page, *, rng=None, estado=None) -> dict:
    """Move o mouse e as vezes rola a pagina. NAO faz requisicao nenhuma.

    Existe porque a sessao passava 30 minutos com a pagina do Instagram carregada,
    o JS dela rodando, e gerava **zero** evento de ponteiro enquanto POSTs saiam.
    O JS do Instagram observa esses eventos; a ausencia total deles e o lado
    cliente da mesma anomalia que o `nav_chain` vazio e no lado servidor.

    Custa zero dados. `estado` guarda a posicao do cursor entre chamadas, o que
    evita os dois `page.evaluate` por movimento que o `human.move_to` faz."""
    r = rng or random
    pos = estado if isinstance(estado, dict) else {}
    eventos = 0
    try:
        vp = page.viewport_size or {"width": 1280, "height": 720}
        x0 = pos.get("x", vp["width"] * 0.5)
        y0 = pos.get("y", vp["height"] * 0.5)
        for _ in range(r.randint(1, 2)):
            x1 = r.uniform(vp["width"] * 0.15, vp["width"] * 0.85)
            y1 = r.uniform(vp["height"] * 0.15, vp["height"] * 0.85)
            # Bezier curto: curva de verdade, poucos pontos.
            cx = (x0 + x1) / 2 + r.uniform(-60, 60)
            cy = (y0 + y1) / 2 + r.uniform(-60, 60)
            for i in range(1, _PONTOS_MOUSE + 1):
                t = i / _PONTOS_MOUSE
                await page.mouse.move((1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t * t * x1,
                                      (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t * t * y1)
                eventos += 1
            x0, y0 = x1, y1
            await asyncio.sleep(r.uniform(0.05, 0.2))
        if r.random() < 0.35:
            await page.mouse.wheel(0, r.randint(140, 420))
            eventos += 1
        pos["x"], pos["y"] = x0, y0
    except Exception:
        pass          # regra de ouro: ruido nunca derruba a campanha
    return {"ok": eventos > 0, "eventos": eventos}


async def curtir_no_perfil(page, *, rng=None, log=None) -> dict:
    """Curte um post do perfil aberto, CLICANDO na interface.

    Deliberadamente NAO montamos a requisicao de like na mao. Foi rota adivinhada
    que custou dias no 404 do follow; clicando, quem monta a requisicao e o proprio
    Instagram -- com fb_dtsg, nav_chain e container_module corretos, de graca.

    Exige que a pagina JA esteja no perfil (chame depois de ver_perfil_completo).
    Falha e sempre tolerada: devolve {ok: False, motivo}."""
    r = rng or random
    try:
        posts = page.locator('a[href*="/p/"]')
        n = await posts.count()
        if not n:
            return {"ok": False, "motivo": "o perfil nao tem post visivel"}
        alvo = posts.nth(r.randint(0, min(n - 1, 5)))
        await human.click_locator(page, alvo)
        await human.delay(1.2, 3.0)
    except Exception as e:
        return {"ok": False, "motivo": "nao consegui abrir o post (" + str(e) + ")"}

    curtiu = False
    erro = ""
    try:
        for sel in _ALVOS_CURTIR:
            loc = page.locator(sel).first
            try:
                if await loc.is_visible(timeout=1200):
                    await human.click_locator(page, loc)
                    curtiu = True
                    break
            except Exception:
                continue
        await human.delay(0.8, 2.2)
    except Exception as e:
        erro = str(e)
    finally:
        # Fecha o post aconteca o que acontecer -- deixar o modal aberto atrapalharia
        # a proxima iteracao.
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    # Nao loga aqui de proposito: quem narra e o motor, que agrupa a curtida na linha
    # da pausa. Logar nos dois lugares duplicava a mesma acao no terminal.
    return {"ok": curtiu,
            "motivo": "" if curtiu else (erro or "botao de curtir nao encontrado")}


# ══════════════════════════════ MEDIDOR DE DADOS ══════════════════════════════

class MedidorDeDados:
    """Soma os bytes que a sessao baixa, lendo o `content-length` das respostas.

    Existe porque o plano do usuario tem **30 GB/mes** e o modem e a internet do PC
    inteiro -- entao "quanto isto consome" deixou de ser curiosidade e virou
    restricao de projeto. Antes disto o numero na tela era ESTIMATIVA minha, e ela
    ja errou uma vez (contava so a leitura de perfil e ignorava feed/explorar,
    mostrando 1,5 GB onde o real era ~76 GB/mes).

    E um PISO honesto, nao o total exato: resposta em chunked pode nao declarar
    tamanho. Mas o que pesa -- imagem e video -- quase sempre declara, entao o
    numero serve para a decisao que importa. `sem_tamanho` diz o quanto ficou de
    fora, para o resumo nao fingir precisao que nao tem."""

    def __init__(self):
        self.bytes = 0
        self.respostas = 0
        self.sem_tamanho = 0

    def ligar(self, page):
        """Best-effort, como todo o resto deste modulo: se nao der para escutar,
        seguimos sem medir em vez de derrubar a campanha."""
        try:
            page.on("response", self._ver)
            return True
        except Exception:
            return False

    def _ver(self, resposta):
        self.respostas += 1
        try:
            tamanho = (resposta.headers or {}).get("content-length")
        except Exception:
            tamanho = None
        if tamanho:
            try:
                self.bytes += int(tamanho)
                return
            except (TypeError, ValueError):
                pass
        self.sem_tamanho += 1

    @property
    def mb(self) -> float:
        return round(self.bytes / (1024 * 1024), 2)

    def resumo(self) -> str:
        t = f"{self.mb} MB em {self.respostas} resposta(s)"
        if self.sem_tamanho:
            t += f" · {self.sem_tamanho} sem tamanho declarado (o real é um pouco maior)"
        return t


# ══════════════════════════════ COORDENADOR ══════════════════════════════

PADROES = {
    "humanoAtivo": True,
    # % dos follows precedidos de uma LEITURA do perfil (o que torna verdadeiro o
    # container_module=profile que ja mandamos). Custa ~50KB: a 2.000 follows/dia
    # sao ~3 GB/mes, ou seja o sinal mais importante e quase de graca.
    "verPerfilPct": 100,
    # Dessas, quantas carregam a PAGINA inteira (~3MB). Baixo de proposito: e este
    # numero, junto com o "explorar", que come o plano de 30 GB. Curtir so acontece
    # quando a pagina foi carregada, entao mexer aqui mexe na curtida tambem.
    "verPerfilCompletoPct": 5,
    "curtirPct": 12,          # % dos follows seguidos de uma curtida na interface
    # % das voltas em que a primeira acao PODE ser conteudo pesado (feed ~2,5MB ou
    # explorar ~4MB). Controla CUSTO, nunca existencia: em 0 a conta continua agindo
    # em toda volta, so que sempre pelas acoes baratas (~50KB).
    #
    # Substituiu o antigo `leituraPct`, que decidia SE a volta teria acao -- e que em
    # 25% deixava 3 de cada 4 pausas voltando direto para o follow. O usuario cravou:
    # "esse e um padrao que nao pode ser quebrado independente de configuracao".
    "conteudoPesadoPct": 100,
    # % das voltas que ganham uma SEGUNDA acao. A segunda e sempre barata (~50KB) e e
    # a ultima coisa antes do follow -- e o que faz o follow parecer consequencia do
    # que se estava olhando, em vez de "voltou da pausa e ja seguiu".
    "segundaAcaoPct": 65,
    "blocoVariacaoPct": 25,   # +- no tamanho do turno (100 vira 75-125)
    # Estrutura de sessao: rajada curta de follows, depois uma pausa maior.
    "rajadaAtiva": True,
    "rajadaMin": 2,
    "rajadaMax": 5,
    # Rampa por idade da conta. Ligada por padrao: foi a unica coisa que separou
    # limpo quem caiu de quem sobreviveu na medicao de 2026-08-22.
    "rampaAtiva": True,
    "sonoAtivo": False,
    "sonoInicio": 1,
    "sonoFim": 8,
}


def config(settings: dict) -> dict:
    """Mescla os padroes com o que veio das settings do InstaFollow."""
    c = dict(PADROES)
    for k in PADROES:
        if settings and settings.get(k) is not None:
            c[k] = settings[k]
    # Migracao do antigo `leituraPct` (que decidia SE a volta agia) para o
    # `conteudoPesadoPct` (que decide o CUSTO da volta). Config antiga com valor baixo
    # passa a significar "volta barata", nao mais "volta muda" -- ou seja, migra para
    # o lado seguro sem que ninguem precise mexer em nada.
    if settings and settings.get("conteudoPesadoPct") is None \
            and settings.get("leituraPct") is not None:
        c["conteudoPesadoPct"] = settings["leituraPct"]
    return c


def _pct(valor) -> float:
    try:
        return max(0.0, min(100.0, float(valor))) / 100.0
    except (TypeError, ValueError):
        return 0.0


class Comportamento:
    """Reune as rotinas acima em torno de UM turno de follow.

    O motor (`follower.run`) chama tres metodos: `antes_de_seguir`, `depois_de_seguir`
    e `intervalo`. Tudo o mais e detalhe interno daqui."""

    def __init__(self, settings=None, *, chave_conta="", rng=None, log=None):
        self.c = config(settings or {})
        self.chave = chave_conta
        self.rng = rng or random
        self.log = log
        self.desvio = desvio_da_conta(chave_conta)
        self.contagem = {"perfil_leve": 0, "perfil_completo": 0, "curtidas": 0,
                         "feed": 0, "explorar": 0, "busca": 0, "espiar": 0, "rajadas": 0}
        # De onde sai o perfil que a conta vai ESPIAR sem seguir. Quem define e o
        # motor, que tem a fila; o modulo so pede um nome quando precisa.
        self.fonte_de_perfis = None
        self.dados = MedidorDeDados()
        # Estado da rajada em curso.
        self._rajada = tamanho_da_rajada(self.c.get("rajadaMin"), self.c.get("rajadaMax"),
                                         self.rng)
        self._na_rajada = 0
        self._ultima_rajada = self._rajada
        self._cursor = {}          # posicao do mouse, para nao reler do navegador
        # Trilha do turno -- e o que permite VER a variedade em vez de confiar nela.
        # O usuario pediu explicitamente para conseguir "prevenir comportamento
        # repetitivo", e para isso ele precisa dos numeros, nao de uma media.
        self.trilha = {"rajadas": [], "pausas": [], "termos": [], "espiados": []}

    # ── estado ──────────────────────────────────────────────────────────
    @property
    def ativo(self) -> bool:
        return self.c.get("humanoAtivo") is not False

    def dormindo(self, agora=None) -> float:
        """Segundos de sono restantes, ou 0. Ja aplica o desvio da conta."""
        if not self.ativo or self.c.get("sonoAtivo") is not True:
            return 0.0
        return segundos_ate_acordar(self.c.get("sonoInicio"), self.c.get("sonoFim"),
                                    agora, self.desvio)

    def bloco(self, centro) -> int:
        if not self.ativo:
            return int(centro or 0)
        return tamanho_do_bloco(centro, self.c.get("blocoVariacaoPct"), self.rng)

    # ── ganchos do motor ────────────────────────────────────────────────
    async def antes_de_seguir(self, page, username, *, ja_leu=False) -> dict:
        """Le o perfil antes de escrever nele. `ja_leu=True` quando o enriquecimento
        de filtros ja fez essa leitura -- nao vale gastar a requisicao duas vezes."""
        if not self.ativo or not username:
            return {"ok": False, "tipo": "desligado", "segundos": 0.0}
        # Mexer o mouse antes de agir custa ZERO dados e tira a sessao do estado
        # "pagina carregada, JS rodando, nenhum evento de ponteiro por 30 minutos".
        await mexer_na_pagina(page, rng=self.rng, estado=self._cursor)
        if self.rng.random() >= _pct(self.c.get("verPerfilPct")):
            return {"ok": False, "tipo": "sorteio", "segundos": 0.0}
        completo = self.rng.random() < _pct(self.c.get("verPerfilCompletoPct"))
        if completo:
            r = await ver_perfil_completo(page, username, rng=self.rng)
            if r.get("ok"):
                self.contagem["perfil_completo"] += 1
            return r
        if ja_leu:
            return {"ok": True, "tipo": "leve", "reaproveitada": True, "segundos": 0.0}
        r = await ver_perfil_leve(page, username)
        if r.get("ok"):
            self.contagem["perfil_leve"] += 1
        return r

    async def depois_de_seguir(self, page, contexto: dict) -> dict:
        """Curtida ocasional. So faz sentido quando a PAGINA do perfil foi carregada
        -- e o unico caso em que ha um post na tela para clicar."""
        if not self.ativo:
            return {"ok": False, "motivo": "desligado"}
        if (contexto or {}).get("tipo") != "completo" or not contexto.get("ok"):
            return {"ok": False, "motivo": "a pagina do perfil nao estava carregada"}
        if self.rng.random() >= _pct(self.c.get("curtirPct")):
            return {"ok": False, "motivo": "sorteio"}
        r = await curtir_no_perfil(page, rng=self.rng, log=self.log)
        if r.get("ok"):
            self.contagem["curtidas"] += 1
        return r

    def proximo_intervalo(self, base_ms):
        """Sorteia (segundos, modo) ate o proximo follow. Puro -- e chamado ANTES de
        esperar, para a tela poder anunciar 'proximo em X' em vez de contar depois.

        Com a rajada ligada (padrao), a sessao ganha FORMA: alguns follows rapidos
        em sequencia e depois uma pausa maior, em vez do gotejar uniforme de antes.
        A media e preservada por construcao, entao o volume nao muda."""
        if not self.ativo:
            return max(0.3, float(base_ms or 0) / 1000.0), "fixo"
        if self.c.get("rajadaAtiva") is False:
            return intervalo_humano(base_ms, self.rng)

        self._na_rajada += 1
        if self._na_rajada < self._rajada:
            return gap_da_rajada(base_ms, self.rng), "rajada"

        # Fechou a rajada: pausa maior e sorteia o tamanho da proxima.
        self.contagem["rajadas"] += 1
        self.trilha["rajadas"].append(self._rajada)
        # Guarda o tamanho da rajada que ACABOU: e ele que dimensiona a volta.
        self._ultima_rajada = self._rajada
        self._na_rajada = 0
        self._rajada = tamanho_da_rajada(self.c.get("rajadaMin"), self.c.get("rajadaMax"),
                                         self.rng)
        segundos, longa = pausa_entre_rajadas(base_ms, self.rng)
        self.trilha["pausas"].append(int(round(segundos)))
        return segundos, ("pausa longa" if longa else "pausa")

    def _perfil_para_espiar(self):
        """Pede ao motor um perfil para abrir SEM seguir. Best-effort: se a fonte
        nao existe ou falha, devolve None e a pausa faz outra coisa."""
        if not callable(self.fonte_de_perfis):
            return None
        try:
            return self.fonte_de_perfis()
        except Exception:
            return None

    def sortear_termo(self) -> str:
        """Termo de busca que NAO esteja entre os ultimos usados.

        Sorteio uniforme repete por acaso, e repetir a mesma busca e exatamente o
        padrao que o usuario quer evitar. Se a lista de termos for menor que a
        janela, cai no sorteio normal em vez de travar."""
        recentes = self.trilha["termos"][-_TERMOS_SEM_REPETIR:]
        opcoes = [t for t in TERMOS_BUSCA if t not in recentes] or list(TERMOS_BUSCA)
        termo = self.rng.choice(opcoes)
        self.trilha["termos"].append(termo)
        return termo

    def quantas_acoes_na_volta(self) -> int:
        """Quantas acoes a conta faz ao voltar da pausa.

        Escala com a rajada que acabou, para a densidade de acoes NAO depender de como
        a rajada esta configurada. Antes disto, rajada de 6-11 rendia uma acao a cada
        5,7 follows contra uma a cada 2,4 na rajada de 2-5 -- e foi exatamente essa
        escassez que o usuario sentiu como "falta de acoes apos"."""
        n = 1
        if self.rng.random() < _pct(self.c.get("segundaAcaoPct")):
            n += 1
        rajada = max(1, int(self._ultima_rajada or 1))
        n += max(0, int(round(rajada / _FOLLOWS_POR_ACAO_EXTRA)) - 1)
        return n

    async def _agir(self, page, permitir_caras=True) -> dict:
        """Uma acao de pausa: escolhe, resolve o que ela precisa (termo/perfil) e
        executa. Best-effort como todo o resto do modulo."""
        nome = sortear_leitura(self.rng, so_baratas=not permitir_caras)
        termo = self.sortear_termo() if nome == "busca" else None
        usuario = None
        if nome == "espiar":
            usuario = self._perfil_para_espiar()
            if not usuario:
                # Sem ninguem para espiar, cai numa acao igualmente barata em vez de
                # desperdicar a volta da pausa.
                nome, termo = "busca", self.sortear_termo()
        r = await fazer_leitura(page, nome, rng=self.rng, termo=termo, usuario=usuario)
        if r.get("ok"):
            self.contagem[nome] = self.contagem.get(nome, 0) + 1
            if nome == "espiar":
                self.trilha["espiados"].append(usuario)
        return r

    async def esperar(self, page, segundos, should_stop=None, modo="") -> dict:
        """Cumpre o intervalo. Na PAUSA, o silencio vem PRIMEIRO e a atividade DEPOIS.

        Essa ordem e o ponto todo. Antes a acao acontecia no INICIO da pausa, e o que
        colava no proximo follow eram dezenas de segundos de silencio -- a conta
        "voltava direto para seguir". O usuario nomeou isso em 2026-08-21:
        *"seguir - parar - seguir novamente e padrao obvio"*. Agora ela volta FAZENDO
        alguma coisa, e a ultima acao antes do follow e sempre BARATA (~50KB), porque
        acontece em toda pausa.

        Dentro da rajada nao ha acao nenhuma: ali os follows sao seguidos mesmo."""
        na_pausa = (self.c.get("rajadaAtiva") is False) or str(modo).startswith("pausa")
        base = {"segundos": segundos, "acoes": [], "na_pausa": na_pausa}
        if not (self.ativo and na_pausa):
            return {**base, "parado": await spider._sleep_stop(segundos, should_stop)}

        # 1. o descanso de fato, guardando um naco do fim para a volta
        reserva = self.rng.uniform(*_RESERVA_VOLTA)
        if await spider._sleep_stop(max(0.0, float(segundos) - reserva), should_stop):
            return {**base, "parado": True}

        # 2. a volta -- SEMPRE acontece, dimensionada pelo trabalho que a precedeu.
        #
        # NAO ha sorteio aqui, e isso e deliberado. "Seguir -> parar -> seguir" e uma
        # assinatura, e assinatura nao pode ser opcao de configuracao: se da para
        # desligar, uma hora fica desligada. Aconteceu exatamente isso em 2026-08-21 --
        # o ajuste salvo estava em 25% e 75% das pausas voltavam seguindo direto.
        # O que a config controla agora e o CUSTO da volta, nunca a existencia dela.
        acoes = []
        pode_pesado = self.rng.random() < _pct(self.c.get("conteudoPesadoPct"))
        for i in range(self.quantas_acoes_na_volta()):
            if i:
                # respiro entre uma acao e outra
                if await spider._sleep_stop(self.rng.uniform(1.5, 5.0), should_stop):
                    return {**base, "acoes": acoes, "parado": True}
            # So a PRIMEIRA pode ser cara, e so se o sorteio de custo deixar. As
            # demais sao sempre baratas (~50KB) -- a ultima e o que antecede o follow.
            acoes.append(await self._agir(page, permitir_caras=(i == 0 and pode_pesado)))

        # 3. respiro CURTO entre a ultima acao e o follow -- e o que faz o follow
        #    parecer consequencia do que se estava olhando.
        parado = await spider._sleep_stop(self.rng.uniform(0.8, 2.5), should_stop)
        return {**base, "acoes": acoes, "parado": parado}

    def resumo(self) -> str:
        """Uma linha para o log dizer o que a conta fez alem de seguir."""
        c = self.contagem
        partes = []
        if c.get("rajadas"):
            partes.append(str(c["rajadas"]) + " rajada(s)")
        if c["perfil_completo"]:
            partes.append(str(c["perfil_completo"]) + " perfil(is) abertos")
        if c["perfil_leve"]:
            partes.append(str(c["perfil_leve"]) + " perfil(is) lidos")
        if c.get("cliques_seguir"):
            partes.append(str(c["cliques_seguir"]) + " follow(s) por CLIQUE na interface")
        if c["curtidas"]:
            partes.append(str(c["curtidas"]) + " curtida(s)")
        for nome, rotulo in (("espiar", "perfil(is) espiados sem seguir"),
                             ("feed", "feed"), ("explorar", "explorar"),
                             ("busca", "busca(s)")):
            if c.get(nome):
                partes.append(str(c[nome]) + "x " + rotulo)
        if self.dados.respostas:
            partes.append("📶 " + self.dados.resumo())
        return " · ".join(partes)

    def relatorio_de_variedade(self) -> list:
        """Linhas que mostram a VARIEDADE em numeros, para dar para conferir em vez
        de confiar. Existe porque o pedido do usuario foi explicitamente
        'saber o que esta sendo feito para prevenir comportamento repetitivo' --
        e media nenhuma revela repeticao; a sequencia crua revela."""
        t, linhas = self.trilha, []
        raj, pau, termos = t["rajadas"], t["pausas"], t["termos"]
        if raj:
            distintos = len(set(raj))
            aviso = "" if distintos > 1 else "  ⚠ SEMPRE o mesmo tamanho"
            linhas.append(f"   ↳ rajadas: {', '.join(str(x) for x in raj[-20:])}"
                          f" (de {min(raj)} a {max(raj)}){aviso}")
        if pau:
            media = sum(pau) / len(pau)
            iguais = len(set(pau)) == 1 and len(pau) > 2
            aviso = "  ⚠ SEMPRE a mesma duração" if iguais else ""
            linhas.append(f"   ↳ pausas: {', '.join(str(x) + 's' for x in pau[-20:])}"
                          f" (de {min(pau)}s a {max(pau)}s, média {media:.0f}s){aviso}")
        if termos:
            repetidos = len(termos) - len(set(termos))
            extra = f"  ⚠ {repetidos} repetido(s)" if repetidos else ""
            linhas.append(f"   ↳ buscas: {', '.join(termos[-12:])}{extra}")
        espiados = t.get("espiados") or []
        if espiados:
            linhas.append("   ↳ espiados sem seguir: "
                          + ", ".join("@" + str(u) for u in espiados[-10:]))
        # Conversao leitura->follow. Media 100% e assinatura: gente real abre muito
        # perfil e segue poucos. Aqui isso vira numero na tela, nao suposicao.
        lidos = self.contagem["perfil_leve"] + self.contagem["perfil_completo"]
        seguidos = lidos                       # todo perfil lido antes do follow virou follow
        total_lidos = lidos + len(espiados)
        if total_lidos:
            pct = 100.0 * seguidos / total_lidos
            aviso = "  ⚠ seguiu TODO perfil que abriu" if pct >= 99.5 else ""
            linhas.append(f"   ↳ abriu {total_lidos} perfil(is), seguiu {seguidos}"
                          f" ({pct:.0f}%){aviso}")
        return linhas
