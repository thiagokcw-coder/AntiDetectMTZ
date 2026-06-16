# AntiDetect Camoufox

Gerenciador de perfis de navegador **antidetect** baseado em **Camoufox** (Firefox com stealth avançado via Playwright), com backend FastAPI e interface web.

Cria e gerencia perfis com fingerprints realistas e proxies, e serve como **central de cadastro, guarda e monitoramento de contas** (foco em Instagram). Pode rodar **sozinho** (SQLite local) ou **compartilhado entre vários usuários** (PostgreSQL).

> **📌 Direção atual (importante):** os testes mostraram que criar contas de forma **totalmente automatizada pelo PC** esbarra na detecção do Instagram (correlação de dispositivo + automação) — mesmo com IP móvel limpo e comportamento humano, a conta costuma nascer sinalizada. Por isso o **fluxo recomendado** é:
> 1. **Criar as contas num dispositivo real (Android, app oficial do IG)** — onde o IG menos desconfia;
> 2. usar este app como **painel central**: cadastrar credenciais, organizar por perfil/proxy e **monitorar o status**.
>
> A **criação assistida (modo manual)** — o app gera os dados e abre a janela, você preenche — e a criação automática continuam disponíveis, mas como apoio, não como caminho principal.

---

## ✨ Funcionalidades

- **Perfis** com fingerprint coerente (browserforge + Camoufox), proxy e geoip — fuso/idioma derivados do IP do proxy em runtime.
- **Lançamento persistente** do navegador (cookies/histórico salvos por perfil), com cadeia de fallbacks que evita erros de geração de fingerprint.
- **Criar / editar / remover perfis** pela interface — incluindo trocar o proxy (manual ou escolhido do pool com status visível).
- **Cadastro e monitoramento de contas** (foco do uso atual): registre contas criadas em qualquer lugar (ex.: no celular), com usuário/senha/email/WhatsApp/status, e acompanhe o estado de cada uma (ativa, checkpoint, banida…).
- **Verificação de status automática**: ao abrir um perfil e fechar a janela, o app reabre em segundo plano, detecta no IG e atualiza o status — sem clicar em nada. Também há o botão "Verificar".
- **Criação de contas Instagram** (apoio):
  - **Modo manual** (recomendado p/ testes): o app gera os dados (email/senha/nome/nascimento/username), aquece o navegador e abre a janela no cadastro — **você preenche** copiando do popup. O código do email aparece no popup.
  - **Conexão local (tethering)**: cria/abre pela internet do PC — ex.: ancoragem USB do celular = **IP móvel real** (melhor reputação). O geoip ajusta idioma/fuso ao IP.
  - Automática em lote com emails **catch-all + IMAP**, **SMS-Activate**, comportamento humano e data de nascimento gerada.
- **Distribuição inteligente de proxies** por ban rate real (modo "Proxy Inteligente") ou limite fixo, com **país-alvo** (coerência geográfica).
- **Aquecimento** de perfis (navega sites comuns) e de contas (atividade no IG) em segundo plano.
- **Importação em massa via CSV** (sequencial, com intervalo entre contas).
- **Aba Perfis**: busca + filtros (com/sem conta, conta logada, aquecidos), ordenação por status e **destaque verde para perfis com conta IG logada**.
- **Monitoramento**: status das contas, vida útil, uso de proxies/WhatsApp e métricas de ban.
- **Lock de perfil em uso**: impede que duas máquinas abram o mesmo perfil ao mesmo tempo (essencial no modo compartilhado).
- **Modo compartilhado** (PostgreSQL) ou **local** (SQLite) — automático conforme a `DATABASE_URL`.
- Interface web (PyWebView ou navegador) com design moderno.

---

## ⚙️ Requisitos

- Windows 10/11 **ou** macOS 12+ (Linux também funciona)
- Python 3.11+ (recomendado)
- Conexão com internet (primeira execução baixa ~150 MB de Camoufox + Firefox)
- (Opcional) Um PostgreSQL hospedado para o modo compartilhado

---

## 🚀 Como rodar

