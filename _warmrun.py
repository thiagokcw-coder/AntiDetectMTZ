import asyncio, logging, sys, time
import db, profile_warmup as pw
import session_manager as sm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)

MINUTES = 3.0

async def main():
    db.init_db()
    profiles = db.list_profiles()
    if not profiles:
        print("Nenhum perfil disponível"); return
    profile = profiles[0]
    print(f">>> Aquecendo perfil: {profile['name']} ({profile['id'][:8]}) por ~{MINUTES} min — JANELA VISÍVEL", flush=True)

    captured = {}
    async def fn(page):
        # navega acumulando cookies/histórico
        res = await pw.browse(page, MINUTES, profile["fingerprint"].get("language",""))
        # captura cookies e histórico ao final, ainda com a página aberta
        try:
            ctx = page.context
            cookies = await ctx.cookies()
            captured["cookies"] = cookies
        except Exception as e:
            captured["err"] = str(e)
        return res

    res = await sm.run_profile_task(profile, fn, headless=False, load_extension=False)
    cookies = captured.get("cookies", [])
    domains = sorted(set(c.get("domain","") for c in cookies))
    print(f">>> Resultado: visitou {res.get('visited') if res else '?'} site(s)", flush=True)
    print(f">>> Cookies acumulados: {len(cookies)} de {len(domains)} domínios", flush=True)
    print(f">>> Domínios: {domains[:15]}", flush=True)
    if res and res.get("visited",0) > 0:
        db.mark_profile_warmed(profile["id"])
        p = db.get_profile(profile["id"])
        print(f">>> Perfil marcado como aquecido: warmup_count={p.get('warmup_count')} last={p.get('last_warmup_at')}", flush=True)
    print("FIM", flush=True)

asyncio.run(main())
