# AntiDetect Camoufox

Gerenciador de perfis de navegador **antidetect** baseado em **Camoufox** (Firefox com stealth avançado via Playwright), com backend FastAPI e interface web.

Cria e gerencia perfis com fingerprints realistas, proxies e comportamento humano, e automatiza a criação, verificação e aquecimento de contas (foco em Instagram). Pode rodar **sozinho** (SQLite local) ou **compartilhado entre vários usuários** (PostgreSQL).

---

## ✨ Funcionalidades

- **Perfis** com fingerprint coerente (browserforge + Camoufox), proxy e geoip — fuso/idioma derivados do IP do proxy em runtime.
- **Lançamento persistente** do navegador (cookies/histórico salvos por perfil), com cadeia de fallbacks que evita erros de geração de fingerprint.
- **Criar / editar / remover perfis** pela interface — incluindo trocar o proxy (manual ou escolhido do pool com status visível).
- **Criação de contas Instagram** com:
  - Emails **catch-all + IMAP** (lê o código de verificação automaticamente).
  - **SMS-Activate** (número de WhatsApp) para contas que pedem telefone.
  - Preenchimento com **comportamento humano** (mouse, digitação, pausas).
  - Data de nascimento opcional (gerada aleatória se não informada).
- **Distribuição inteligente de proxies** por ban rate real (modo "Proxy Inteligente") ou limite fixo.
- **Aquecimento** de perfis (navega sites comuns) e de contas (atividade no IG) em segundo plano.
- **Importação em massa via CSV** (sequencial, com intervalo entre contas).
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

1. Entre na pasta `antidetect-camoufox`.
2. Execute o inicializador:
   - **Windows:** `run.bat`
   - **macOS / Linux:** `run.command` (duplo-clique no Finder) ou `./run.command` no terminal

   Na primeira vez ele:
   - cria o ambiente virtual (`venv/`),
   - instala as dependências (`requirements.txt`),
   - instala o Firefox do Playwright e baixa o Camoufox,
   - abre a interface.

   > No macOS, se o Finder bloquear o `run.command` ("desenvolvedor não identificado"),
   > clique com o botão direito → **Abrir**, ou rode `chmod +x run.command` uma vez.

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

**A) Arquivo `database_url.txt`** (mais simples — o `run.bat` carrega sozinho):
crie um arquivo `database_url.txt` ao lado do `run.bat` com a connection string
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

- **Compartilhado (no banco):** contas, emails, limites/config (incl. IMAP) e o pool de proxies.
- **Local em cada máquina:** os perfis e seus `user_data` — a sessão logada do navegador vive fisicamente numa máquina.

Por isso, contas criadas por outro usuário aparecem com as credenciais, mas a sessão só pode ser aberta/aquecida na máquina que a criou. O **lock de perfil** impede que duas máquinas abram o mesmo perfil simultaneamente (a UI mostra "em uso por X" e desabilita o botão).

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

### Criação de contas
1. (Recomendado) Aqueça o navegador do perfil antes (acumula cookies/histórico).
2. Aba **Criar Contas** → automática em lote (usa emails livres com perfil) ou criação manual.
3. O app preenche o cadastro com comportamento humano, lê o código por email
   (catch-all/IMAP) e, se a conta pedir telefone, usa o SMS-Activate (se houver chave).

---

## 📁 Estrutura principal

```
antidetect-camoufox/
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
├── run.bat                 # instalador + launcher
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
