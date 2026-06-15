# AntiDetect Camoufox - Development Context

**Última atualização:** 2026-06-14 (Codex)

Este arquivo existe para que não seja necessário recomeçar do zero toda vez que abrirmos o projeto.  
**Sempre leia este arquivo primeiro** ao retomar o trabalho.

---

## 📌 Visão Geral do Projeto

- **Nome do repositório no GitHub:** AntiDetectMTZ (privado)
- **Owner:** thiagokcw-coder
- **URL:** https://github.com/thiagokcw-coder/AntiDetectMTZ
- **Pasta principal de desenvolvimento:** `antidetect-camoufox/` (esta é a versão "oficial" / atualizada)
- **Pasta secundária:** `antidetect-dev/` (cópia mais antiga / experimental – atualmente bagunçada com worktree do OneDrive)

**Propósito:** Gerenciador de perfis de navegador antidetect usando **Camoufox** (Firefox stealth) + FastAPI + interface web. Foco em automação de criação e aquecimento de contas Instagram com proxies, SMS-Activate e emails catch-all.

---

## ✅ O que já foi feito (histórico recente)

- Correção do erro de launch de perfis no Camoufox:
  - Problemas: `AttributeError: 'dict' object has no attribute 'is_set'` e "No headers based on this input can be generated"
  - Solução: Adicionado parâmetro `constrain_screen` em `_camoufox_kwargs` + cadeia de 5 fallbacks em `_open_camoufox` (session_manager.py)
  - Aplicado nas duas pastas (camoufox e dev)

- Redesign completo do layout da aba **"Criar Contas"** (página Instagram):
  - Fluxo recomendado (automático) destacado no topo
  - Formulário manual mais organizado em duas colunas
  - Status + Aquecimento de conta mais limpos
  - Aplicado em ambas as pastas mantendo compatibilidade total com o JavaScript existente

- Git:
  - Criado `.gitignore` forte (ignora venv, __pycache__, debug.log, proxys/, .installed, etc.)
  - Repositório inicializado e commitado dentro de `antidetect-camoufox/`
  - Usuário criou o repo no GitHub como **AntiDetectMTZ**
  - Instalado **GitHub Desktop** para facilitar (evitar comandos manuais no PowerShell)
  - README.md criado

- Arquivos criados recentemente:
  - `README.md` (descrição do projeto)
  - `DEVELOPMENT_CONTEXT.md` (este arquivo)

---

## Estado atual do fluxo de criação (2026-06-14)

Esta seção substitui as notas antigas abaixo que mencionam a extensão `ig-filler-extension` como caminho principal da automação.

- A automação de criação de conta não deve mais depender da extensão para preencher o cadastro do Instagram.
- O fluxo principal agora fica em `instagram.py`, usando `_page_fill(page, payload)` para preencher e enviar diretamente no contexto da página.
- `_page_fill` tenta preencher contato, senha, nome completo e data por estrutura do DOM/inputs visíveis, sem depender do idioma da página.
- A data de nascimento não tem mais fallback Python separado. O preenchimento da data acontece dentro do mesmo preenchimento da página.
- O Python deve atuar principalmente no preenchimento controlado e no clique final de submit/enviar. A intenção é evitar que a extensão mexa no cursor, recarregue a tela ou perca os dados.
- O antigo `_ext_fill` e o carregamento automático da extensão foram removidos/desativados do caminho automático.
- `api.py` deixa `auto_load_filler = False` nos lançamentos manuais e em lote.
- `session_manager.py` mantém `load_ig_filler=False` por padrão; só use a extensão em teste manual específico.
- `ig-filler-extension/content.js` ainda existe no projeto e pode estar modificado por tentativas anteriores, mas não deve ser considerado o fluxo principal.
- Validação feita após a mudança:
  - `python -m py_compile instagram.py api.py session_manager.py`
  - Busca por `_ext_fill`, `_filler_path`, `authorizedRegistrationFiller`, `auto_load_filler = True` e `load_ig_filler=True` nos arquivos principais não encontrou usos ativos.
  - `git diff --check -- instagram.py api.py session_manager.py`

Próximo ponto importante: testar o fluxo real de criação de conta no Instagram com páginas em idiomas variados. Se falhar, o ajuste deve ser feito em `_page_fill`, não reativando a extensão como caminho principal.

Atualização após teste com fechamento da tela:

