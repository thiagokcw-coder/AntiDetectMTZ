# registro.md — log de sessões do AntiDetectMTZ

> Log de mudanças e decisões, **mais recente primeiro**. Leia isto e o `CLAUDE.md`
> antes de trabalhar. O `CLAUDE.md` diz *como o sistema é hoje*; este arquivo diz
> *como se chegou nele* — em especial as hipóteses que morreram, para ninguém
> refazer o mesmo caminho.

---

## 2026-08-18 a 20 — InstaFollow: do 404 ao ciclo automático rodando 24h

### O que estava quebrado no começo
O follow devolvia **404 em tudo**. Coleta, fila, ródizio, limitador e métricas
funcionavam; só a escrita falhava.

### A investigação (6 hipóteses mortas, todas por medição)
Nesta ordem, e cada uma derrubada por dado, não por raciocínio:

1. **Body `container_module`** — alinhei ao `spider._follow` (comprovado ao vivo em
   2026-06-21). Não era.
2. **Conta específica** — o ródizio testou as 3, todas 404.
3. **Sessão caída** — a sonda dizia "✓ confirmada", mas ela usava o **GraphQL público
   legado**, que responde deslogado. Falso positivo. Troquei por leitura do estado da
   própria página (classe do `<html>` + cookie `ds_user_id`), **zero requisição**.
4. **Id da fila errado** — instrumentei o 404 para resolver o pk e re-tentar. O log
   ficava **mudo** quando o resolve falhava; fechei esse furo também.
5. **Família `/api/v1/` morta** — `_diag_rotas` mediu `web_profile_info` respondendo
   **200 com usuário**. A v1 estava viva; o 404 era só na escrita.
6. **Regressão nossa** — `git log` limpo. **Errei aqui** (ver "Correções" abaixo).

### A causa real, achada por captura
Construí um **espião** (`_SPY_INIT`, via `page.add_init_script`) que embrulha `fetch` e
XHR no perfil, e o botão **"🔬 Capturar requisição real"**: o usuário segue alguém na
mão e o app registra o que a interface manda. Resultado:

```
POST /api/v1/friendships/create/8402629992/
container_module=profile&include_follow_friction_check=true&nav_chain=…
&user_id=8402629992&jazoest=22845&fb_dtsg=NAfzVS1a…
```

**A rota sempre esteve certa. Faltava o corpo** — em especial o **`fb_dtsg`**, token
anti-CSRF da Meta. Sem ele o IG devolve **404, não 403**, e foi isso que mandou a
investigação para o lado errado por dias.

Mais 4 headers que a UI manda e nós não: `x-ig-www-claim`, `x-instagram-ajax`,
`x-web-session-id`, `x-ig-max-touch-points` — e o `x-asbd-id` atual é **359341** (o
nosso fixo, 129477, estava velho).

**Nada disso é fixado no código**: rotaciona. O espião **colhe** dos headers e do corpo
do tráfego real da interface, com o bootstrap da página como reserva.
Conferido: o `jazoest` **não** deriva do `fb_dtsg` pelo algoritmo clássico (daria 26330,
o real era 22845) — por isso é colhido, nunca calculado.

### Duas correções a afirmações minhas (o usuário estava certo nas duas)
1. Eu disse **"o follow nunca funcionou"**. O banco mostrava **12 follows reais em
   16/08** (`dry_run=0`). O "começou funcionando" dele era literalmente exato.
2. Eu disse **"não houve regressão"**. Houve — só não do nosso código: o Instagram
   passou a exigir o `fb_dtsg` entre 16 e 18/08.

**Lição:** git log limpo prova que *nós* não mudamos; não prova que *nada* mudou. A
superfície de terceiro é parte do sistema. **Consultar o banco antes de negar um relato.**

### Depois que voltou a funcionar
- **Métrica "seguiram de volta" por perfil.** 1ª tentativa usou `friendships/show_many`
  e deu **1500 conferidos / 0 retribuições** num perfil com 60 seguidores — esse endpoint
  responde `following` (*eu* sigo?), não `followed_by` (*ela me segue?*). Trocado por
  **ler a própria lista de seguidores e cruzar**: exato, e custa 2 requisições em vez de
  49. Duas colunas (retribuiu / foi verificado) para a taxa não mentir para baixo.
- **Aba reorganizada:** principal com 2 cartões (Coletar / Rodar); filtros, ritmo e
  unfollow foram para **⚙ Preferências**, com um resumo de uma linha na principal.
- **Ciclo automático** (`/follow/cycle/*`): supervisor que dirige `harvest` e `start` e
  espera cada um terminar. **De propósito não reimplementa** coleta nem ródizio.
