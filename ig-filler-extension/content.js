(() => {
  const fillerVersion = "1.9.8";

  if (window.authorizedRegistrationFiller?.version === fillerVersion) {
    return;
  }

  const monthNames = {
    1: ["january", "janeiro", "enero", "janvier", "januar", "gennaio", "ocak", "januari", "jan"],
    2: ["february", "fevereiro", "febrero", "fevrier", "februar", "febbraio", "subat", "februari", "feb", "fev"],
    3: ["march", "marco", "marzo", "mars", "marz", "maerz", "mart", "maret", "mar"],
    4: ["april", "abril", "avril", "aprile", "nisan", "apr"],
    5: ["may", "maio", "mayo", "mai", "maggio", "mayis", "mei"],
    6: ["june", "junho", "junio", "juin", "juni", "giugno", "haziran", "jun"],
    7: ["july", "julho", "julio", "juillet", "juli", "luglio", "temmuz", "jul"],
    8: ["august", "agosto", "aout", "augustus", "agust", "agustus", "ago", "aug"],
    9: ["september", "setembro", "septiembre", "septembre", "settembre", "eylul", "sep", "set"],
    10: ["october", "outubro", "octubre", "octobre", "oktober", "ottobre", "ekim", "oct", "okt", "out"],
    11: ["november", "novembro", "noviembre", "novembre", "kasim", "nov"],
    12: ["december", "dezembro", "diciembre", "decembre", "dezember", "dicembre", "aralik", "desember", "dec", "dez"]
  };

  function normalize(text) {
    return String(text || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .trim();
  }

  // Casa um matcher (ja normalizado) contra um texto. Para matchers curtos e ambiguos
  // (ex: "day", "dia", "ano", "mes", "year") exige limite de palavra, para nao casar dentro
  // de palavras maiores como "Birthday" (que continha "day" e fazia o filler mirar o botao
  // de ajuda do aniversario em vez do dropdown "Day"). Matchers maiores usam substring normal.
  function matcherInText(haystack, matcher) {
    if (/^[a-z]{2,4}$/.test(matcher)) {
      return new RegExp(`(^|[^a-z])${matcher}([^a-z]|$)`).test(haystack);
    }
    return haystack.includes(matcher);
  }

  function isVisible(element) {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

  function wait(milliseconds) {
    return new Promise((resolve) => setTimeout(resolve, milliseconds));
  }

  function elementText(element) {
    const label = element.id ? document.querySelector(`label[for="${CSS.escape(element.id)}"]`) : null;
    const parentLabel = element.closest("label");
    const container = element.closest("div, section, article, fieldset");

    return normalize(
      [
        element.getAttribute("aria-label"),
        element.getAttribute("name"),
        element.getAttribute("id"),
        element.getAttribute("placeholder"),
        label?.textContent,
        parentLabel?.textContent,
        container?.textContent?.slice(0, 180)
      ].join(" ")
    );
  }

  function dispatchInput(element) {
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function activate(element) {
    element.scrollIntoView({ block: "center", inline: "center" });
    element.focus();
    if (window.PointerEvent) {
      element.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, cancelable: true, view: window }));
      element.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, cancelable: true, view: window }));
    }
    element.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
    element.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
    element.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
  }

  function setControlValue(control, value) {
    const prototype = Object.getPrototypeOf(control);
    const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
    descriptor?.set ? descriptor.set.call(control, value) : (control.value = value);
    dispatchInput(control);
  }

  function findInput(matchers, typeHint) {
    const normalizedMatchers = matchers.map(normalize);
    const controls = [...document.querySelectorAll("input, textarea")].filter(isVisible);

    return controls.find((control) => {
      const haystack = elementText(control);
      const type = normalize(control.getAttribute("type") || "text");
      const typeMatches = !typeHint || type === typeHint;
      return typeMatches && normalizedMatchers.some((matcher) => matcherInText(haystack, matcher));
    });
  }

  function visibleControls(selector = "input, textarea, select, button, [role='button'], [role='combobox'], [aria-haspopup], [aria-expanded], [tabindex]") {
    return [...document.querySelectorAll(selector)]
      .filter(isVisible)
      .sort((a, b) => {
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        return ar.top - br.top || ar.left - br.left;
      });
  }

  function signupScope() {
    const password = findPasswordInput() || document.querySelector('input[type="password"]');
    return password?.closest("form") || password?.closest("main, section, article") || document;
  }

  function inputMeta(input) {
    return normalize([
      input.type,
      input.name,
      input.id,
      input.autocomplete,
      input.inputMode,
      input.getAttribute("aria-label"),
      input.getAttribute("placeholder")
    ].join(" "));
  }

  function textLikeInputs(scope = signupScope()) {
    return [...scope.querySelectorAll("input, textarea")]
      .filter(isVisible)
      .filter((input) => {
        const type = normalize(input.getAttribute("type") || "text");
        return !["hidden", "password", "checkbox", "radio", "submit", "button"].includes(type);
      })
      .sort((a, b) => {
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        return ar.top - br.top || ar.left - br.left;
      });
  }

  function looksUsername(input) {
    const meta = inputMeta(input);
    const type = normalize(input.getAttribute("type") || "");
    return type === "search" || /user|usuario|utilisateur|benutzer|nome de usuario|username|handle|login/.test(meta);
  }

  function fillByFormShape(data) {
    const result = { filled: 0, missing: [] };
    const scope = signupScope();
    const texts = textLikeInputs(scope);
    const used = new Set();

    if (data.contact) {
      const contact = texts.find((input) => {
        const meta = inputMeta(input);
        return !looksUsername(input) && /(email|mail|phone|tel|mobile|celular|telefone|fono|telefono|e-mail)/.test(meta);
      }) || texts.find((input) => !looksUsername(input)) || texts[0];
      if (contact) {
        contact.focus();
        setControlValue(contact, data.contact);
        contact.blur();
        used.add(contact);
        result.filled += 1;
      } else {
        result.missing.push("contato");
      }
    }

    if (data.password) {
      const password = findPasswordInput() || scope.querySelector('input[type="password"]') || document.querySelector('input[type="password"]');
      if (password) {
        password.focus();
        setControlValue(password, data.password);
        password.blur();
        used.add(password);
        result.filled += 1;
      } else {
        result.missing.push("senha");
      }
    }

    if (data.fullName) {
      const fullName = texts.find((input) => {
        if (used.has(input) || looksUsername(input)) return false;
        const meta = inputMeta(input);
        return /name|nome|nombre|nom|vorname|nachname|fullname|full name/.test(meta);
      }) || texts.find((input) => !used.has(input) && !looksUsername(input));
      if (fullName) {
        fullName.focus();
        setControlValue(fullName, data.fullName);
        fullName.blur();
        used.add(fullName);
        result.filled += 1;
      } else {
        result.missing.push("nome");
      }
    }

    return result;
  }

  function fillInput(matchers, value, typeHint) {
    if (!value) {
      return false;
    }

    const input = findInput(matchers, typeHint);
    if (!input) {
      return false;
    }

    input.focus();
    setControlValue(input, value);
    input.blur();
    return true;
  }

  function findPasswordInput() {
    return findInput(["senha", "password"], "password") || findInput(["senha", "password"]);
  }

  function isPasswordVisible(input) {
    return normalize(input.getAttribute("type") || "text") !== "password";
  }

  function findPasswordRevealControl(input) {
    const hideWords = ["ocultar", "hide", "esconder"];
    const showWords = ["mostrar", "show", "exibir", "revelar", "visualizar", "ver senha", "senha"];
    const inputRect = input.getBoundingClientRect();
    const containers = [];
    let current = input.parentElement;

    while (current && containers.length < 5) {
      containers.push(current);
      current = current.parentElement;
    }

    for (const container of containers) {
      const controls = [
        ...container.querySelectorAll("button, [role='button'], input[type='checkbox'], [aria-label], [title], [tabindex]")
      ].filter((control) => control !== input && isVisible(control));

      const namedControl = controls.find((control) => {
        const text = normalize(
          [
            control.getAttribute("aria-label"),
            control.getAttribute("title"),
            control.textContent
          ].join(" ")
        );

        return showWords.some((word) => text.includes(word)) && !hideWords.some((word) => text.includes(word));
      });

      if (namedControl) {
        return namedControl;
      }

      const nearbyControl = controls
        .map((control) => ({ control, rect: control.getBoundingClientRect() }))
        .filter(({ rect }) => {
          const verticalOverlap = rect.bottom >= inputRect.top && rect.top <= inputRect.bottom;
          const nearRightSide = rect.left >= inputRect.left && rect.left <= inputRect.right + 80;
          return verticalOverlap && nearRightSide;
        })
        .sort((a, b) => Math.abs(a.rect.left - inputRect.right) - Math.abs(b.rect.left - inputRect.right))[0]?.control;

      if (nearbyControl) {
        return nearbyControl;
      }
    }

    return null;
  }

  async function revealPassword(input) {
    if (isPasswordVisible(input)) {
      return true;
    }

    const revealControl = findPasswordRevealControl(input);

    if (revealControl) {
      activate(revealControl);
      await wait(120);

      if (isPasswordVisible(input)) {
        return true;
      }
    }

    try {
      input.setAttribute("type", "text");
      input.type = "text";
      dispatchInput(input);
    } catch (error) {
      return false;
    }

    return isPasswordVisible(input);
  }

  async function fillPassword(value) {
    if (!value) {
      return false;
    }

    const input = findPasswordInput();
    if (!input) {
      return false;
    }

    await revealPassword(input);
    input.focus();
    setControlValue(input, value);
    input.blur();
    return true;
  }

  function optionMatches(option, candidates) {
    const text = normalize(
      [
        option.getAttribute("aria-label"),
        option.getAttribute("value"),
        option.textContent
      ].join(" ")
    );

    return candidates.some((candidate) => {
      if (/^\d+$/.test(candidate)) {
        return text.split(/\s+/).includes(candidate);
      }

      return text === candidate || text.startsWith(`${candidate} `) || text.endsWith(` ${candidate}`) || text.includes(candidate);
    });
  }

  function buildCandidates(value, aliases = []) {
    const candidates = [String(value), ...aliases];

    if (/^\d+$/.test(String(value))) {
      candidates.push(String(Number(value)));
    }

    return [...new Set(candidates.map(normalize).filter(Boolean))];
  }

  function fillSelect(matchers, value, aliases = [], fallbackIndex) {
    if (!value) {
      return false;
    }

    const normalizedMatchers = matchers.map(normalize);
    const selects = [...document.querySelectorAll("select")];
    const select = selects
      .find((control) => normalizedMatchers.some((matcher) => matcherInText(elementText(control), matcher)));
    const fallbackSelect = Number.isInteger(fallbackIndex) ? selects[fallbackIndex] : null;
    const targetSelect = select || fallbackSelect;

    if (!targetSelect) {
      return false;
    }

    const candidates = buildCandidates(value, aliases);
    const option = [...targetSelect.options].find((item) => optionMatches(item, candidates));

    if (!option) {
      return false;
    }

    setControlValue(targetSelect, option.value);
    return true;
  }

  function dropdownTriggers() {
    return visibleControls("button, [role='button'], [role='combobox'], [aria-haspopup], [aria-expanded], [tabindex], select")
      .filter((element) => {
        if (element.tagName === "SELECT") return true;
        const role = normalize(element.getAttribute("role"));
        const ariaHasPopup = normalize(element.getAttribute("aria-haspopup"));
        const ariaExpanded = element.hasAttribute("aria-expanded");
        const text = normalize(element.textContent || element.getAttribute("aria-label") || "");
        const bad = /(submit|enviar|next|avancar|avançar|continue|continuar|sign|cadastrar|criar|login|entrar|cancel|voltar|back)/;
        if (bad.test(text)) return false;
        return role === "combobox" || role === "button" || ariaHasPopup || ariaExpanded;
      });
  }

  function findCustomTrigger(matchers, fallbackIndex) {
    const normalizedMatchers = matchers.map(normalize);
    const controls = dropdownTriggers();

    const matched = controls.find((element) => {
      const attrs = normalize(
        [
          element.getAttribute("aria-label"),
          element.getAttribute("name"),
          element.getAttribute("id"),
          element.getAttribute("placeholder")
        ].join(" ")
      );
      const visibleText = normalize(element.textContent);

      return normalizedMatchers.some((matcher) => {
        return matcherInText(attrs, matcher) || visibleText === matcher || visibleText.startsWith(`${matcher} `);
      });
    });

    if (matched) return matched;
    return Number.isInteger(fallbackIndex) ? controls[fallbackIndex] || null : null;
  }

  function findVisibleOption(candidates) {
    const selector = [
      "[role='option']",
      "[role='menuitem']",
      "[aria-selected]",
      "option",
      "li",
      "button",
      "[tabindex]"
    ].join(", ");

    return [...document.querySelectorAll(selector)]
      .filter(isVisible)
      .filter((element) => optionMatches(element, candidates))
      .sort((a, b) => normalize(a.textContent).length - normalize(b.textContent).length)[0] || null;
  }

  function findScrollableContainers() {
    return [...document.querySelectorAll("body, [role='listbox'], [role='menu'], ul, div")]
      .filter(isVisible)
      .filter((element) => element.scrollHeight > element.clientHeight + 8);
  }

  async function scrollUntilOptionAppears(candidates) {
    const containers = findScrollableContainers();

    for (const container of containers) {
      const maxScroll = container.scrollHeight - container.clientHeight;
      const step = Math.max(48, Math.floor(container.clientHeight * 0.75));

      for (let scrollTop = 0; scrollTop <= maxScroll; scrollTop += step) {
        container.scrollTop = scrollTop;
        container.dispatchEvent(new Event("scroll", { bubbles: true }));
        await wait(40);

        const option = findVisibleOption(candidates);
        if (option) {
          return option;
        }
      }

      for (let scrollTop = maxScroll; scrollTop >= 0; scrollTop -= step) {
        container.scrollTop = scrollTop;
        container.dispatchEvent(new Event("scroll", { bubbles: true }));
        await wait(40);

        const option = findVisibleOption(candidates);
        if (option) {
          return option;
        }
      }
    }

    return null;
  }

  async function waitForOption(candidates, timeout = 1200) {
    const start = Date.now();

    while (Date.now() - start < timeout) {
      const option = findVisibleOption(candidates);

      if (option) {
        return option;
      }

      await wait(50);
    }

    return null;
  }

  function pressKeysForValue(trigger, value, kind) {
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) {
      return false;
    }

    let steps = numericValue - 1;
    if (kind === "month") steps = numericValue - 1;
    if (kind === "day") steps = numericValue - 1;
    if (kind === "year") steps = Math.max(0, new Date().getFullYear() - numericValue);

    trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true, cancelable: true }));
    for (let index = 0; index < steps && index < 140; index += 1) {
      trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    }
    trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    dispatchInput(trigger);
    return true;
  }

  async function clickCustomDropdown(matchers, value, aliases = [], kind, fallbackIndex) {
    if (!value) {
      return false;
    }

    const trigger = findCustomTrigger(matchers, fallbackIndex);

    if (!trigger) {
      return false;
    }

    const candidates = buildCandidates(value, aliases);
    activate(trigger);
    let option = await waitForOption(candidates);
    option ||= await scrollUntilOptionAppears(candidates);

    if (option) {
      activate(option);
      return true;
    }

    return pressKeysForValue(trigger, value, kind);
  }

  async function fillDatePart(matchers, value, aliases = [], kind, fallbackIndex) {
    if (fillInput(matchers, value) || fillSelect(matchers, value, aliases, fallbackIndex)) {
      return true;
    }

    return clickCustomDropdown(matchers, value, aliases, kind, fallbackIndex);
  }

  function flushFormEvents() {
    for (const control of document.querySelectorAll("input, textarea, select")) {
      dispatchInput(control);
      control.dispatchEvent(new Event("blur", { bubbles: true }));
    }
    if (document.activeElement?.blur) {
      document.activeElement.blur();
    }
  }

  function findSubmitControl() {
    const scope = signupScope();
    const good = /(submit|enviar|sign\s*up|inscrev|cadastre|cadastrar|criar\s*conta|registrar|registrarse|next|avancar|continue|continuar|join|get\s*started)/i;
    const bad = /(entrar|log\s*in|login|already|ja tenho|tenho uma conta|voltar|back|cancel|cookies?|privacy|privacidade|terms|termos|help|ajuda)/i;

    return visibleControls("button, [role='button'], input[type='submit']")
      .map((element) => {
        const text = normalize(element.innerText || element.textContent || element.value || element.getAttribute("aria-label") || "");
        const type = normalize(element.getAttribute("type") || "");
        let score = 0;
        if (scope && scope.contains(element)) score += 50;
        if (element.tagName === "BUTTON") score += 10;
        if (type === "submit") score += 35;
        if (good.test(text)) score += 70;
        if (bad.test(text)) score -= 160;
        if (element.disabled || element.getAttribute("aria-disabled") === "true") score -= 200;
        const rect = element.getBoundingClientRect();
        score += Math.min(16, Math.max(0, rect.top / 55));
        return { element, text, score };
      })
      .filter((item) => item.score > 20)
      .sort((a, b) => b.score - a.score)[0]?.element || null;
  }

  async function submitPrimary() {
    flushFormEvents();
    await wait(120);
    const control = findSubmitControl();
    if (!control) {
      const scope = signupScope();
      const form = scope?.closest?.("form") || scope?.querySelector?.("form");
      if (form?.requestSubmit) {
        form.requestSubmit();
        return { ok: true, method: "requestSubmit" };
      }
      return { ok: false, reason: "submit control not found" };
    }

    activate(control);
    try {
      control.click();
    } catch (error) {
      // activate() already dispatched pointer/mouse/click events.
    }
    await wait(180);
    flushFormEvents();
    return {
      ok: true,
      method: "extension-click",
      text: normalize(control.innerText || control.textContent || control.value || control.getAttribute("aria-label") || "")
    };
  }

  async function fill(data) {
    // =====================================================
    // COMO O INSTAGRAM FUNCIONA NO CADASTRO (explicação para manutenção)
    // =====================================================
    // - O formulário é React. Inputs são "controlled components".
    //   Usamos setControlValue (prototype descriptor + dispatch input/change)
    //   + activate() com pointer/mouse events para simular usuário real.
    //
    // - Data de nascimento: frequentemente NÃO é <select> nativo.
    //   Usa combobox customizado (botão trigger + lista de role=option).
    //   fillDatePart tenta: input → select nativo → clickCustomDropdown
    //   (que faz scroll em containers + setas do teclado como fallback).
    //
    // - Username: NÃO é mais preenchido aqui (por design).
    //   O Instagram costuma sugerir/preencher automaticamente o username
    //   assim que email + nome completo são digitados.
    //   Na criação automática o script Python captura o valor depois.
    //   Por isso este fill() ignora completamente username.
    //
    // - Senha: pode ter botão "Mostrar". Tentamos achar por texto/proximidade
    //   ou forçamos type="text".
    //
    // - Tudo roda em MAIN world para ver o mesmo estado React da página.
    //
    // Quando data.submit=true, a extensão também clica em Avançar/Criar conta.
    // =====================================================

    let filled = 0;
    const missing = [];
    const monthAliases = monthNames[Number(data.month)] || [];

    const shape = fillByFormShape(data);
    filled += shape.filled;

    if (await fillDatePart(["dia", "day"], data.day, [], "day", 0)) filled += 1;
    else if (data.day) missing.push("dia");

    if (await fillDatePart(["mes", "month"], data.month, monthAliases, "month", 1)) filled += 1;
    else if (data.month) missing.push("mes");

    if (await fillDatePart(["ano", "year"], data.year, [], "year", 2)) filled += 1;
    else if (data.year) missing.push("ano");

    for (const item of shape.missing) {
      if (!missing.includes(item)) missing.push(item);
    }

    let submit = null;
    if (data.submit) {
      submit = await submitPrimary();
    }

    return { filled, missing, submit };
  }

  window.authorizedRegistrationFiller = { version: fillerVersion, fill };
})();