- Se a janela fechar durante erro de cadastro, verificar primeiro `session_manager.py`: o gerenciador agora observa todas as abas vivas do contexto, não apenas a primeira aba `about:blank`.
- Em `api.py`, o fluxo de lote por email não fecha mais automaticamente a janela quando o cadastro termina em `error`; ela fica aberta para diagnóstico. Cancelamento manual ainda pode fechar.
- Em `instagram.py`, o primeiro preenchimento não tenta mais preencher data de nascimento. Ele preenche apenas contato, senha e nome. Dia/mês/ano só são preenchidos quando `_date_controls_ready(page)` detectar controles reais de data.
- `_page_fill` agora ignora data com `allowDate=False` e retorna `dateSkipped` quando recebe data mas a tela não parece ser de aniversário.
- Se o app continuar fechando logo após uma correção, confirmar se ele foi reiniciado. Em 2026-06-14 foi detectado processo Python antigo ainda rodando antes das mudanças.
- `session_manager.stop()` e `shutdown_all()` agora registram no `debug.log` o motivo e o ponto de chamada do fechamento.

---

## 🗂️ Estrutura Importante

```
AntiDetect by Grok/
├── antidetect-camoufox/          ← TRABALHE AQUI (repo principal)
│   ├── static/index.html         ← UI (layout da aba Criar Contas foi redesenhado aqui)
│   ├── session_manager.py        ← Lógica de lançamento de navegadores (fix de launch aqui)
│   ├── run.bat
│   ├── requirements.txt
│   ├── proxys/                   ← **NUNCA commitar** (contém credenciais reais)
│   ├── venv/                     ← Ignorado pelo .gitignore
│   └── debug.log                 ← Ignorado
│
├── antidetect-dev/               ← Cópia secundária (evite por enquanto)
│   └── (tem worktree bagunçado ligado ao OneDrive)
│
└── antidetect-camoufox-v1.1-atualizado.zip  ← Backup/versão anterior
```

**Pastas/arquivos que DEVEM ser ignorados no Git:**
- `venv/`
- `__pycache__/`
- `debug.log`
- `proxys/`
- `.installed`
- `*.zip`
- user_data gerados pelo app (vão para %LOCALAPPDATA%\ProfileManager)

---

## 🚀 Como Rodar o Projeto

1. Entre na pasta `antidetect-camoufox`
2. Execute `run.bat` (recomendado)
   - Ele cria venv, instala tudo e baixa Camoufox na primeira vez
3. Alternativa manual:
   ```powershell
   .\venv\Scripts\Activate.ps1
   python main.py
   ```

O app abre interface via PyWebView (ou navegador como fallback).

---

## 🛠️ Git / GitHub (Status Atual)

- Usando **GitHub Desktop** (muito mais fácil que PowerShell)
- Remote: `https://github.com/thiagokcw-coder/AntiDetectMTZ.git`
- Branch principal: `main`
- Últimos pushes incluem:
  - Fixes de launch
  - Novo layout da aba Criar Contas
  - README.md

**Dica:** Sempre use o GitHub Desktop para commit/push daqui pra frente. Evita erros de "origin does not appear", remote, etc.

---

## 📝 Tarefas / Próximos Passos (abertos)