1. Entre na pasta do projeto (`AntiDetectMTZ`).
2. Execute o inicializador:
   - **macOS / Linux:** `MonitorMtzMac.command` (duplo-clique no Finder) ou `./MonitorMtzMac.command` no terminal
   - **Windows:** `MonitorMtzWin.bat`

   Na primeira vez ele:
   - cria o ambiente virtual (`venv/`),
   - instala as dependências (`requirements.txt`),
   - instala o Firefox do Playwright e baixa o Camoufox,
   - abre a interface.

   > No macOS, se o Gatekeeper bloquear o `MonitorMtzMac.command` ("desenvolvedor não identificado"),
   > clique com o botão direito → **Abrir** (só na 1ª vez), ou rode `chmod +x MonitorMtzMac.command`.

   Com o app aberto, o `main.py` ajusta o **ícone do Dock (MTZ verde)** e o **nome ("Monitor MTZ")** no macOS.

   **Ícones** (em `icons/`, fundo preto com "MTZ"):
   - macOS: `MonitorMtzMac.icns` (verde) — aplicado ao Dock/menu em tempo de execução pelo `main.py`.
   - Windows: `MonitorMtzWin.ico` (azul). Como um `.bat` não carrega ícone sozinho, crie um **atalho**
     do `MonitorMtzWin.bat`, abra **Propriedades → Alterar ícone** e selecione o `MonitorMtzWin.ico`.

Execução manual (com o venv ativado):

```powershell
# Windows
.\venv\Scripts\python.exe main.py
```

```bash
# macOS / Linux
./venv/bin/python main.py
```

A interface abre via PyWebView; se não houver backend de GUI, cai para o navegador padrão.

---

## 🌐 Modo compartilhado (vários usuários)

Por padrão o app é **single-user** e guarda tudo num SQLite local
(`%LOCALAPPDATA%\ProfileManager\profiles.db`).

Para **compartilhar contas, emails, configurações e o pool de proxies** entre
vários usuários, aponte o app para um **PostgreSQL hospedado**. Há duas formas:

**A) Arquivo `database_url.txt`** (mais simples — o inicializador carrega sozinho):
crie um arquivo `database_url.txt` ao lado do inicializador com a connection string
na primeira linha:

```
postgresql://usuario:senha@host:5432/banco?sslmode=require
```

**B) Variável de ambiente** `DATABASE_URL`:

```powershell
$env:DATABASE_URL = "postgresql://usuario:senha@host:5432/banco?sslmode=require"
.\venv\Scripts\python.exe main.py
```

- Sem `DATABASE_URL` / `database_url.txt` → **SQLite local** (comportamento antigo).
- Com a URL definida → **PostgreSQL compartilhado**.

### Migrar os dados existentes (uma única vez)

Leva perfis, contas, emails, configurações e o pool do SQLite local para o PostgreSQL:

```powershell
$env:DATABASE_URL = "postgresql://usuario:senha@host:5432/banco?sslmode=require"
.\venv\Scripts\python.exe migrate_to_postgres.py
```

É idempotente (linhas já existentes são ignoradas) e preserva os timestamps.

### O que é compartilhado x local

- **Compartilhado (no banco):** o **registro** de perfis, contas, emails, limites/config (incl. IMAP), pool de proxies e métricas/histórico.
- **Local em cada máquina:** o `user_data` de cada perfil — os **cookies/sessão logada** do navegador vivem fisicamente na máquina que abriu a conta (não vão pro banco).

Por isso, perfis e contas criados por outro usuário **aparecem para todos** (com as credenciais e o status), mas **abrir/aquecer com a sessão logada** só funciona na máquina que tem o `user_data`. O **lock de perfil** impede que duas máquinas abram o mesmo perfil simultaneamente (a UI mostra "em uso por X" e desabilita o botão).

> ⚠️ **Segurança:** senhas de contas, senha do IMAP e senhas dos proxies são
> gravadas em **texto puro**. Quem tiver a `DATABASE_URL` enxerga todos esses
> segredos. Use sempre `sslmode=require` e compartilhe a URL apenas com usuários
> de confiança.

---

## 🧩 Como funciona

