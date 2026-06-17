# CLAUDE.md — AntiDetect Camoufox (contexto do projeto)

> Notas de contexto para o Claude. App de **gestão de contas Instagram** (Camoufox/Firefox antidetect + Playwright). Backend FastAPI (`api.py`/`main.py`), frontend em `static/index.html`. **Multiplataforma (Windows + macOS).** Idioma do usuário: **português**.

## ⚡ ESTADO ATUAL — fluxo mudou (2026-06-15)
**A criação automática de contas pelo desktop foi ABANDONADA** (queimava as contas na hora; proxy/IP era o fator nº1). Novo fluxo, decisão firme do usuário:
- Contas são **criadas manualmente no app do Instagram em um celular Android real** (IP móvel 4G/5G).
- O app agora serve para **cadastrar (manual), monitorar e abrir os perfis no PC**.
- Aba "Criar Contas" (page-instagram) **oculta/fora do menu** — página e endpoints continuam no código, **dormentes** (não apagar). As seções abaixo sobre o fluxo de signup descrevem esse código dormente.
- **Cadastro manual** ("+ Adicionar conta", aba Contas): por padrão cria um perfil Camoufox com **conexão local** (proxy=None = internet do celular/tethering, mesmo IP da criação). Sentinela `profile_id="__new__"` no POST /accounts; perfil usa o SO real do PC no fingerprint. Proxy opcional (não recomendado). Endpoint extra POST /accounts/{id}/create-profile. "Abrir no PC" = login de "novo dispositivo" → deixar amadurecer no celular + proxy do mesmo país.
- **Monitoramento** (aba Contas): resumo por status no topo, destaque de linhas com problema, aviso "sem perfil" + criar perfil em 1 clique, re-checagem automática (setting `auto_recheck_hours`, loop no startup de api.py).
- **Aba Emails** virou **registro manual** (livre/usado) espelhando WhatsApps; IMAP/catch-all removido da UI (código IMAP do backend fica dormente). Endpoints `/emails` (GET/POST/PATCH/DELETE).
- Menu: Início = Comece aqui · Perfis · Contas · Emails · WhatsApps · Métricas; Utilitários = Importar CSV · Novo Perfil · Proxies · Abrir Todos · Sessões · Diagnóstico.

## 🆕 SESSÃO 2026-06-16 (continuar de outro PC daqui)

**Banco compartilhado CONFIGURADO (Supabase Postgres).** Projeto "ManagerMTZ data", região São Paulo. Conexão via **Session pooler** (`postgresql://postgres.lakoruwdfabeyqbqcffg:<senha>@aws-1-sa-east-1.pooler.supabase.com:5432/postgres?sslmode=require`). Dados locais já migrados (`migrate_to_postgres.py`). A URL fica em **`database_url.txt`** ao lado do launcher (**gitignored** — não vai pro git). Compartilhado = registro de perfis/contas/emails/proxies/métricas; **local em cada PC** = `user_data` (cookies/sessão logada).
- **Para continuar de OUTRO PC:** (1) clone `thiagokcw-coder/AntiDetectMTZ`; (2) crie `database_url.txt` na pasta com a string do Session pooler (senha do Supabase → Settings·Database·Reset se não tiver); (3) rode `MonitorMtzWin.bat` (mostra "PostgreSQL compartilhado"). Os cookies/sessões NÃO migram — logins ficam por máquina.

