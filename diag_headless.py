"""
diag_headless.py -- o headless do Camoufox e detectavel? MEDIR em vez de supor.

Motivo (2026-08-22): o usuario levantou que "headless browser sem cliques esta dando
sinal de bot", e duas contas da frota cairam. A parte de "sem cliques" foi corrigida
(follow por clique + eventos de ponteiro). A parte "headless" era CHUTE meu de um lado e
do outro -- o Camoufox e construido justamente contra deteccao de headless, mas ninguem
tinha medido neste ambiente.

COMO ESTE TESTE FUNCIONA, e por que ele e melhor que abrir um site de deteccao:
abre o MESMO perfil duas vezes -- uma headless, outra com janela -- e faz o DIFF das
duas sondas. Qualquer campo que difira e, por definicao, um jeito de distinguir os dois
modos. Nao depende do veredito de terceiro, nao manda o seu perfil para site nenhum e
roda em `about:blank`, ou seja **nao gasta dados** nem toca no Instagram.

COMO RODAR (voce, na sua maquina -- eu nao posso abrir o app):

    venv\\Scripts\\python.exe diag_headless.py                 # lista os perfis
    venv\\Scripts\\python.exe diag_headless.py <nome-ou-id>    # roda a medicao

Feche o app antes (o perfil fica travado enquanto o app o usa). Uma janela do Camoufox
vai abrir por alguns segundos na segunda metade do teste -- e esperado.
"""
import asyncio
import json
import sys

import db
import session_manager as sm

# Sonda: so LEITURA de propriedades do proprio navegador. Nenhuma requisicao de rede.
# Os campos foram escolhidos entre os que costumam denunciar headless em Firefox --
# se algum deles diferir entre os dois modos, ele e um vetor de deteccao real.
_SONDA = r"""
() => {
  const val = (f) => { try { const v = f(); return v === undefined ? null : v; }
                       catch (e) { return "ERRO:" + e.name; } };
  const gl = val(() => {
    const c = document.createElement("canvas");
    const g = c.getContext("webgl") || c.getContext("experimental-webgl");
    if (!g) return null;
    const d = g.getExtension("WEBGL_debug_renderer_info");
    return d ? [g.getParameter(d.UNMASKED_VENDOR_WEBGL),
                g.getParameter(d.UNMASKED_RENDERER_WEBGL)].join(" | ") : "sem-debug-info";
  });
  return {
    // O classico. Camoufox deve zerar isto.
    "navigator.webdriver":        val(() => navigator.webdriver),
    // Janela sem gerenciador de janelas costuma zerar as medidas externas.
    "window.outerWidth":          val(() => outerWidth),
    "window.outerHeight":         val(() => outerHeight),
    "outer-menos-inner (largura)":val(() => outerWidth - innerWidth),
    "outer-menos-inner (altura)": val(() => outerHeight - innerHeight),
    "window.screenX":             val(() => screenX),
    "window.screenY":             val(() => screenY),
    "screen.width":               val(() => screen.width),
    "screen.height":              val(() => screen.height),
    "screen.availWidth":          val(() => screen.availWidth),
    "screen.availHeight":         val(() => screen.availHeight),
    "screen.colorDepth":          val(() => screen.colorDepth),
    "devicePixelRatio":           val(() => devicePixelRatio),
    // Foco: headless costuma reportar que a janela nao esta em foco.
    "document.hasFocus()":        val(() => document.hasFocus()),
    "document.visibilityState":   val(() => document.visibilityState),
    "document.hidden":            val(() => document.hidden),
    // Renderizacao por software e um dos tells mais fortes de ambiente sem tela.
    "WebGL vendor|renderer":      gl,
    "navigator.hardwareConcurrency": val(() => navigator.hardwareConcurrency),
    "navigator.deviceMemory":     val(() => navigator.deviceMemory ?? null),
    "navigator.maxTouchPoints":   val(() => navigator.maxTouchPoints),
    "navigator.plugins.length":   val(() => navigator.plugins.length),
    "navigator.mimeTypes.length": val(() => navigator.mimeTypes.length),
    "navigator.languages":        val(() => (navigator.languages || []).join(",")),
    "navigator.platform":         val(() => navigator.platform),
    "navigator.userAgent":        val(() => navigator.userAgent),
    "navigator.buildID":          val(() => navigator.buildID ?? null),
    "navigator.pdfViewerEnabled": val(() => navigator.pdfViewerEnabled),
    "navigator.cookieEnabled":    val(() => navigator.cookieEnabled),
    "Notification.permission":    val(() => Notification.permission),
    "matchMedia reduced-motion":  val(() => matchMedia("(prefers-reduced-motion: reduce)").matches),
    "matchMedia color-gamut srgb":val(() => matchMedia("(color-gamut: srgb)").matches),
    "matchMedia any-pointer fine":val(() => matchMedia("(any-pointer: fine)").matches),
    "matchMedia any-hover hover": val(() => matchMedia("(any-hover: hover)").matches),
    "timezone":                   val(() => Intl.DateTimeFormat().resolvedOptions().timeZone),
    "locale":                     val(() => Intl.DateTimeFormat().resolvedOptions().locale),
    "fontes-medidas":             val(() => {
        // Largura de um texto em fontes comuns: muda quando nao ha fontes do sistema.
        const c = document.createElement("canvas").getContext("2d");
        return ["Arial", "Times New Roman", "Courier New"].map(f => {
          c.font = "48px " + f; return Math.round(c.measureText("mmmmmmmmmmlli").width);
        }).join("/");
    }),
    "canvas-hash":                val(() => {
        const c = document.createElement("canvas");
        c.width = 200; c.height = 40;
        const x = c.getContext("2d");
        x.textBaseline = "top"; x.font = "14px Arial";
        x.fillStyle = "#f60"; x.fillRect(0, 0, 100, 20);
        x.fillStyle = "#069"; x.fillText("MTZ-probe", 2, 15);
        const d = c.toDataURL();
        let h = 0; for (let i = 0; i < d.length; i++) h = (h * 31 + d.charCodeAt(i)) | 0;
        return h;
    }),
  };
}
"""

