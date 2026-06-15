// Compatibility shim for Firefox (browser.*) / Camoufox and Chrome (chrome.*)
if (typeof browser !== "undefined") {
  // Prefer browser when present (Firefox/Camoufox). Map to chrome for unified code.
  // Note: in MV3 Firefox, chrome.* is often also available as alias, but this ensures.
  var chrome = browser;
} else if (typeof chrome === "undefined" && typeof browser !== "undefined") {
  var chrome = browser;
}
const form = document.querySelector("#profileForm");
const statusEl = document.querySelector("#status");
const saveButton = document.querySelector("#saveButton");
const csvFile = document.querySelector("#csvFile");
const csvRow = document.querySelector("#csvRow");

// Campos suportados pela extensão.
// IMPORTANTE: username foi removido intencionalmente.
// O Instagram costuma sugerir e pré-preencher o username automaticamente
// após preencher email + nome completo. Na criação automática capturamos
// o valor que o IG realmente usou (lado Python). No uso manual via popup
// o usuário revisa na página depois do "Preencher".
const fields = ["contact", "password", "day", "month", "year", "fullName"];
let csvRows = [];

const headerMap = {
  contact: [
    "contact",
    "email",
    "e-mail",
    "celular",
    "telefone",
    "numero",
    "numero de celular ou email",
    "numero_celular_ou_email"
  ],
  password: ["password", "senha"],
  day: ["day", "dia", "dia nascimento", "dia_nascimento"],
  month: ["month", "mes", "mes nascimento", "mes_nascimento"],
  year: ["year", "ano", "ano nascimento", "ano_nascimento"],
  fullName: ["fullname", "full_name", "full name", "nome", "nome completo", "nome_completo"]
};

const birthdateHeaders = ["birthdate", "birth_date", "birthday", "data", "nascimento", "data de nascimento", "data_nascimento"];

const monthMap = {
  janeiro: "1",
  jan: "1",
  fevereiro: "2",
  fev: "2",
  marco: "3",
  mar: "3",
  abril: "4",
  abr: "4",
  maio: "5",
  mai: "5",
  junho: "6",
  jun: "6",
  julho: "7",
  jul: "7",
  agosto: "8",
  ago: "8",
  setembro: "9",
  set: "9",
  outubro: "10",
  out: "10",
  novembro: "11",
  nov: "11",
  dezembro: "12",
  dez: "12"
};

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.style.color = isError ? "#c2382b" : "";
}

function normalize(text) {
  return String(text || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .trim();
}

function repairMojibake(text) {
  return String(text || "")
    .replace(/ÃƒÂ§/g, "Ã§")
    .replace(/ÃƒÂ£/g, "Ã£")
    .replace(/ÃƒÂ¡/g, "Ã¡")
    .replace(/ÃƒÂ©/g, "Ã©")
    .replace(/ÃƒÂª/g, "Ãª")
    .replace(/ÃƒÂ­/g, "Ã­")
    .replace(/ÃƒÂ³/g, "Ã³")
    .replace(/ÃƒÂ´/g, "Ã´")
    .replace(/ÃƒÂº/g, "Ãº");
}

function normalizeMonthValue(value) {
  const repaired = repairMojibake(value).trim();
  const normalized = normalize(repaired);

  if (/^\d+$/.test(normalized)) {
    return String(Number(normalized));
  }

  return monthMap[normalized] || repaired;
}

function getPayload() {
  return Object.fromEntries(fields.map((field) => [field, document.querySelector(`#${field}`).value.trim()]));
}

function setFormValues(values = {}) {
  for (const field of fields) {
    document.querySelector(`#${field}`).value = values[field] || "";
  }
}

function parseCsv(text) {
  const rows = [];
  let row = [];
  let cell = "";
  let quoted = false;

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    const next = text[index + 1];

    if (char === '"' && quoted && next === '"') {
      cell += '"';
      index += 1;
    } else if (char === '"') {
      quoted = !quoted;
    } else if ((char === "," || char === ";" || char === "\t") && !quoted) {
      row.push(cell.trim());
      cell = "";
    } else if ((char === "\n" || char === "\r") && !quoted) {
      if (char === "\r" && next === "\n") {
        index += 1;
      }
      row.push(cell.trim());
      if (row.some(Boolean)) {
        rows.push(row);
      }
      row = [];
      cell = "";
    } else {
      cell += char;
    }
  }

  row.push(cell.trim());
  if (row.some(Boolean)) {
    rows.push(row);
  }

  return rows;
}

function findHeaderIndex(headers, names) {
  const normalizedNames = names.map(normalize);
  return headers.findIndex((header) => normalizedNames.includes(header));
}