### Proxies (pool compartilhado)
Os proxies ficam num pool no banco. Na primeira execução, o app importa
automaticamente os arquivos da pasta local `proxys/` (formatos aceitos:
`host:porta`, `host:porta:usuario:senha`, `scheme://usuario:senha@host:porta`).
Na criação e na edição de perfis você escolhe um proxy do pool já vendo o status
de cada um.

### Proxy Inteligente x Proxy Fixo (aba Criar Contas)
- **Proxy Inteligente:** o app decide quanto usar cada proxy com base no
  histórico real de banimentos (aba Métricas) — usa mais os proxies que seguram
  contas sem ban, evita os que banam muito, e dá folga inicial aos novos.
- **Proxy Fixo:** o mesmo limite de contas para todos os proxies (previsível).

### Criação e cadastro de contas
**Fluxo recomendado (mais seguro):**
1. **Crie a conta num Android real** (app oficial do IG), com IP móvel (4G/5G) e ritmo humano.
2. No app, **Contas → Adicionar conta**: cadastre usuário/senha/email/WhatsApp. (Se informar um perfil de "conexão local", dá pra abrir/monitorar no PC depois.)
3. Acompanhe o **status** pela aba Contas/Perfis (verificação automática ao fechar o perfil).

**Criação assistida no PC (modo manual, p/ testes):** na aba **Criar Contas**, "✍️ Criar conta manual" gera os dados, aquece o navegador e abre a janela no cadastro — **você preenche** copiando do popup; o código do email aparece sozinho (catch-all/IMAP).

**Criação automática em lote:** o app preenche com comportamento humano, lê o código por email e, se pedir telefone, usa o SMS-Activate (se houver chave). 

> ⚠️ **Sobre detecção:** criar contas **automaticamente pelo PC** tende a nascer sinalizado (o IG correlaciona dispositivo + automação), mesmo com proxy/fingerprint bons. Para escala e segurança, prefira **criar no dispositivo real** e usar o PC como **central de guarda/monitoramento**.

---

## 📁 Estrutura principal

```
AntiDetectMTZ/
├── main.py                 # entrypoint (sobe a API + janela)
├── api.py                  # backend FastAPI (endpoints REST)
├── db.py                   # camada de dados (PostgreSQL ou SQLite)
├── session_manager.py      # sessões Camoufox + lock de perfil em uso
├── profiles.py             # criação/listagem/remoção de perfis
├── fingerprint.py          # geração de fingerprint (browserforge)
├── instagram.py            # fluxo de criação/verificação/aquecimento IG
├── accounts.py             # monitoração, limites e métricas de ban
├── emails.py               # catch-all + leitura de código via IMAP
├── proxies.py              # pool de proxies (compartilhado)
├── sms_activate.py         # cliente sms-activate.org (WhatsApp)
├── batch_import.py         # importação em massa via CSV
├── human.py                # comportamento humano (mouse/teclado)
├── profile_warmup.py       # aquecimento do perfil (navegação geral)
├── migrate_to_postgres.py  # migração SQLite -> PostgreSQL (one-shot)
├── static/index.html       # interface web completa
├── MonitorMtzWin.bat       # instalador + launcher (Windows)
├── MonitorMtzMac.command   # instalador + launcher (macOS / Linux)
├── MonitorMtzWin.ico       # ícone Windows (azul, MTZ)
├── MonitorMtzMac.icns      # ícone macOS (verde, MTZ)
├── icons/                  # fontes dos ícones (SVG/PNG)
└── requirements.txt
```

**Não versionado (no `.gitignore`):** `venv/`, `__pycache__/`, `debug.log`,
`*.db`, `user_data/`, `logs/`, **`proxys/`** e **`database_url.txt`** (contêm
credenciais — nunca commitar).

---

## 📚 Documentação de desenvolvimento

Veja `DEVELOPMENT_CONTEXT.md` para o histórico do projeto, decisões e tarefas em aberto.

---

## ⚠️ Aviso importante

Esta ferramenta é destinada **apenas para fins educacionais, de pesquisa e desenvolvimento**.
O uso para criação em massa de contas pode violar os Termos de Serviço do Instagram e de outros serviços. Use por sua própria conta e risco.

## 📄 Licença

Sem licença definida (uso privado).

---

Desenvolvido com assistência do Grok (xAI) e do Claude (Anthropic).