# Campos que MUDAM legitimamente entre execucoes e nao acusam headless.
# O canvas-hash entra aqui porque o Camoufox aplica ruido por sessao de proposito.
_ESPERADO_VARIAR = {"canvas-hash"}


async def _sondar(profile, headless: bool) -> dict:
    async def _fn(page):
        # NAO navegar se a pagina ja esta em about:blank. O contexto persistente abre
        # a primeira aba ja navegando para la, e um `goto` nosso corre contra ela:
        # "Navigation to about:blank is interrupted by another navigation to
        # about:blank" -- foi exatamente o que derrubou a sondagem com janela na
        # primeira execucao. E se ainda assim falhar, seguimos: a sonda so precisa de
        # um documento qualquer, nao de uma navegacao bem-sucedida.
        try:
            if not (page.url or "").startswith("about:"):
                await page.goto("about:blank", wait_until="domcontentloaded")
        except Exception:
            pass
        await asyncio.sleep(1.0)          # deixa a janela assentar antes de medir
        return await page.evaluate(_SONDA)
    return await sm.run_profile_task(profile, _fn, headless=headless)


def _achar(perfis, alvo: str):
    alvo = (alvo or "").strip().lower()
    for p in perfis:
        if str(p.get("id", "")).lower() == alvo or (p.get("name") or "").lower() == alvo:
            return p
    for p in perfis:                       # casamento parcial pelo nome
        if alvo and alvo in (p.get("name") or "").lower():
            return p
    return None


