# -*- coding: utf-8 -*-
"""Medir o compositor de uma rede: fotografar a tela antes de automatizá-la.

POR QUE ESTE MÓDULO EXISTE
--------------------------
A aba Publicar existe para publicar em todas as redes de uma vez. Hoje só o
Instagram publica sozinho; as outras sete abrem o compositor e esperam você
arrastar o arquivo e apertar publicar — ou seja, continua uma a uma, que é
exatamente o que a aba deveria acabar.

O que falta para automatizá-las não é código: é **medição**. Escrever sete
fluxos de DOM sem nunca ter aberto a tela de cada rede produz sete seletores
inventados, e esta base já pagou dias por adivinhar uma rota
(`registro.md`, o 404 de agosto). O que resolveu aquele caso em minutos foi o
botão "🔬 Capturar requisição real" — medir em vez de teorizar. Este módulo é
o mesmo instrumento, para o compositor.

Ele abre o compositor da rede numa aba da janela do perfil (sessão logada, o
mesmo IP, a mesma época) e **lê a estrutura da tela**: onde está o campo de
arquivo, onde está a legenda, quais botões existem e como se chamam. Guarda
num JSON em `data/_compositores/` e tira uma foto da tela.

NÃO PUBLICA NADA. Não clica, não preenche, não envia. É leitura.

O QUE ELE NUNCA LÊ
------------------
Valor de campo (`.value`), `document.cookie`, `localStorage`. A sessão da rede
vive nesses lugares, e o relatório é um arquivo em disco que pode ser enviado
adiante — então ele carrega só ESTRUTURA (nome, rótulo, seletor, visibilidade).
Regra do projeto: nunca registrar valor de cookie ou de sessão.
"""
import asyncio
import json
import time
from pathlib import Path

#: Onde os relatórios ficam. Dentro de `data/`, que o `.gitignore` já cobre
#: inteiro — relatório de tela logada não vai para o repositório.
PASTA = Path(__file__).parent / "data" / "_compositores"

#: Palavras que, num botão, costumam ser o "publicar" daquela rede. Serve só
#: para o PALPITE do relatório — o seletor de verdade sai da leitura humana
#: do JSON, nunca desta lista.
_VERBOS_DE_PUBLICAR = (
    "publicar", "publish", "post", "postar", "tweet", "compartilhar", "share",
    "enviar", "upload", "próximo", "proximo", "next", "avançar", "avancar",
    "concluir", "done", "salvar",
)