function parseBirthdate(value) {
  const text = String(value || "").trim();
  let match = text.match(/^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$/);

  if (match) {
    return { day: match[1], month: match[2], year: match[3] };
  }

  match = text.match(/^(\d{4})[./-](\d{1,2})[./-](\d{1,2})$/);

  if (match) {
    return { day: match[3], month: match[2], year: match[1] };
  }

  return null;
}

function mapCsvRows(rows) {
  if (rows.length < 2) {
    return [];
  }

  const headers = rows[0].map(normalize);

  return rows.slice(1).map((row) => {
    const mapped = {};

    for (const field of fields) {
      const index = findHeaderIndex(headers, headerMap[field]);
      mapped[field] = index >= 0 ? repairMojibake(row[index] || "") : "";
    }

    const birthdateIndex = findHeaderIndex(headers, birthdateHeaders);
    const birthdate = birthdateIndex >= 0 ? parseBirthdate(row[birthdateIndex]) : null;

    if (birthdate) {
      mapped.day ||= birthdate.day;
      mapped.month ||= birthdate.month;
      mapped.year ||= birthdate.year;
    }

    mapped.month = normalizeMonthValue(mapped.month);

    return mapped;
  }).filter((row) => Object.values(row).some(Boolean));
}

function refreshCsvSelect() {
  csvRow.innerHTML = "";

  if (!csvRows.length) {
    csvRow.disabled = true;
    csvRow.append(new Option("Nenhum CSV carregado", ""));
    return;
  }

  csvRow.disabled = false;
  csvRows.forEach((row, index) => {
    const label = row.fullName || row.contact || `Linha ${index + 2}`;
    csvRow.append(new Option(`${index + 1} - ${label}`, String(index)));
  });
}

async function saveProfile() {
  await chrome.storage.local.set({ profile: getPayload() });
  setStatus("Dados salvos neste navegador.");
}

async function getCurrentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) {
    throw new Error("Nao encontrei a aba ativa.");
  }
  return tab;
}

async function fillCurrentPage(payload) {
  const tab = await getCurrentTab();

  // Strategy for Camoufox/Firefox + Chrome:
  // The manifest does not auto-inject content.js. The popup injects it into MAIN world only
  // when the user clicks Fill, so IG's React state is accessed without starting automatically.

  // Try direct call first (preferred - no re-injection)
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: (data) => {
        const filler = window.authorizedRegistrationFiller;
        if (filler && typeof filler.fill === "function") {
          const r = filler.fill(data);
          // Support both sync and async fill
          if (r && typeof r.then === "function") {
            return r;
          }
          return r || { filled: 0, missing: [] };
        }
        return null; // not ready yet
      },
      args: [payload]
    });
    const direct = results && results[0] && results[0].result;
    if (direct) {
      return direct;
    }
  } catch (e) {
    // ignore, will fallback
  }

  // Fallback: inject filler into MAIN world (exactly like Python page.evaluate does)
  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "MAIN",
    files: ["content.js"]
  });

  const [{ result: fillResult }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "MAIN",
    func: (data) => {
      const filler = window.authorizedRegistrationFiller;
      if (filler && typeof filler.fill === "function") {
        const r = filler.fill(data);
        if (r && typeof r.then === "function") return r;
        return r || { filled: 0, missing: [] };
      }
      return { filled: 0, missing: ["filler nao exposto"] };
    },
    args: [payload]
  });

  return fillResult || { filled: 0, missing: [] };
}

saveButton.addEventListener("click", saveProfile);

csvFile.addEventListener("change", async () => {
  const [file] = csvFile.files;
  if (!file) {
    return;
  }

  try {
    const text = await file.text();
    csvRows = mapCsvRows(parseCsv(text));
    refreshCsvSelect();

    if (!csvRows.length) {
      setStatus("CSV sem linhas validas. Use cabecalhos como email, senha, dia, mes, ano, nome.", true);
      return;
    }

    csvRow.value = "0";
    setFormValues(csvRows[0]);
    setStatus(`CSV carregado: ${csvRows.length} linha(s).`);
  } catch (error) {
    setStatus(error.message || "Nao foi possivel ler o CSV.", true);
  }
});

csvRow.addEventListener("change", () => {
  const selected = csvRows[Number(csvRow.value)];
  if (selected) {
    setFormValues(selected);
    setStatus("Linha do CSV aplicada ao formulario.");
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = getPayload();

  try {
    await saveProfile();
    const result = await fillCurrentPage(payload);
    const missing = result.missing?.length ? ` Faltando: ${result.missing.join(", ")}.` : "";
    setStatus(`Campos preenchidos: ${result.filled}.${missing} Envie manualmente quando estiver tudo certo.`);
  } catch (error) {
    setStatus(error.message || "Nao foi possivel preencher esta pagina.", true);
  }
});

// Load saved profile on popup open (works in Chrome + Firefox/Camoufox via shim)
chrome.storage.local.get("profile").then((res) => {
  if (res && res.profile) setFormValues(res.profile);
}).catch(() => {});