- [x] Corrigir "tudo preenchido, cursor move sozinho, página atualiza, bota DOB de novo e para" (submit não avança):
  - Usuário relatou: extensão/Python preencheu tudo (incluindo DOB custom dropdowns), depois o human simulation moveu o cursor, IG atualizou/re-renderizou a página (resetou os dropdowns de Data de nascimento), script tentou preencher DOB de novo e parou sem submit.
  - Causa: Formulário "tudo em uma tela" (como na imagem) é dinâmico. Sugestão de username, cliques nos comboboxes customizados ou validação interna do IG causam re-render, perdendo os valores dos dropdowns de DOB. Submit "Enviar" não pegava porque os campos estavam vazios ou o estado React não estava committed.
  - Fix:
    - DOB fill explícito como "última ação" logo antes de cada tentativa de submit (extensão com day/month/year + Python _fill_birthday).
    - No loop de submit: após cada clique/Enter, detecta se ainda estamos no form inicial (password visível OU comboboxes/Dia visíveis). Se sim → refaz DOB fill fresco (recupera do update da página) + re-tenta "Enviar". Até 4 tentativas.
    - Early DOB reinforcement já detecta comboboxes também.
  - Isso trata exatamente o fluxo "preencheu → cursor sozinho (human) → página atualizou + DOB de novo → retry com refill → submit".
  - Adicionado DOB no payload inicial da extensão estava fazendo o fill tentar localizar triggers de data de nascimento que ainda não existem na primeira tela de signup → timing ruim, interferia no estado do form e no clique do botão de submit.
  - Removido birthday do payload da chamada inicial de extensão (só campos principais: contato/senha/nome). DOB continua sendo passado nos blocos dedicados de aniversário (pré e pós submit), que é onde os controles realmente aparecem.
  - Atraso humano maior (1.8~3.2s) obrigatório depois do preenchimento + captura de username sugerido pelo IG e antes do submit (React precisa estabilizar).
  - `_click_signup` muito mais robusto: + seletores ("Criar conta", "Cadastrar", "Join", etc.), mais variações de role=button, fallback amplo com regex, 3 tentativas + Enter + dismiss entre elas.
  - Bloco de submit ganhou mais retries, delays e verificação de "ainda no form".
  - Resultado esperado: submit deve agora realmente disparar o avanço do form.
  - create_account retornava imediatamente após preencher código (email ou WhatsApp) ou sem verificação, sem esperar a transição real para o feed/logado.
  - Callers (especialmente _create_account_for_email usado pelos fluxos de "emails livres" + perfis prontos) davam await sm.stop() no finally → janela fechava antes do usuário ver o login acontecer.
  - Agora: após código preenchido (com sucesso), create_account chama _confirm_login_success (espera até 50s por elementos de Home/Reels/feed + dismiss de overlays) antes de retornar "created"/"logged_in".
  - No wrapper de criação por email: em caso de sucesso com login confirmado, NÃO para o perfil e dispara `asyncio.create_task(ig.warmup(page, profile_id, mins))` — aquecimento roda em background na mesma sessão/janela enquanto o lote segue para a próxima conta.
  - Janela só é fechada automaticamente em erro/cancel explícito. Para estados intermediários (aguardando código manual) a janela fica aberta para intervenção.
  - Submit do formulário de cadastro também ganhou mais retries/dismiss.
  - Também aplicado no path de WhatsApp e caso sem verificação.
- [x] Corrigir preenchimento de data de nascimento (DOB) ignorado na criação de contas:
  - `bd` nunca era extraído do `data` dentro de `create_account` (causava except silencioso).
  - Fluxos de batch/free-email (usados com os perfis pré-criados) nunca geravam nem passavam birthday.
  - Payload enviado para a extensão ig-filler nunca incluía day/month/year → a lógica avançada de fillDatePart (custom dropdowns + scroll/keyboard) nunca rodava para DOB.
  - Solução: gen_birthday() + sempre injetar bd + enriquecer payload da extensão + tentar extensão primeiro nas telas de aniversário (Python como fallback).
  - Aplicado em instagram.py + api.py (paths de create-account, _create_account_for_email e free batch).
- [ ] Limpar / decidir o que fazer com a pasta `antidetect-dev/` (está com worktree estranho)
- [ ] Adicionar a pasta `firefox-extension` que está faltando (necessária para carregar extensão em alguns fluxos)
- [ ] Melhorar ainda mais a UI (ex: mais cards, status em tempo real, métricas de proxies)
- [x] Adicionado timeout no connect do Postgres e wrapper no on_startup para evitar hang eterno no iniciar (conexão PG bloqueante quando database_url.txt ou DATABASE_URL está presente mas servidor indisponível). O wait_api_ready agora falha rápido e mostra a página de erro apontando para os logs.
- [ ] Testar o fluxo completo de criação de conta após o push (incluindo injeção da extensão IG form filler)
- [ ] Adicionar mais documentação (ex: como configurar Hostinger catch-all + IMAP)
- [ ] Verificar se há outros bugs relatados pelo usuário (ex: ao abrir o projeto)
- [ ] Commitar este `DEVELOPMENT_CONTEXT.md`

---

## 💡 Dicas para Futuras Sessões

- **Sempre leia primeiro:** `DEVELOPMENT_CONTEXT.md` + `README.md`
- Evite trabalhar na pasta `dev` por enquanto.
- O foco atual é a pasta `antidetect-camoufox`.
- Se precisar de ajuda com Git, use o GitHub Desktop (não PowerShell).
- Se quiser mostrar erro ou tela, salve como imagem e envie o caminho do arquivo (o Grok consegue ler imagens assim).

---

**Última nota:**  
Este projeto foi trabalhado com assistência do Grok. O objetivo é manter o desenvolvimento organizado e não recomeçar do zero a cada interação.

Se precisar atualizar este arquivo, é só pedir: "atualize o DEVELOPMENT_CONTEXT.md com ..."

Boa sorte com o AntiDetectMTZ! 🚀
