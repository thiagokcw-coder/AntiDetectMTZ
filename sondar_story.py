# -*- coding: utf-8 -*-
"""Sonda do story com sticker de LINK, pela API do app — sem login novo.

POR QUE ESTE ARQUIVO EXISTE (2026-08-28)
----------------------------------------
O mobile web tem criação de story mas NÃO tem o sticker de link, que é o ponto
(o destino é o grupo de WhatsApp). O único caminho que resta é a API privada,
que tem o conjunto completo de stickers.

O risco conhecido do instagrapi é o LOGIN: autenticar com usuário e senha
emulando um aparelho que não é o de origem foi o que queimou conta em 26/07
("não é a velocidade nem o cap — é o LOGIN"). Aqui NÃO há login: a sessão vem
dos cookies que o perfil Camoufox já tem, lidos sem uma única requisição ao
Instagram, e entram por `login_by_sessionid`.

O QUE ESTA SONDA RESPONDE, EM DUAS PERGUNTAS SEPARADAS
------------------------------------------------------
1. A sessão nascida no NAVEGADOR autentica na API do APP?
   (validamos navegador→navegador em 26/07; este elo nunca foi provado)
2. O servidor aceita o sticker de LINK para esta conta?
   Se recusar, a restrição é de CONTA — e aí o que resolve é tempo e posts,
   não caminho técnico. Nenhuma API contorna capacidade que o servidor nega.

A pergunta 1 custa uma requisição de leitura. A 2 publica um story de verdade.
Por isso postar exige `--postar` explícito: rodar sem ele só sonda.

COMO RODAR (no terminal, na pasta do projeto)
---------------------------------------------
    venv\\Scripts\\python.exe sondar_story.py
    venv\\Scripts\\python.exe sondar_story.py --conta cap.martim.rs.2026
    venv\\Scripts\\python.exe sondar_story.py --conta X --postar arte.jpg --link https://wa.me/5551999999999

⚠ Rode VOCÊ, no seu terminal. Processo iniciado pelas ferramentas do Claude
herda o contêiner do app Claude, onde o %LOCALAPPDATA% é redirecionado — ele
leria uma cópia velha dos perfis, não os seus.

RISCO QUE SOBRA, NÃO MEDIDO: o instagrapi manda cabeçalhos de aparelho que ele
inventa sobre uma sessão nascida no navegador. É bem menor que um login, mas é
uma superfície nova para a conta. O device fica GRAVADO em disco e é reusado nas
próximas execuções — device novo a cada vez seria pior que o problema.
"""
import argparse
import asyncio
import sys
from pathlib import Path

import db
import ig_session as igs
from paths import APP_DIR

# Coerência com o resto da operação: as contas são do Rio Grande do Sul e saem
# pelo modem de Porto Alegre. Dizer "US/en_US" aqui contradiria o IP.
PAIS, LOCALE, FUSO_SEGUNDOS = "BR", "pt_BR", -3 * 3600

_SESSOES = Path(APP_DIR) / "ig_sessions"


def _contas_com_perfil():
    saida = []
    for a in db.list_accounts():
        pid = a.get("profile_id")
        if not pid:
            continue
        p = db.get_profile(pid)
        if p:
            saida.append((a, p))
    return saida


def _escolher(nome_conta):
    itens = _contas_com_perfil()
    if not itens:
        sys.exit("Nenhuma conta com perfil vinculado no cadastro.")
    if nome_conta:
        alvo = [x for x in itens if (x[0].get("username") or "").lower() == nome_conta.lower()]
        if not alvo:
            print("Conta não encontrada. Disponíveis:")
            for a, _ in itens:
                print("   -", a.get("username"), "·", a.get("status"))
            sys.exit(1)
        return alvo[0]
    print("Contas com perfil (use --conta <username>):")
    for a, p in itens:
        print("   - %-24s status=%-10s perfil=%s" % (a.get("username"), a.get("status"), p.get("name")))
    sys.exit(0)


def _sessionid(perfil):
    """Lê o sessionid do perfil. ZERO requisição ao Instagram (não navega)."""
    cookies = asyncio.run(igs.export_cookies(perfil))
    resumo = igs.summarize(cookies)          # nunca expõe o valor
    print("   cookies do instagram.com: %d · ds_user_id=%s" % (resumo["ig"], resumo.get("ds_user_id")))
    for c in cookies:
        if c.get("name") == "sessionid" and c.get("value"):
            return c["value"]
    sys.exit("   ✗ este perfil não tem sessionid — ou seja, não há sessão para usar. "
             "Abra o perfil e confirme que a conta está logada.")