# Lê a estrutura da tela. Roda no navegador, devolve JSON puro.
#
# Escolhas que importam:
#  - `seletor` prefere atributo ESTÁVEL (data-e2e, name, aria-label) a caminho
#    de classes: classe de SPA muda a cada build, data-e2e sobrevive. O caminho
#    de classes é o último recurso, e vem marcado como frágil por ser o que é.
#  - `visivel` separa o campo real do decorativo: compositor sério esconde o
#    `input[type=file]` atrás de um botão bonito, e é no escondido que o
#    Playwright deve mandar o arquivo (`set_input_files` funciona em campo
#    oculto, clique não).
#  - nenhum `.value`, nenhum cookie — ver o cabeçalho do módulo.
_LER_TELA = r"""
() => {
  // "Esta na tela?" -- medido, nao adivinhado. Exigir largura E altura > 1
  // parecia certo e dava FALSO NEGATIVO: elemento em bloco herda a largura do
  // pai, e num viewport degenerado (aba de fundo, headless mal dimensionado)
  // isso e zero, entao um `div[role=button]` bem visivel saia como oculto.
  // Medido em 13/10 num DOM de verdade. O criterio passou a ser: tem caixa de
  // layout, nao e o 1x1 que as redes usam para esconder input, e o estilo nao
  // o apaga. Erra para o lado de MOSTRAR -- num relatorio que uma pessoa le,
  // esconder um controle que existe e pior que listar um duvidoso.
  const vis = (el) => {
    try {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      const temCaixa = el.getClientRects().length > 0;
      const minusculo = r.width <= 1 && r.height <= 1;
      return temCaixa && !minusculo && s.visibility !== 'hidden'
             && s.display !== 'none' && s.opacity !== '0';
    } catch (e) { return false; }
  };
  const attr = (el, n) => (el.getAttribute(n) || '');
  const sel = (el) => {
    for (const a of ['data-e2e', 'data-testid', 'data-test-id', 'name', 'aria-label']) {
      const v = attr(el, a);
      if (v && v.length < 60) {
        return el.tagName.toLowerCase() + '[' + a + '="' + v.replace(/"/g, '') + '"]';
      }
    }
    if (el.id && !/^[0-9]/.test(el.id)) return '#' + el.id;
    const partes = [];
    let n = el;
    for (let i = 0; i < 4 && n && n.nodeType === 1; i++) {
      let p = n.tagName.toLowerCase();
      if (n.classList && n.classList.length) {
        p += '.' + Array.from(n.classList).slice(0, 2).join('.');
      }
      partes.unshift(p);
      n = n.parentElement;
    }
    return partes.join(' > ');
  };
  const txt = (el) => ((el.innerText || el.textContent || '') + '')
                        .trim().replace(/\s+/g, ' ').slice(0, 60);
  const take = (q, lim, fn) => Array.prototype.slice.call(
      document.querySelectorAll(q), 0, lim).map(fn);

  const arquivos = take('input[type=file]', 20, el => ({
    seletor: sel(el), nome: attr(el, 'name'), id: el.id || '',
    aceita: attr(el, 'accept'), varios: !!el.multiple, visivel: vis(el),
  }));

  const textos = take(
    'textarea, [contenteditable="true"], div[role=textbox], input[type=text], input[type=search]',
    25, el => ({
      seletor: sel(el), tag: el.tagName.toLowerCase(),
      placeholder: attr(el, 'placeholder') || attr(el, 'data-placeholder')
                   || attr(el, 'data-text'),
      aria: attr(el, 'aria-label'), papel: attr(el, 'role'),
      limite: attr(el, 'maxlength'), visivel: vis(el),
    }));

  const botoes = take('button, [role=button], input[type=submit], a[role=button]',
    80, el => ({
      seletor: sel(el), texto: txt(el), aria: attr(el, 'aria-label'),
      data_e2e: attr(el, 'data-e2e'),
      data_testid: attr(el, 'data-testid') || attr(el, 'data-test-id'),
      desabilitado: !!el.disabled || attr(el, 'aria-disabled') === 'true',
      visivel: vis(el),
    })).filter(b => b.texto || b.aria || b.data_e2e || b.data_testid);

  return {
    titulo: (document.title || '').slice(0, 120),
    url: location.href,
    campos_senha: document.querySelectorAll('input[type=password]').length,
    arquivos: arquivos,
    textos: textos,
    botoes: botoes,
    iframes: take('iframe', 10, f => attr(f, 'src').slice(0, 140)),
  };
}
"""


def _visiveis_primeiro(itens: list) -> list:
    """Visível antes de oculto, mantendo a ordem do documento dentro de cada
    grupo. Quem lê o relatório procura primeiro o que está na tela."""
    return [i for i in itens if i.get("visivel")] + [i for i in itens if not i.get("visivel")]


def _palpite(lido: dict) -> dict:
    """Um PALPITE de quais elementos seriam o arquivo, a legenda e o publicar.

    É palpite e o relatório o chama assim. Serve para encurtar a leitura, não
    para virar seletor sem alguém conferir a foto — rótulo que afirma o que não
    foi conferido é o erro que esta base já pagou quatro vezes.
    """
    arq = next((a for a in lido.get("arquivos") or [] if a.get("visivel")), None) \
        or next(iter(lido.get("arquivos") or []), None)

    # Campo de legenda: o maior candidato visível, preferindo contenteditable e
    # textarea (é o que as redes usam) a um input de busca que só passava por ali.
    def _peso(t):
        bom = t.get("tag") in ("textarea",) or t.get("papel") == "textbox" \
              or t.get("tag") == "div"
        return (1 if t.get("visivel") else 0) + (1 if bom else 0)
    textos = sorted(lido.get("textos") or [], key=_peso, reverse=True)
    leg = next(iter(textos), None)

    pub = []
    for b in lido.get("botoes") or []:
        alvo = ((b.get("texto") or "") + " " + (b.get("aria") or "") + " "
                + (b.get("data_e2e") or "")).lower()
        if any(v in alvo for v in _VERBOS_DE_PUBLICAR):
            pub.append(b)
    return {
        "arquivo": arq, "legenda": leg,
        "publicar": _visiveis_primeiro(pub)[:5],
    }