**Spider (NOVO — aba Utilitários).** Reimplementação do mtzSpider SEM extensão: raspa a API do IG via `fetch` autenticado rodando `page.evaluate` DENTRO de um perfil Camoufox JÁ LOGADO (cada perfil = 1 conta+IP). `spider.py` + endpoints `/spider/extract|jobs/{id}|/stop|/csv` + aba "Spider" (commit `5775403`).
- **Fase 1 = seguidores (FEITA, não testada ao vivo ainda).** Resolve `username→userId` (`/api/v1/users/web_profile_info/?username=`), pagina `/api/v1/friendships/{id}/followers/?count=50&max_id={next_max_id}` (→ `users[]`, `next_max_id`), jitter humano, trata 429. Headers mínimos: `x-csrftoken`(cookie)+`x-ig-app-id:936619743392459`+`x-asbd-id:129477`+`x-requested-with`.
- **Seguindo + Posts (FEITO, 2026-06-17).** Web (`spider.py`): `_friendship_list(edge)` cobre followers/following (`/api/v1/friendships/{id}/{edge}/`); `_posts` usa `/api/v1/feed/user/{id}/?count=12` (→ `items[]`, `next_max_id`, `more_available`, dedup por `code`/Shortcode). App (`spider_app.py`): `_friendship_loop(edge)` usa `user_followers_v1_chunk`/`user_following_v1_chunk`; `_posts_loop` usa `user_medias_paginated_v1(uid, amount=33, end_cursor=)`. `spider.headers_for(kind)` → POSTS_HEADERS (Shortcode/URL/Caption/Likes/Comments/Views/Taken At/Media Type/Product Type) ou FOLLOWERS_HEADERS. CSV nomeia por tipo (IGFollowers/IGFollowing/IGPosts). Resume funciona p/ os 3 (seed dedup por User Id ou Shortcode). UI: seletor "O que extrair" com Seguidores/Seguindo/Posts. **Não testado ao vivo** (depende de conta logada). instagrapi: métodos confirmados na lib instalada.
- **Fase 2 (PENDENTE) — extrações de GraphQL** (receita mapeada do mtzSpider em `C:\Users\T-GAMER\Desktop\APPS\SPIDER + CAMOUFOX\mtzSpider-main\chrome-extension\`):
  - Curtidas/comentários/hashtag/local via **GraphQL** `/graphql/query/?query_hash=<hash>&variables={...}` (cursor `after` ← `page_info.end_cursor`/`has_next_page`). Hashes mudam (mtzSpider tem hash-discovery varrendo bundles JS): likes `1cb6ec562846122743b61e492c85999f`, comments `33ba35852cb50da46f5b5e889df7d159`, hashtag `f92f56d47dc7a55b606908374b43a314`, location `36bd0f2bf5911908de389b8ceaa3be6d`.
- **Fase 3 (PENDENTE):** rotação entre VÁRIOS perfis (espalha por IPs), timing/pausas mais finos.
- **Terminal do processo + Histórico (FEITO, 2026-06-17).** **Terminal:** `spider.extract`/`spider_app.extract` recebem callback `log(msg)`; o job acumula `log[]` (cap 500), `/spider/jobs/{id}` devolve o tail (300) e a UI mostra num `<pre id="sp-log">` rolando (páginas, rate limit, fim). **Histórico persistente:** tabela `spider_runs` (db.py: id, method, profile, target, kind, count, status, error, **rows_json**, started_at, finished_at; `spider_run_upsert`/`list_spider_runs`/`get_spider_run`/`delete_spider_run`). Gravado no início (running) e no `finally` do worker (com rows_json). Endpoints `GET /spider/history`, `DELETE /spider/history/{id}`; `/spider/jobs/{id}/csv` faz **fallback** pro `rows_json` do banco (baixa execuções antigas mesmo após reiniciar). UI: tabela "Histórico de execuções" (CSV + remover) + `loadSpiderHistory()`.
- **Velocidade de extração (FEITO, 2026-06-17).** Seletor na UI (Fantasma 8s / Seguro 5s / Balanceado 2,5s / Turbo 1,2s = `delay_ms`) replicando os presets do mtzSpider. `delay_ms` flui: front → `SpiderExtractRequest` → `_launch_spider_job` → `spider.extract`/`spider_app.extract` → loops. Entre páginas: `_page_delay_s` (base × jitter **±40%**) + **pausa de proteção** 5–20s a cada ~15–25 páginas (logada no terminal). Persistido em `spider_runs.delay_ms` → resume mantém a velocidade. Vale Web e App (rotação multi-conta do mtzSpider = Fase 3, ainda não).
- **Continuar de onde parou / RESUME (FEITO, 2026-06-17).** `spider._followers`/`spider_app._followers_sync` retornam `(rows, cursor)` e aceitam `start_max_id`+`seed_rows`; `extract` retorna `cursor` (None=completo). `spider_runs` ganhou colunas `profile_id/account_id/use_local/limit_n/cursor` (migração via `_ensure_column`). Job que para no meio vira status **`stopped`** com `cursor`. `api._launch_spider_job` (refatorado, usado por extract e resume) salva tudo; **`POST /spider/resume/{run_id}`** recarrega rows_json (seed) + cursor e continua dedupando (seen pelo "User Id"). UI: botão "▶ Continuar de onde parou" no job parado e "▶" por linha no histórico (só quando há cursor). Funciona Web e App.
- **DOIS MÉTODOS na aba Spider (usuário escolhe por extração):**
  - **Web** (`spider.py`): fetch na API WEB dentro de um perfil Camoufox JÁ LOGADO. Sem login novo; superfície web (mais vigiada, mas você resolve desafio na tela do Camoufox). **Fallback confiável.**
  - **App** (`spider_app.py`, **instagrapi** — em `requirements.txt`): API MOBILE, loga com usuário/senha da conta emulando o app, sessão salva em `APP_DIR/ig_sessions/{id}.json`. Interface: status do login + **campo de código** (checkpoint) — endpoints `/spider/jobs/{id}/code` + estado `needs_code` (threading.Event). Opção **use_local** (marcada por padrão): ignora o proxy da conta e sai pela **conexão local do PC** (IP do celular/tethering = coerente → menos desafio).
  - **APRENDIZADO (teste real):** 1º login do instagrapi numa conta nascida no celular costuma dar **`challenge_required` NÃO auto-resolvível** (fluxo novo "auth_platform" do IG — `instagrapi/mixins/challenge.py` levanta antes do code handler) e/ou disparado por IP de proxy ruim. instagrapi usa **device EMULADO** (não é o celular real; sem root não dá pra herdar a sessão do app). → Mitigação: `use_local` (IP coerente). Quando ainda assim desafia, **usar o método Web** (login manual no Camoufox resolve o desafio). `spider_app.extract` já dá mensagens claras (challenge/2FA/senha).
- **FIX "app travava em Carregando" (commit `45a632f`):** `main.py` pingava `/profiles` (timeout 1s) pra prontidão, mas com o banco remoto `/profiles` levava 6,5s → travava. Agora: endpoint **`/health`** (instantâneo) usado pelo `main.py`; `/profiles` otimizado p/ ~0,9s (contas via `db.list_accounts` cru + `db.get_active_profile_locks` em 1 query, em vez de N+1). **Aba Contas ainda usa `acc._merged_accounts` (~2,8s no remoto) — otimizar se incomodar.**

**UI/backend recentes desta sessão (commits `9b42998`/`199b69b`/`ede1c17`):** menu **Contas antes de Perfis**; aba **Perfis** com busca+filtros (com/sem conta, conta logada, aquecidos)+ordenação por status+**card verde p/ conta IG logada** (`/profiles` traz `account_status`/`account_username`); aba **Contas** sem as seções "proxies/whatsapps/limites usados" (resquícios); e-mail marcado **"usado"** ao vincular conta e ao detectar ATIVA (`emails.mark_used_by_address`); **auto-verificar status ao FECHAR** perfil aberto manual (`session_manager._run`); perfil do modo manual nomeado pelo username. README atualizado p/ a direção atual.

## 🆕 SESSÃO 2026-06-16 (Mac) — banco compartilhado ativado + estratégia de extração

**`database_url.txt` corrigido.** Estava salvo como `database_url.txt.txt` (`.txt` dobrado), então o app caía no SQLite local em vez do Supabase. Renomeado p/ `database_url.txt` → **Supabase compartilhado agora ATIVO neste Mac** (e o nome certo volta a ser coberto pelo `.gitignore`). Obs.: o `.gitignore` cobre `database_url.txt` exato, não `*.txt.txt` — se reaparecer com nome errado, a senha fica exposta.

**Estratégia de EXTRAÇÃO repensada (decisão PENDENTE — não implementado ainda).** Discussão sobre como raspar com menos risco, dado o fluxo "conta nasce no app do celular":
- **Spider web atual (fetch na web logada, PC ou Chrome do celular) = mesma classe de risco "web".** O Chrome do celular NÃO resolve: logar no `instagram.com` é evento de login web + fingerprint de navegador; o celular só dá **coerência de IP**, não tira do terreno web (o mais vigiado).
- **App = superfície confiável (API privada mobile `i.instagram.com/api/v1/...`), "mais brechas"/mais dados.** PORÉM **não dá pra reaproveitar a sessão do app real** sem root: o app usa **certificate pinning** (bloqueia MITM em celular sem root) e ler o token dos dados do app exige root.
- **Caminhos realistas p/ usar a superfície do app:**
  1. **instagrapi** (cliente da API mobile em Python): loga emulando o app, **salva a sessão**, roteia pelo **IP do celular (tethering)**. Rápido, API completa, roda no PC. Contra: é login programático (separado do app real) → exige device+IP coerentes e ritmo lento. **← recomendado (acesso + segurança).**
  2. **Appium/ADB no app real**: automatiza a UI (scroll + lê a tela). É literalmente o app logado (zero login novo, mais seguro), mas lento/frágil e limitado ao que aparece.
  3. Spider web = plano C.
- **PRÓXIMO:** usuário escolher entre instagrapi (protótipo: seguidores via tethering + sessão persistida) e Appium. Qualquer um = volume baixo, 1 conta/IP, pausas humanas.

## Como rodar / validar
- Rodar: **`MonitorMtzWin.bat`** (Windows) ou **`MonitorMtzMac.command`** (macOS/Linux) — criam venv, instalam deps + Firefox/Playwright + Camoufox, iniciam `main.py`. (Eram `run.bat`/`run.command`.)
- **macOS:** o `python3` do PATH pode ser antigo (3.7); o launcher tenta `python3.12/3.11/...`. Usa-se o **python3.11 do Homebrew** (`/opt/homebrew`). Branding no `main.py` troca ícone do Dock (MTZ) e nome ("Monitor MTZ"). `.app` foi descartado (Gatekeeper bloqueia bundle não-assinado no macOS 26). Ícones em `icons/` (`MonitorMtzMac.icns` verde, `MonitorMtzWin.ico` azul).
- Validar Python: `./venv/bin/python -m py_compile arquivo.py` (macOS) ou `venv\Scripts\python.exe -m py_compile` (Windows). Testar UI ao vivo: launch.json + preview na porta 18760. Não há suíte de testes.
- Banco: **SQLite local** (Win: `%LOCALAPPDATA%\ProfileManager\`; macOS: `~/Library/Application Support/ProfileManager/`; ver `paths.py`). `db.py` usa **PostgreSQL compartilhado SE existir `database_url.txt`** ao lado do launcher (ou env `DATABASE_URL`); senão SQLite. `_ex()` adapta `?`→`%s`.
- Git: repo em `main`, remote **`thiagokcw-coder/AntiDetectMTZ`**. **Push é feito pelo usuário no GitHub Desktop** (gh CLI expirou) — eu só commito local. `.gitignore` cobre venv/proxys/database_url.txt/*.db/logs/screenshots/*.png/*.zip/.claude/*.app.

## ⚠️ PENDÊNCIAS ABERTAS
1. ~~Commit/push pendente~~ **FEITO** (commits `d5383af`/`c6d1047` e seguintes). `CLAUDE.md` segue só local (não rastreado). Push para `mtz-ag/AntiDetectMTZ` ainda **pode estar pendente** — confirmar com o usuário.
2. ~~Estabilidade do fingerprint por perfil~~ **RESOLVIDO (commit `34c9407`).** Confirmado que NÃO persistia por `user_data_dir` (só cookies/storage). Solução: **semente estável por perfil** (`fp_seed` no JSON do fingerprint; perfis antigos derivam do `id`). `session_manager._stable_webgl` fixa o par GPU vendor/renderer (kwarg `webgl_config`), `_stable_fingerprint` fixa navigator/UA/hardwareConcurrency/audio (kwarg `fingerprint=`, gerado determinístico com `random.seed` save/restore — concorrência-safe; em falha retorna None e Camoufox gera o seu, nunca derruba o launch). Validado ao vivo (2 launches = idêntico).
   - **FOLLOW-UP (menor):** `canvas:aaOffset` (`randint -50..50`) e `fonts:spacing_seed` ainda re-randomizam por launch dentro de `launch_options` (rodam no executor, sem kwarg p/ fixar). Só dá pra travar congelando o config inteiro (`from_options`), o que prende o geoip ao IP da criação — **descartado por ora** (risco de incoerência fuso↔IP em proxy residencial rotativo > ganho). `window.history.length` varia mas não importa.
   - ~~WebRTC leak~~ **VERIFICADO + CORRIGIDO (commit `50ad7b6`).** Medido ao vivo (ICE candidates): com **geoip on** só o IP do proxy aparece (sem LAN, sem IPv6 — Camoufox seta `network.dns.disableIPv6`) → seguro. Com **geoip off** (fallback da cascata) o WebRTC fica sem spoof e o STUN (UDP, ignora o proxy HTTP) **vaza o IP real** (IPv4+IPv6). Fix: `_camoufox_kwargs` passa `block_webrtc=True` quando `not geoip and proxy`. Tradeoff: bloquear remove `RTCPeerConnection` (leve sinal), mas só no fallback raro e melhor que vazar.
3. **DETECÇÃO IMEDIATA NO SIGNUP (conta sinalizada/telefone de cara) — diagnóstico 2026-06-14.** Investigado com logs + ledger + DB + ip-api. Fatores, em ordem de impacto:
   - **Proxy = fator nº1 (INFRA do usuário, não código).** Pool atual = `proxys/Webshare 20 proxies.txt` = **ISP-estáticos compartilhados** da Webshare (IPs fixos, multi-cliente, países aleatórios IT/US/DE/FR/AT; um já marcado `proxy:true`). Para CRIAÇÃO, queimam rápido. Recomendado: **residencial rotativo de verdade ou móvel (4G/5G)**, **1 IP limpo por conta** (sticky), **um país coerente**, volume baixo/devagar. Datacenter/ISP-estático só servem p/ aquecer conta já criada.
   - ~~Reuso do mesmo IP em todas as contas~~ **CORRIGIDO (commit `060a3f8`).** `proxies._active_profile_counts()` conta perfis VIVOS por proxy e soma ao score do `assign()` (espalha por IPs distintos). Causa era `usage_counts()` contar só contas concluídas → pool todo com 0 → mesmo IP sempre.
   - ~~Incoerência geográfica (persona BR em IP estrangeiro)~~ **CORRIGIDO (commit `3fdcb4a`).** Setting **`target_country`** (ISO-2, aba Contas → Limites): `assign()` filtra o pool p/ esse país (resolve país do IP OFFLINE via GeoLite2 do Camoufox; fallback ao pool inteiro c/ aviso se não houver). Default "" = sem filtro. **Pool atual não tem IP BR** — p/ coerência BR, adicionar proxies do Brasil.
   - ~~Bugs de comportamento no signup (double-submit + re-fill + aniversário falhando + captcha no loop ignorado)~~ **CORRIGIDO (commit `23ec511`).** `instagram.create_account`: `submit_ok_once` (não reenvia após submit aceito — humano só espera); detecta `_is_captcha_screen` dentro do loop → sai p/ `_wait_manual_captcha` em vez de re-preencher; espera mais paciente.
   - **Email** `personalidade.online` (catch-all custom) = sinal menor; mainstream (gmail/outlook) é mais seguro.
   - **NÃO testado em signup real** após os fixes (depende de proxy bom). Os fixes reduzem sinais, mas sem proxy decente o IG provavelmente ainda sinaliza.

## Decisões do usuário (firmes)
- **SEM IA embutida.** Adaptação/aprendizado = **regras + estatística** sobre dados coletados (determinístico, grátis, explicável). IA de visão p/ telas desconhecidas foi considerada e **descartada** por ora.
- **Captcha (reCAPTCHA Enterprise) = MANUAL.** Aparece pós-submit; o app pausa e avisa, o usuário resolve na janela.
- **Telefone/WhatsApp na CRIAÇÃO → descartar** (nasce sinalizada). **Pedido DEPOIS (checkpoint) → recuperável** (estratégia de verificação ainda não definida — não implementar sem o usuário definir).
- Proxies do usuário = **residenciais**.
- Code review/edições: **mudanças pequenas e testadas** (o código já quebrou várias vezes com refactors grandes às pressas).

## Fingerprint (fingerprint.py + Camoufox)
`fingerprint.py` (browserforge, browser="firefox") guarda só campos para exibição; **Camoufox só consome `platform`(OS) e `screen`**. O fingerprint REAL é gerado pelo Camoufox no motor: Canvas, WebGL(GPU), Audio, fontes, navigator, **TLS/JA3, HTTP/2** (ver `session_manager.py` cabeçalho). Idioma/timezone vêm do **geoip** (IP do proxy) em runtime. Resumo: o que o app exibe é simples; o real é profundo. **Gotcha:** travar `Screen` na resolução EXATA pode quebrar a geração do fingerprint no browserforge — por isso `_open_camoufox` tem cascata de fallback (pin_screen→sem pin→sem geoip→sem constrain_screen).

## Fluxo de criação — `instagram.py` `create_account()` (TUDO trusted)
Abre `accounts/emailsignup/` em página dedicada. Preenchimento humano real:
- **Email/senha/nome:** digitação de teclado real — `_fill_text_fields_trusted` → `_signup_field_handle` (evaluate_handle acha o campo) + `_type_like_human` (clique real, Ctrl+A/Delete, `keyboard.type` letra a letra). Substituiu injeção JS de valor (sinal detectável).
- **Aniversário:** `_fill_birthday_trusted` — `<select>` nativo (`select_option`) ou dropdown custom (`_pick_custom_dropdown`: gatilho por NOME month/day/year, **exclui links e o botão `?`**, clica real + opção por texto exato). Antes o JS clicava no `?`/links por índice e abria abas + errava a data.
- **Username:** `_fill_username_trusted` — alta entropia LOCAL (`_gen_username_strong`: slug+4-5 dígitos). Só regenera se o IG marcar AQUELE nome como indisponível (`_username_taken_for`, checa a frase mencionando o candidato). **NUNCA lê sugestões da página** (já pegou lixo tipo "terms"/"jobs" do rodapé). `gen_full_name` remove dígitos do email (`[^a-zA-Z]`, partes len>=2) → "gabriel56255" vira "Gabriel Ribeiro" → username sem números colados, mantendo coerência email↔nome.
- **Submit:** `_submit_trusted` — `ElementHandle.click()` REAL (o IG ignora clique JS sintético `isTrusted=false`).
- **Guarda anti-bug:** se já avançou (tela de código / fora de emailsignup) → não refaz o fill (senão escrevia o email no campo do código).
- `_page_fill` (JS inline) sobrou como utilitário; `allowDate` sempre False.
- Diagnóstico em `submit_failed`: salva screenshot + texto da tela + URL em `LOGS_DIR`.

A extensão `ig-filler-extension/content.js` é só para sessões MANUAIS (popup); existem 2 cópias divergentes (Desktop vs antidetect-camoufox) — tech debt. NÃO preenche username por design.

## Captcha manual + alerta (trabalhar livre, ser chamado só p/ o captcha)
`_is_captcha_screen` detecta (iframe recaptcha ou textos). `_wait_manual_captcha(timeout=420)` faz `_set(status="waiting_captcha")` + polling até resolver / aparecer tela de código. `_alert_captcha`: som (`winsound.MessageBeep` em executor) + `page.bring_to_front()` + `_win_focus_browser()` (ctypes user32: EnumWindows classe 'MozillaWindowClass' → SW_RESTORE + SetForegroundWindow); re-bipa a cada ~20s. Janela visível pq criação roda `headless=False` (`_ensure_session_with_page` em api.py). PENDENTE opcional: auto-minimizar no launch (cada conta foca a janela; `-foreground` vem do Camoufox — não feito por risco aos cliques com janela minimizada).

## Telefone → descarte
Tela de telefone sem `sms_key` → status `flagged_phone` → `_ACCOUNT_STATUS` mapeia p/ status persistido **"discarded"** (NÃO "banned", pra não poluir ban_rate). api.py `_create_account_for_email`: `flagged_phone` → `sm.stop` (fecha janela, libera proxy/lock). `_verify_whatsapp` (sms-activate, service "wa") existe mas só roda se passar a chave.

## Persistência de métricas (sobrevive a apagar perfil/conta)
Apagar PERFIL faz **cascata** (api.py remove emails+contas vinculados) → perdia métrica. Agora:
- Tabela **`account_history`** (db.py, PK=account_id, upsert) gravada em `accounts.set_status`/`record_new_account`/`delete_account`.
- Métricas (`get_ban_analytics`, `proxy_outcomes`) leem de `accounts._merged_accounts()` = contas vivas ∪ histórico (dedup por id).
- **Ledger de aprendizado:** tabela `creation_attempts` (campos: proxy_key, os, warmup_minutes, from_pool, outcome, **gate**=success/phone/captcha/email_pending/submit_failed/error via `_derive_gate`, step, detail). Gravado no `finally` de `_create_account_for_email`. Endpoints `GET /learning/attempts` e `/learning/summary`. PRÓXIMO (com volume): insights, quarentena de proxy, auto-tune.

## Risco do perfil (`assess_profile`, api.py)
Penaliza histórico real do proxy (`proxy_outcomes`: banned/suspended alto+escalona, checkpoint médio, discarded baixo) + reachability (`_proxy_test_cache`: proxy caído = alto). Com 0 contas, só reachability afeta. Card de perfil mostra "Status" (warming/ready/in_use ou aquecido com tempo decorrido).

## Pool de perfis pré-aquecidos (`warm_pool.py`)
Mantenedor em background (startup hook `warm_pool.start()`) mantém N (`warm_pool_size`=5) perfis `ready` (coluna `profiles.pool_status` warming/ready/in_use; `warm_started_at` p/ tempo). Aquece até 2/vez headless por `warm_pool_minutes`(15) com backoff; `recover_orphans()` no boot remove 'warming' órfãos; `asyncio.wait_for` timeout no aquecimento. `claim()` reserva ready→in_use. `create-batch` puxa do pool, fallback cria+aquece inline. `GET /warm-pool/status`; barra na aba Perfis (`loadWarmPool`/`#warm-pool-bar`). Desligar: setting `warm_pool_enabled=0`.

## Aba WhatsApps (Utilitários)
Cadastro MANUAL de números + controle de uso (free/used). Tabela `whatsapp_numbers`; endpoints `/whatsapps` (GET, POST lote, PATCH, DELETE). Nav em "Utilitários" → `page-whatsapps`. SÓ controle manual, não integra ao fluxo.

## Defaults anti-detecção (subidos)
warmup navegador **15min**, intervalo entre contas **90s**, account_warmup **5min** (api.py `IgCreateBatchRequest` + frontend). Aba "Comece aqui" atualizada com o fluxo real (captcha manual, descarte de telefone, proxy residencial como fator nº1).
- **Criação agora é SÓ MANUAL via Camoufox (2026-06-17).** Removidos os botões "Criação em massa segura" (`createMass`) E "Criar contas" em lote (`createSpecificNumber`) + campos quantidade (`ig-bulk-qty`) e "aquecer conta" (`ig-bulk-acctwarm`). A aba Criar Contas (PC) virou **criação manual**: `createManual()` (botão primário "Criar conta (abrir Camoufox)") gera perfil+email+dados, aquece o navegador (`ig-bulk-warm`) e abre a janela do Camoufox p/ o usuário preencher. Mantido o seletor de método de proxy (local/inteligente/fixo) que o `createManual` consome. As funções JS de lote/massa e os endpoints (`/instagram/create-batch`, `/email/create-accounts-free`) continuam no código (sem botão) — decisão do usuário: criação em lote/massa é irreal.
- **Aba Criar Contas (PC) REFORMULADA p/ o fluxo manual (2026-06-17).** Removido do VISÍVEL tudo que não condizia: cards Proxy Inteligente/Fixo + capacidade ("até X contas"), card "ancoragem/tethering" (tethering foi descartado), seção SMS/sms-activate, "Aquecer conta no Instagram (no PC)", barra de progresso de lote, e o **formulário automático `igCreate`** (o `ig-workspace` ficou `display:none` — HTML legado oculto, tech debt; `igLoadProfiles` guardado p/ não quebrar). A página agora tem: passo-a-passo do fluxo → **Conexão de saída** (Proxy do pool 4G/5G [value `inteligente`] vs Conexão local [value `local`, lido por `createManual`]) → aquecer navegador → botão **"Criar conta (abrir Camoufox)"** → lembrete de amadurecer no Android. `onProxyMethodChange`/`loadIgProxyCapacity` já eram guardados.
- **Aba "Comece aqui" REESCRITA p/ o fluxo atual (2026-06-17).** Antes descrevia "criar no app Android, PC desativado" (pivot 2026-06-15) — contradizia o fluxo manual no PC. Agora: criação **manual no Camoufox (PC) + proxy 4G/5G** → **amadurecer no Android** → monitorar/extrair (Spider). Passos: (1) configurar proxy 4G/5G + País-alvo + emails; (2) criar conta manual (aba Criar Contas); (3) amadurecer no Android; (4) monitorar/Spider. Boas práticas atualizadas (4G/5G dedicado sticky, fingerprint DESKTOP coerente — não mobile, ritmo lento, marcar queimado, amadurecer no Android). Card de saúde de proxies (topo da aba) mantido.
- **E-mails agora COMPRADOS de terceiros (2026-06-17, decisão do usuário).** Catch-all/IMAP descartados. Textos de "Comece aqui" + "Criar Contas" atualizados (sem catch-all/IMAP; "cadastre os e-mails comprados na aba Emails"; código pego no provedor do e-mail, não por IMAP). Hint do modal manual ajustado. **PENDENTE (perguntado ao usuário):** o backend `createManual`/`/instagram/create-manual` ainda GERA um e-mail catch-all e o modal tenta IMAP (cai no fallback "pegue no provedor" agora). Decidir se a criação manual passa a usar um e-mail comprado (escolhido na aba Emails) em vez de gerar — mexe no backend. Menções residuais a catch-all só no `ig-workspace` oculto e em comentários.
- **Aba "Criar Contas (PC)" VOLTOU ao menu (2026-06-17).** No pivot Android (2026-06-15) o link foi removido do menu (a página `#page-instagram` e os endpoints sempre continuaram). Como o usuário vai **testar criação no PC com proxy 4G/5G**, o link voltou (em "Início", abaixo de "Comece aqui", rotulado **"Criar Contas (PC)"** → `showPage('instagram')`). É aí que estão TODAS as opções antidetecção: aquecer navegador/conta, intervalo entre contas, conexão local (sem proxy) vs pool, etc. Fluxo principal segue Android.

## Instabilidade conhecida
`TargetClosedError` (Camoufox fecha no meio) e `Execution context was destroyed` (navegação durante o fill) aparecem nos logs (`%LOCALAPPDATA%\ProfileManager\logs\app.log`/`errors.log`). `Path` está importado no topo de instagram.py (já causou NameError antes).

Melhorias em Rastreamento de Proxies, Status e Visibilidade (Junho 2026)
Objetivo principal atual do projeto:

Maximizar o tempo de vida das contas usando o spider.
Ter um processo confiável de criação quando as contas caem.
Atualmente o usuário está sem contas.

Problema atual:

Não é possível ver facilmente qual proxy/IP está sendo usado durante a criação.
Não existe controle de status dos proxies (limpo, sujo, em quarentena, etc.).
Os dados de uso de proxy já são parcialmente registrados em creation_attempts, mas não são bem aproveitados para visualização, avaliação de bans/suspensões e aprendizado do sistema.
O usuário precisa dessa informação visível na aba “Comece aqui”, junto com a explicação do fluxo.

Estratégia recomendada (híbrida):

Criação: PC com Camoufox + proxy mobile 4G/5G (preferencialmente sticky).
Aquecimento inicial: App nativo do Instagram no celular (forte recomendação para aumentar longevidade).
Uso do spider: Após a conta já ter passado pelo aquecimento mobile.
Usar fingerprint mobile-like no Camoufox sempre que possível.

Requisitos de implementação:

Rastreamento e Status de Proxies
Garantir que o proxy_key (e IP quando disponível) seja sempre registrado de forma consistente.
Criar/atualizar estrutura para controlar o status de cada proxy.
Atualizar o status automaticamente durante o fluxo de criação (assign(), _create_account_for_email, etc.).
Status sugeridos:
clean — Proxy em bom estado para uso
in_use — Sendo utilizado no momento
warming — Em aquecimento
quarantined — Pausado temporariamente (alto índice de falhas recentes)
dirty / marked — Alto índice de bans ou suspensões
banned — Não usar

Visibilidade no Frontend
Na aba “Comece aqui”, exibir de forma clara e visível:
Qual proxy está sendo usado no momento (proxy_key + IP se disponível)
Status atual do proxy
Informações úteis (ex: contas criadas recentemente com ele, taxa de sucesso/falha recente)


Integração com Métricas e Aprendizado
Fortalecer o uso dos dados de proxy nas métricas existentes (proxy_outcomes, creation_attempts, get_ban_analytics).
Permitir avaliação da “saúde” dos proxies com base em outcomes recentes.
(Desejável) Implementar lógica simples de quarentena automática baseada em histórico de falhas.

Requisitos Gerais
Manter compatibilidade com o sistema atual de atribuição de proxies.
Preferir soluções incrementais e simples.
Atualizar o CLAUDE.md com as mudanças realizadas.
Documentar como o status do proxy é atualizado.


Prioridades sugeridas:

Alta: Visibilidade do proxy atual + status na aba “Comece aqui”.
Média: Atualização automática de status durante a criação + integração com métricas.
Baixa: Lógica de quarentena automática.

Observações importantes:

Tethering (rotear internet do celular pro PC) não resolveu o problema de forma satisfatória (inconsistência entre IP mobile e fingerprint desktop).
Proxies mobile 4G/5G dedicados são fortemente recomendados para melhorar tanto a criação quanto a longevidade das contas.
Manter o estilo de mudanças pequenas e bem testadas.

### Status de implementação
**Parte 1 — Visibilidade do proxy em uso (FEITA, 2026-06-16).** Indicador GLOBAL no header (`static/index.html`, `#proxy-indicator`) — fica FORA do `<main>`, então é visível em QUALQUER aba (inclusive "Comece aqui"). Mostra o proxy da conta sendo criada AGORA: `país · host:porta` (ou "Conexão local (celular)" sem proxy, ou "Proxy: sem criação ativa" ocioso). **Cor: verde = livre pra ir; vermelho = atenção** (`.proxy-ok`/`.proxy-warn`). Tooltip detalha uso/ban/reachability/email.
- **Endpoint `GET /proxies/active`** (api.py): acha o item em `status=="creating"` no job de criação em execução mais recente (`_ig_batch_jobs`) e devolve `{active, level: ok|warn|idle, local, email, username, proxy_key, host, country, status:{used, ban_rate_pct, blocked, reachable, recommended_remaining}}`. **Reaproveita tudo:** `pool.summary()` (uso/ban/blocked), `_proxy_test_cache` (reachability — só LEITURA, não dispara teste), `pool.proxy_country()` (país offline GeoLite2). Regra `warn`: `blocked` OU `reachable is False` OU `ban_rate_pct >= 20`.
- Item do job ganhou `proxy_key`/`proxy_host` (preenchidos quando entra em `creating`, lidos do perfil; inicializados em `None`).
- Frontend: `loadActiveProxy()` no intervalo global de 3s (`pollInterval`) + no init. Fetch falho = silencioso (estado ocioso). **Sem tabelas novas, sem mudança de schema.** Validado em preview estático (verde/vermelho/ocioso; layout intacto).
- **OBS:** hoje cobre só o proxy da criação EM ANDAMENTO. As "infos úteis por proxy" (contas recentes, taxa sucesso/falha) e um painel DENTRO da aba "Comece aqui" ainda NÃO foram feitos — virão na Parte 3 (métricas). Como a criação automática pelo PC está desativada (pivot Android), o indicador só acende quando houver lote rodando no PC.

**Parte 2 — Rastreamento consistente + saúde por proxy (FEITA, 2026-06-17).**
- **`proxy_key` consistente:** o fallback no `finally` de `_create_account_for_email` agora usa `pool.proxy_key()` (mesma função que chaveia pool/contas) em vez de um f-string próprio — garante JOIN entre `creation_attempts`, `proxy_outcomes` e o pool. Formato `host:port` idêntico em `proxies.proxy_key`/`accounts.proxy_key`.
- **Endpoint `GET /proxies/health`:** saúde por proxy juntando `pool.summary()` (uso/ban/blocked) + `accounts.proxy_outcomes()` (desfechos reais: banned/suspended/checkpoint/discarded/bad_rate) + funil de criação por proxy de `creation_attempts` (gates) + reachability (`_proxy_test_cache`, só leitura) + país (offline). **Rótulo derivado por REGRA** em `_derive_proxy_health`: `unknown` (sem dados), `banned` (total≥3 e bad_rate≥60), `dirty` (total≥1 e bad_rate≥30), `clean`. **Sem tabela/coluna nova.** Itens ordenados pior→melhor.
- **Frontend:** coluna "Saúde" na aba Proxies (`renderProxyHealth`): 🟢 limpo / 🟡 atenção / 🔴 queimado / cinza sem-dados, com tooltip (desfechos + funil + reachability). Validado em preview.
- **OBS escopo:** os status `in_use`/`warming`/`quarantined` da spec NÃO viram coluna persistida — `in_use` já é o indicador do topo (Parte 1), `warming` é do warm_pool, `quarantined` é a Parte 4. A "saúde" é DERIVADA on-the-fly (regra), não armazenada — mais simples e suficiente p/ a régua do teste 4G/5G.

**Régua do teste 4G/5G (estratégia escolhida 2026-06-16):** criar no PC com proxy 4G/5G dedicado sticky + **fingerprint DESKTOP coerente** (NÃO mobile — `fingerprint.py` é desktop-only por design; mobile no Camoufox = incoerente). Lote pequeno, ritmo lento, e depois ler `/proxies/health` p/ ver se o IP nasceu limpo. Logar/amadurecer a conta no Android real depois (atenção: troca de dispositivo pode pedir verificação).

**Parte 3 — Status automático + visibilidade ociosa (FEITA, 2026-06-17).**
- **Indicador do topo = LEMBRETE permanente do proxy + saúde (limpo/sujo).** `/proxies/active` retorna `mode` em prioridade: `creating` (conta sendo criada) → `in_use` (perfil aberto AGORA via `sm.list_active()`) → `pool_one` (parado, 1 proxy: mostra ELE) → `pool_many` (parado, vários: resume pelo PIOR + `dirty_count`) → `empty` (pool vazio). **NUNCA "próximo" do `assign()`** — mostra o proxy REAL em uso ou o que você TEM no pool, SEMPRE com `health` (limpo/sujo/queimado, mesma regra de `/proxies/health`) pra responder "meu IP está limpo?". Intenção do usuário (2026-06-17): é um lembrete de proxy sujo, visível mesmo parado. `_proxy_status_block` agora inclui `health` e aceita summary/outcomes pré-computados (evita re-scan no `pool_many`). Frontend: pill "🌐 país·host · limpo/SUJO" (verde/vermelho) ou "N proxies · X sujo(s)"; card na "Comece aqui" idem ("Seu proxy: … [limpo]").
- **Reachability automática:** helper `_mark_proxy_reachable(key, ok, ...)` atualiza o `_proxy_test_cache` a partir da **checagem TCP do worker** de criação (online com latência / offline com erro) — antes só `/proxies/test` manual atualizava. A chave usa `pool.proxy_key` (consistente). Saúde/indicador refletem proxy caído na hora.
- **Card de saúde na aba "Comece aqui":** topo da aba mostra "Proxies — em uso agora e saúde" (`loadStartProxyHealth`): linha do proxy em uso/próximo + lista compacta (até 6) com badge de saúde + contas + taxa de criação (reusa `/proxies/active` e `/proxies/health`). Atualiza no `showPage('start')` e no poll. Validado em preview.

- **Caixa de status maior (Meu IP real vs Proxy) + painel com tudo (2026-06-17).** A pedido: ver SEMPRE o IP real do PC e o proxy, pra comparar. Header virou `.proxy-box` de 2 linhas: "Meu IP: <ip>·<país>" (IP REAL via `_my_public_ip` — urllib + ip-api.com, cache 120s) + "Proxy: …·limpo/SUJO". Clicar abre `#proxy-panel` (dropdown ancorado à direita, `right:0`) com TUDO: Meu IP+país, Proxy em uso/atual (saúde, conectividade, ban, contas), comparação de país (proxy≠conexão = aviso informativo), e resumo do Pool (total/limpos/sujos/queimados/sem-dados). `/proxies/active` agora também retorna `my_ip`/`my_ip_country` (via `_active_core` + wrapper) e `pool` (`_pool_health_counts`). O card da "Comece aqui" também ganhou a linha "Meu IP".

- **Marcação MANUAL "queimado" (2026-06-17, a pedido).** Você pode marcar um proxy (host:porta) OU um IP cru (ex.: seu IP real quando cria via conexão local) como queimado. **Tabela nova `proxy_flags`** (db.py: `key` PK, `status`, `reason`, `marked_at`; funções `set_proxy_flag`/`delete_proxy_flag`/`list_proxy_flags`). Efeitos: (1) `proxies.assign()` **filtra** chaves `banned` (para de criar contas nelas; se esvaziar o pool, mantém o original p/ não travar); (2) saúde vira `banned`/queimado e `manual:true` em `_proxy_status_block`, `/proxies/health`, `_pool_health_counts` (a marca manual VENCE a regra automática); (3) `/proxies/active` retorna `my_ip_flagged`. **Endpoint `POST /proxies/mark` {key, status: banned|clean, reason}** (clean = libera). UI: botões "🔥 Marcar como queimado / ♻ liberar" no painel do topo (proxy atual + meu IP) e por linha na aba Proxies (coluna Saúde). **Feedback visual claro (a pedido):** ao marcar, o IP/caixa fica VERMELHO com "🔥 QUEIMADO" e o botão vira "♻ liberar"; quando não marcado, VERDE "● bom p/ uso". A caixa do topo fica vermelha se o proxy estiver em atenção OU o meu IP estiver marcado. Isto cobre a quarentena MANUAL; a automática segue pendente.

**PENDENTE — Parte 4 (quarentena AUTOMÁTICA, baixa):** pausar proxy no `assign()` por janela após N falhas recentes (reaproveitar `proxy_flags` com um status temporário ou TTL + o rótulo `dirty`/`banned` do health). Decisão firme: **regras + estatística, sem IA.**