- **Ritmo, em 3 iterações do usuário:** tirar tetos → repor 100/dia → *"o descanso é o
  tempo de passar todas as contas e retornar"*. Estado final: **sem teto por hora nem por
  dia; o freio é o intervalo e o descanso é o ródizio**, mais um **descanso extra de 1h**
  ao fechar a volta (`restAfterRoundMin`).

### Bugs que os testes pegaram antes de chegar em produção
- **337 mil rodadas em 40s**: o supervisor não tinha piso entre rodadas e, pior, `await`
  em corotina que não aguarda nada **não cede o event loop** — nem o Parar era processado.
- **`0 or 60` = 60**: desligar o descanso extra era silenciosamente ignorado.
- **`add_follow_action` perdia ações no mesmo milissegundo** (colisão de PK) — o
  limitador contava a menos.

### Três rótulos falsos: o motor certo, a tela errada
Três vezes o usuário achou que estava quebrado e **parou algo que funcionava**:
1. **Terminal vazio** — a UI não adotava o job do ciclo e mostrava o log de uma execução
   anterior.
2. **"SIMULAÇÃO" numa execução real** — `_follow_new_job` criava todo job com
   `dry_run: True` chumbado; só o start corrigia.
3. **"0 seguidas" após 16 follows** — o contador só somava no fim da rodada e ignorava as
   **solicitadas** (conta privada).
E depois: **"Concluído" no fim de cada rodada** lia como fim do ciclo.

**Lição registrada:** *trabalho invisível é indistinguível de trabalho quebrado*. Num
processo longo, o feedback na tela **é parte da funcionalidade**. Também fechei o último
silêncio (abertura do navegador, 10-40s) e a espera muda do modem (até 45s).

### Análise do CRM Mobiliza (`mtz-ag/mobilizalight-2ede7ae0`)
Pedido: avaliar se o app ajudaria com os números de WhatsApp caindo.

**Conclusão: disparos ficam no Mobiliza.** Ele já tem throttle por instância, warmup de
4 dias, pacing hierárquico, 20 variações de mensagem, ródizio e opt-out — um superset do
que o nosso `broadcaster.py` seria.

Dois achados úteis para ele:
- **`numeroCanonico` existe e não é usado nas campanhas.** O worker descobre se o número
  existe **tentando enviar**, e no formato errado envia duas vezes. A base tem ~88% dos
  celulares no formato legado. Alta taxa de envio para número inexistente é assinatura
  clássica de disparo em massa.
- **A causa das quedas está documentada no código dele:** o ródizio fazia o número B
  responder quem escreveu para o número A — primeira mensagem a desconhecido, com link.
  *"exatamente o disparo frio que derrubou três números nesta campanha"*. Corrigido por
  ele em 17/08.

**A troca de DDD (51 → 79) está confundida** com pelo menos 5 correções anti-ban que
entraram na mesma janela de 48h. O teste que isola: `crm_messages.direction` + a primeira
mensagem por conversa → por número, % de conversas em que **ele** falou primeiro.

**O que NÃO transfere do nosso app:** a rotação de IP do modem é **nociva** no WhatsApp
(a sessão é vínculo persistente); Camoufox é irrelevante (HTTP puro); e o nosso ródizio,
portado ingenuamente, produziria exatamente o bug que matou 4 dos 5 números dele.

### Estado medido no fim (20/08)
- **2.950 follows em 24h** por 6 contas (700 · 700 · 560 · 542 · 248 · 200).
- **Zero suspensões**, todas `active`.
- Ciclo rodando **~9h ininterruptas**, rodada 3, 1.926 follows no ciclo.
- A conta `cap.martim.poa.2026` sumiu do cadastro: **foi roubada, não banida**.

### Commits
`7e1c793` captura da requisição real · `8f20edb` headers colhidos · `00906fa` fb_dtsg ·
`c5cb23d` correções das minhas afirmações · `0183281`+`28a4a80` seguiram de volta ·
`f16d367` preferências em janela + ciclo · `18d3cd6`→`c70ba45` ritmo/descanso ·
`effb64e` descanso de 1h · `1c7e3b2`+`8195e89` rótulos falsos · `0d01801` countdown ·
`d9f7890` abertura do navegador · `0643817` fila reaproveitada · `026761b` fim de rodada

### Aberto
- **Unfollow: 0 execuções ao vivo.** Primeira vez com **simulação ligada**.
- **"Seguiram de volta" não testado ao vivo** com o método novo. Os 1.500 marcados como
  verificados com 0 retribuição são dado falso do método antigo e se auto-corrigem.
- **Disparos WhatsApp:** planejamento existe no `CLAUDE.md`, mas a conclusão da análise do
  Mobiliza mudou o escopo — não construir aqui.
- **Métrica "follows por conta até cair"** — recomendei para o Mobiliza e vale aqui;
  transforma "parece tranquilo" em teto conhecido. Não construída.