def _resumo(rede_nome: str, lido: dict, login: bool) -> str:
    if login:
        return ("o compositor do %s respondeu com a tela de login — a sessão "
                "desta conta não valeu, não há o que medir ainda" % rede_nome)
    a = len([x for x in lido.get("arquivos") or [] if x.get("visivel")])
    ao = len(lido.get("arquivos") or [])
    t = len([x for x in lido.get("textos") or [] if x.get("visivel")])
    b = len([x for x in lido.get("botoes") or [] if x.get("visivel")])
    return ("%s: %d campo(s) de arquivo (%d visível(is)), %d campo(s) de texto "
            "visível(is), %d botão(ões) visível(is)" % (rede_nome, ao, a, t, b))


async def medir(perfil: dict, rede_id: str, espera_ms: int = 5000) -> dict:
    """Abre o compositor de `rede_id` no perfil e devolve o que viu.

    `espera_ms` existe porque compositor de rede social é SPA: o
    `domcontentloaded` chega antes do formulário existir, e medir aí mediria a
    tela vazia. Esperar alguns segundos depois do DOM é mais honesto que
    esperar `load`, que numa página que carrega mídia por minutos daria timeout
    numa tela já utilizável (a mesma razão do `abrir_aba`).

    Deixa a aba ABERTA de propósito: a foto é minha, mas a tela é sua — ver o
    que foi medido é metade da medição.
    """
    import ig_session as igs
    import redes as _redes
    import session_manager as sm

    r = _redes.rede(rede_id)
    if not r:
        raise ValueError("não conheço a rede %r" % rede_id)
    url = r.get("compositor") or r.get("site")
    if not url:
        raise ValueError("não sei onde fica o compositor do %s" % r["nome"])

    quando = time.strftime("%Y%m%d-%H%M%S")
    PASTA.mkdir(parents=True, exist_ok=True)
    png = PASTA / ("%s-%s.png" % (rede_id, quando))

    async def _na_aba(page):
        try:
            await page.wait_for_timeout(espera_ms)
        except Exception:
            pass
        lido = await page.evaluate(_LER_TELA)
        try:
            await page.screenshot(path=str(png))
            lido["_foto"] = True
        except Exception as e:
            lido["_foto"] = False
            lido["_foto_erro"] = str(e)[:140]
        return lido

    aberta = await sm.abrir_aba(perfil, url, fn=_na_aba)
    lido = aberta.get("resultado") or {}

    final = lido.get("url") or aberta.get("url") or url
    login = igs.parece_tela_de_login(final) or bool(lido.get("campos_senha"))

    lido["arquivos"] = _visiveis_primeiro(lido.get("arquivos") or [])
    lido["textos"] = _visiveis_primeiro(lido.get("textos") or [])
    lido["botoes"] = _visiveis_primeiro(lido.get("botoes") or [])

    rel = {
        "rede": rede_id, "rede_nome": r["nome"], "quando": quando,
        "perfil": perfil.get("id"), "perfil_nome": perfil.get("name"),
        "url_pedida": url, "url_final": final,
        "parece_login": login,
        "titulo": lido.get("titulo"),
        "achados": {"arquivos": len(lido["arquivos"]), "textos": len(lido["textos"]),
                    "botoes": len(lido["botoes"])},
        "palpite": _palpite(lido),
        "tela": lido,
        "resumo": _resumo(r["nome"], lido, login),
    }

    jsn = PASTA / ("%s-%s.json" % (rede_id, quando))
    await asyncio.to_thread(
        jsn.write_text, json.dumps(rel, ensure_ascii=False, indent=2), "utf-8")

    rel["arquivo_json"] = str(jsn)
    rel["arquivo_png"] = str(png) if lido.get("_foto") else None
    return rel
