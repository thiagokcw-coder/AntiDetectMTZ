# Preenchedor de Cadastro Autorizado (ig-filler)

Extensão para preencher campos de cadastro do Instagram com dados fornecidos pelo usuário.

**Nunca cria contas, nunca submete o formulário, nunca quebra CAPTCHA.**

**Totalmente compatível com:**
- Camoufox (Firefox stealth / antidetect)
- Firefox
- Chrome / Edge

Funciona de duas formas principais:
- Carregada como addon temporário (uso manual no navegador controlado, via popup)
- Injetada via `page.evaluate` (modo usado pelo antidetect-camoufox na criação automática)

**Importante técnico:** o content script roda em **MAIN world** (não em isolated world). Isso é obrigatório porque o Instagram usa React pesadamente com inputs controlados, dropdowns customizados e estado interno que só é acessível no contexto real da página.

---

## Como o Instagram se comporta ao preencher o cadastro (leia com atenção)

Entender como o formulário do IG funciona ajuda a usar a extensão corretamente:

- **Inputs controlados pelo React**: Email, senha e nome completo são inputs gerenciados pelo React. Não basta fazer `input.value = "..."`. A extensão usa `Object.getOwnPropertyDescriptor` no prototype + dispatch de eventos (`input`, `change`) + eventos de mouse/pointer para simular interação real.

- **Data de nascimento (o mais complexo)**: 
  - Às vezes usa `<select>` nativos.
  - Na maioria das vezes usa dropdowns customizados (botão + lista flutuante com role="option" ou similar).
  - A extensão tenta várias estratégias em sequência: input direto → select nativo → clique no trigger + espera de opções + scroll manual nos containers + simulação de setas do teclado + Enter.
  - Por isso existem waits, polling e múltiplos fallbacks.

- **Username (o ponto mais importante atualmente)**:
  - O Instagram **frequentemente sugere e pré-preenche** um nome de usuário automaticamente assim que você preenche email + nome completo.
  - Ele mostra sugestões logo abaixo do campo ou já coloca um valor no input.
  - Validação de disponibilidade é assíncrona (leva alguns segundos e pode mostrar "The username ... is not available" ou sugestões).
  - **Por isso, na criação automática, paramos completamente de forçar username.** Deixamos o IG escolher o que quiser. A parte Python depois captura (lê) o valor que o Instagram realmente colocou no campo e salva no registro da conta.
  - No uso manual (popup), você preenche os outros campos e o IG pode sobrescrever ou sugerir o username. Revise na página antes de avançar.

- **Senha**: Frequentemente tem um botão "Mostrar" / "Ocultar". A extensão tenta localizar esse botão por proximidade + texto (aria-label, title) ou, como último recurso, força `type="text"`.

- **Validações assíncronas**: Depois de digitar (especialmente username), o IG faz checagens no servidor. A extensão dá tempo (waits) e, em alguns casos, faz polling para o estado estabilizar.

- **Nunca submete**: A extensão só preenche. Quem clica em "Avançar", "Criar conta" ou "Próximo" é você (uso manual) ou o script Python (fluxo automático).

---

## Como carregar no Camoufox / Firefox (addon temporário)

## Como carregar no Camoufox / Firefox (addon temporario)

1. No navegador Camoufox/Firefox, va em `about:debugging#/runtime/this-firefox`
2. Clique em **Carregar Add-on Temporario...**
3. Selecione o arquivo `manifest.json` desta pasta.
4. O icone aparecera na barra. Clique nele enquanto estiver em uma pagina de cadastro do Instagram (`/accounts/emailsignup/`).

Alternativa (em about:addons): "Depurar Complementos" -> Carregar temporario.

## Como instalar no Chrome/Edge (unpacked)

1. Abra `chrome://extensions` (ou edge://extensions).
2. Ative **Modo do desenvolvedor**.
3. **Carregar sem compactacao** e selecione esta pasta.

## Como usar (manual - via popup)

1. Abra a página de cadastro do Instagram (`/accounts/emailsignup/`).
2. Clique no ícone da extensão.
3. Preencha os campos (ou carregue um CSV).
4. Clique **Preencher**.
5. **Revise na página** — o Instagram pode ter sugerido/preenchido o username automaticamente e feito outras validações.
6. Avance manualmente (clique Avançar/Criar conta) quando tudo estiver como você quer.

A extensão **nunca** clica em botões de avanço nem submete o formulário.

## Como funciona no fluxo automático (antidetect-camoufox)

- A extensão é injetada via `page.evaluate` (MAIN world), **não** como addon (para reduzir fingerprint).
- Ela recebe apenas: contact (email/celular), password, fullName e data de nascimento.
- **Username é intencionalmente ignorado** pela extensão. O Instagram costuma sugerir um nome automaticamente. O script Python captura depois o valor que o IG realmente colocou no campo e salva no registro da conta.
- Birthday é tratado com estratégias robustas (classificação por conteúdo dos options + fallbacks).

## Campos suportados

- Celular ou email (contact)
- Senha
- Dia, mês e ano de nascimento
- Nome completo

**Username não é mais preenchido** pela extensão (veja explicação acima sobre o comportamento do IG).

## CSV

Cabeçalhos aceitos (mistura de PT/EN, sem username):

```csv
email,senha,dia,mes,ano,nome
teste@exemplo.com,SenhaForte123,8,6,2000,Nome Completo
```

Ou com data completa:

```csv
email,senha,data_nascimento,nome
teste@exemplo.com,SenhaForte123,08/06/2000,Nome Completo
```

Formato alternativo:

```csv
numero_celular_ou_email,senha,dia_nascimento,mes_nascimento,ano_nascimento,nome_completo
ana.luiza.santos01@example.com,Azul!8472,14,Março,1998,Ana Luiza Santos
```

Outros cabeçalhos suportados: celular, telefone, password, day, month, year, full name, birthdate, etc.

## Dicas para Camoufox no antidetect

- Criação automática: a extensão é injetada apenas via JS (sem carregar como addon). O popup normalmente não é usado.
- Sessões controladas / verificação manual: carregue a extensão temporariamente para ter o popup e poder ajustar os dados manualmente enquanto o IG faz verificações de email/código.
- Nunca carregue a extensão como addon durante criações automáticas em massa (é um sinal detectável). Injeção pura via `page.evaluate` é o método preferido.

Use apenas com permissão e em contas próprias/testes.