def _cliente(conta, perfil, sessionid):
    from instagrapi import Client

    cl = Client()
    _SESSOES.mkdir(parents=True, exist_ok=True)
    arquivo = _SESSOES / ("story_%s.json" % conta["id"])
    if arquivo.exists():
        # Device ESTÁVEL entre execuções. Um aparelho diferente a cada vez seria
        # pior que o risco que estamos tentando evitar.
        try:
            cl.load_settings(arquivo)
            print("   device reusado de %s" % arquivo.name)
        except Exception as e:
            print("   (não consegui reusar o device salvo: %s)" % e)

    cl.set_country(PAIS)
    cl.set_locale(LOCALE)
    cl.set_timezone_offset(FUSO_SEGUNDOS)

    proxy = perfil.get("proxy")
    if proxy and proxy.get("host"):
        usuario = proxy.get("username")
        cred = ("%s:%s@" % (usuario, proxy.get("password"))) if usuario else ""
        url = "%s://%s%s:%s" % (proxy.get("protocol", "http"), cred, proxy["host"], proxy["port"])
        cl.set_proxy(url)
        print("   saindo pelo proxy do perfil (%s)" % proxy["host"])
    else:
        print("   saindo pela conexão local (o modem 4G)")

    cl.login_by_sessionid(sessionid)          # NÃO é login: reusa a sessão
    cl.dump_settings(arquivo)
    return cl


def main():
    ap = argparse.ArgumentParser(description="Sonda o story com link pela API do app")
    ap.add_argument("--conta", help="username da conta (sem --conta lista as disponíveis)")
    ap.add_argument("--postar", metavar="IMAGEM", help="publica um story com esta imagem")
    ap.add_argument("--link", help="destino do sticker (use wa.me, não chat.whatsapp.com)")
    ap.add_argument("--x", type=float, default=0.5126011)
    ap.add_argument("--y", type=float, default=0.5168225)
    ap.add_argument("--largura", type=float, default=0.50998676)
    ap.add_argument("--altura", type=float, default=0.25875)
    ap.add_argument("--apagar", metavar="PK", help="apaga um story publicado (o pk sai no [3/3])")
    args = ap.parse_args()

    conta, perfil = _escolher(args.conta)
    print("\n▸ conta @%s · perfil '%s'" % (conta.get("username"), perfil.get("name")))

    print("\n[1/3] lendo a sessão do perfil (sem tocar o Instagram)")
    sid = _sessionid(perfil)

    print("\n[2/3] a sessão do NAVEGADOR autentica na API do APP?")
    try:
        cl = _cliente(conta, perfil, sid)
        info = cl.account_info()
        print("   ✓ SIM — @%s (pk %s)" % (info.username, info.pk))
        print("     este era o elo que nunca tinha sido provado (navegador → app).")
    except Exception as e:
        print("   ✗ NÃO — %s: %s" % (type(e).__name__, e))
        print("     o caminho do story pela API morre aqui; nada a fazer com o sticker.")
        return 1

    if args.apagar:
        print()
        print("[3/3] apagando o story %s" % args.apagar)
        try:
            print("   %s" % ("✓ apagado" if cl.story_delete(args.apagar) else "✗ o Instagram recusou"))
            return 0
        except Exception as e:
            print("   ✗ %s: %s" % (type(e).__name__, e))
            print("     MEDIDO em 28/08: aqui costuma dar `login_required` mesmo com a sessão")
            print("     boa (a leitura do [2/3] passa na mesma execução). O `media_delete` da")
            print("     instagrapi chama media/{id}/delete/ sem o media_type=STORY, e a sessão")
            print("     vinda de cookie não leva o header authorization que esse endpoint quer.")
            print("     Apague pelo app (story → ⋯ → Excluir), ou espere: story expira em 24h.")
            return 1

    if not args.postar:
        print("\n[3/3] pulado — rode com --postar arte.jpg --link https://wa.me/55… para testar o sticker.")
        return 0

    caminho = Path(args.postar)
    if not caminho.exists():
        sys.exit("Imagem não encontrada: %s" % caminho)
    if not args.link:
        sys.exit("Faltou --link (o sticker é justamente o que estamos testando).")

    print("\n[3/3] publicando story COM sticker de link")
    print("     imagem: %s" % caminho.name)
    print("     link:   %s" % args.link)
    try:
        from instagrapi.types import StoryLink
        story = cl.photo_upload_to_story(
            caminho,
            links=[StoryLink(webUri=args.link, x=args.x, y=args.y,
                             width=args.largura, height=args.altura)],
        )
        print("   ✓ PUBLICADO — story pk %s" % story.pk)
        print("     confira no celular se o sticker aparece e se abre o link.")
        print("     para desfazer:  cl.story_delete('%s')" % story.pk)
        return 0
    except Exception as e:
        print("   ✗ RECUSADO — %s: %s" % (type(e).__name__, e))
        print("     se a recusa for de capacidade de link, o que resolve é tempo e")
        print("     posts, não caminho técnico — a conta já foi recusada para link")
        print("     na bio em 22/08. Nesse caso: número ou wa.me escrito na arte.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