async def main() -> int:
    perfis = db.list_profiles()
    if not perfis:
        print("Nenhum perfil cadastrado.")
        return 1
    if len(sys.argv) < 2:
        print("Perfis disponiveis:\n")
        for p in perfis:
            print(f"  {p['id'][:8]}  {p.get('name')}")
        print("\nRode de novo passando o nome ou o id:")
        print(f"  venv\\Scripts\\python.exe diag_headless.py {perfis[0].get('name')}")
        return 0

    prof = _achar(perfis, sys.argv[1])
    if not prof:
        print(f"Perfil '{sys.argv[1]}' nao encontrado.")
        return 1

    print(f"Perfil: {prof.get('name')} ({prof['id'][:8]})")
    print("Sondando em about:blank -- nenhuma requisicao ao Instagram, nenhum dado gasto.\n")

    print("[1/2] headless (o modo que a campanha usa)...")
    try:
        oculto = await _sondar(prof, True)
    except Exception as e:
        print(f"  falhou: {e}")
        print("  (o app esta aberto usando este perfil? feche-o e tente de novo)")
        return 1

    print("[2/2] com janela -- uma janela vai abrir por alguns segundos...")
    try:
        visivel = await _sondar(prof, False)
    except Exception as e:
        # Nao desperdicar a metade que deu certo: grava o que ha e sai avisando.
        # (na 1a execucao real isto se perdeu por causa de um `goto` concorrente)
        print(f"  falhou: {e}")
        with open("diag_headless_resultado.json", "w", encoding="utf-8") as f:
            json.dump({"perfil": prof.get("name"), "headless": oculto,
                       "janela": None, "erro_janela": str(e)}, f,
                      indent=2, ensure_ascii=False)
        print("\n  A sonda HEADLESS deu certo e foi salva em "
              "diag_headless_resultado.json (sem o comparativo).")
        return 1

    difs = []
    for k in oculto:
        a, b = oculto.get(k), visivel.get(k)
        if a != b and k not in _ESPERADO_VARIAR:
            difs.append((k, a, b))

    print("\n" + "=" * 74)
    print("DIFERENCAS ENTRE HEADLESS E JANELA (cada uma e um jeito de distinguir os dois)")
    print("=" * 74)
    if not difs:
        print("\n  NENHUMA. Pelas propriedades sondadas, o headless do Camoufox e")
        print("  indistinguivel do modo com janela neste perfil.")
        print("  => a hipotese 'o headless nos entrega' NAO se sustenta aqui.")
    else:
        for k, a, b in difs:
            print(f"\n  {k}")
            print(f"     headless : {a}")
            print(f"     janela   : {b}")
        print(f"\n  {len(difs)} campo(s) diferem => sao vetores de deteccao reais.")

    suspeitos = []
    if oculto.get("navigator.webdriver") not in (False, None):
        suspeitos.append(f"navigator.webdriver = {oculto.get('navigator.webdriver')!r} (deveria ser false/null)")
    if oculto.get("window.outerWidth") in (0, None):
        suspeitos.append("window.outerWidth = 0 (janela sem gerenciador de janelas)")
    if oculto.get("document.hasFocus()") is False and visivel.get("document.hasFocus()") is True:
        # So conta quando DIFERE: uma janela comum tambem reporta false quando esta
        # atras de outra, entao o valor sozinho gera alarme falso (medido).
        suspeitos.append("document.hasFocus() = false no headless e true com janela")
    r = str(oculto.get("WebGL vendor|renderer") or "").lower()
    if any(t in r for t in ("swiftshader", "llvmpipe", "software", "mesa offscreen")):
        suspeitos.append(f"WebGL por software: {oculto.get('WebGL vendor|renderer')}")
    if oculto.get("navigator.plugins.length") == 0:
        suspeitos.append("navigator.plugins.length = 0")

    print("\n" + "=" * 74)
    print("SINAIS CLASSICOS, olhando so o headless")
    print("=" * 74)
    if suspeitos:
        for s in suspeitos:
            print(f"  [!] {s}")
    else:
        print("  nenhum dos sinais classicos de headless apareceu.")

    saida = "diag_headless_resultado.json"
    with open(saida, "w", encoding="utf-8") as f:
        json.dump({"perfil": prof.get("name"), "headless": oculto, "janela": visivel,
                   "diferencas": [{"campo": k, "headless": a, "janela": b}
                                  for k, a, b in difs],
                   "sinais_classicos": suspeitos}, f, indent=2, ensure_ascii=False)
    print(f"\nDetalhe completo salvo em {saida} -- me mande esse arquivo.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
