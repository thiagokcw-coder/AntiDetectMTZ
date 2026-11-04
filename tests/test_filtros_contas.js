// Os filtros da aba Contas: a regra do "parada" e a combinacao dos facetas.
//
// Roda com:  node tests\test_filtros_contas.js
//
// Le as funcoes do proprio index.html em vez de copia-las: teste com copia do
// codigo passa a testar a copia, e foi assim que uma funcao duplicada ja
// enganou a validacao antes (13/10).
//
// O que trava aqui, e por que cada um importa:
//   - conta com PROBLEMA nao e "parada". Sao duas coisas diferentes, e junta-las
//     esconderia a que precisa de acao atras da que so esta ociosa.
//   - `parada_dias` NULO conta como parada ("nunca agiu"), e nao como recente.
//     Nulo e o caso de toda conta que nao e Instagram, porque a unica atividade
//     que o app registra e a do InstaFollow.
//   - os facetas se COMBINAM (estado + rede + busca), em vez de um substituir o
//     outro.

const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'index.html'), 'utf8');

// O trecho das funcoes de filtro, do marcador ate o fim do selo.
const ini = html.indexOf("const _PROBLEM_STATUSES");
const fim = html.indexOf('function renderAccounts(');
if (ini < 0 || fim < 0 || fim < ini) {
  throw new Error('nao achei o bloco dos filtros no index.html');
}
const bloco = html.slice(ini, fim);

// Coisas que o bloco menciona mas que este teste nao exercita.
const escapeHtml = s => String(s == null ? '' : s);
const nomeDaRede = id => id;
const renderAccounts = () => {};
let _accountsCache = [];
const document = { getElementById: () => null };

// `eval` em modo sloppy NAO vaza `const`/`let` para o escopo de fora (so
// declaracoes de funcao vazam), entao o proprio bloco devolve o que o teste
// precisa. `_ctFiltro` sai como REFERENCIA: `ctAplicar` le esse mesmo objeto, e
// o teste o muta com Object.assign em vez de trocar por outro.
const M = eval(bloco + ';({ CT_DIAS_PARADA, CT_ESTADOS, _ctFiltro });');
// So os `const`/`let` sao desestruturados. `ctParada` e `ctAplicar` ja chegaram
// aqui sozinhos -- declaracao de FUNCAO vaza do eval sloppy -- e redeclara-las
// com `const` colidiria ("Identifier has already been declared").
const { CT_DIAS_PARADA, CT_ESTADOS, _ctFiltro } = M;

let ok = 0;
function t(cond, msg) {
  if (!cond) { throw new Error('FALHOU: ' + msg); }
  ok++;
}

const conta = (o) => Object.assign(
  { username: 'x', email: '', profile_name: '', platform: 'instagram',
    status: 'active', parada_dias: 0 }, o);

// ── a regra do "parada" ──────────────────────────────────────────────────
t(ctParada(conta({ parada_dias: null })) === true,
  'conta que NUNCA agiu e parada');
t(ctParada(conta({ parada_dias: undefined })) === true,
  'campo ausente tambem conta como nunca agiu');
t(ctParada(conta({ parada_dias: 68.9 })) === true,
  'parada ha 68 dias e parada');
t(ctParada(conta({ parada_dias: CT_DIAS_PARADA })) === true,
  'o limite e inclusivo: exatamente ' + CT_DIAS_PARADA + ' dias ja e parada');
t(ctParada(conta({ parada_dias: CT_DIAS_PARADA - 0.1 })) === false,
  'logo abaixo do limite NAO e parada');
t(ctParada(conta({ parada_dias: 0 })) === false,
  'conta que agiu hoje nao e parada');

// problema nao e parada -- mesmo sem agir ha meses
for (const st of ['banned', 'checkpoint', 'suspended', 'login_required']) {
  t(ctParada(conta({ status: st, parada_dias: 999 })) === false,
    'conta ' + st + ' NAO entra em parada: esta quebrada, nao ociosa');
}
// mas "em uso" pode estar parada: emprestada e sem agir e as duas coisas
t(ctParada(conta({ status: 'in_use', parada_dias: 999 })) === true,
  'conta emprestada e ociosa conta como parada');

// ── os facetas combinam ──────────────────────────────────────────────────
const LISTA = [
  conta({ username: 'ig_ativa',    platform: 'instagram', status: 'active',     parada_dias: 1 }),
  conta({ username: 'ig_parada',   platform: 'instagram', status: 'active',     parada_dias: 90 }),
  conta({ username: 'ig_emprest',  platform: 'instagram', status: 'in_use',     parada_dias: 90 }),
  conta({ username: 'ig_banida',   platform: 'instagram', status: 'banned',     parada_dias: 90 }),
  conta({ username: 'tt_nova',     platform: 'tiktok',    status: 'active',     parada_dias: null }),
  conta({ username: 'x_check',     platform: 'x',         status: 'checkpoint', parada_dias: null }),
];

function comFiltro(f) {
  Object.assign(_ctFiltro, { estado: '', rede: '', busca: '' }, f);
  return ctAplicar(LISTA).map(a => a.username);
}

t(comFiltro({}).length === 6, 'sem filtro, passam todas');
t(JSON.stringify(comFiltro({ estado: 'in_use' })) === '["ig_emprest"]',
  'o filtro "em uso" pega so a emprestada');
t(JSON.stringify(comFiltro({ estado: 'active' }).sort())
  === '["ig_ativa","ig_parada","tt_nova"]',
  'o filtro "ativas" e por STATUS, e nao exclui as paradas');
t(JSON.stringify(comFiltro({ estado: 'parada' }).sort())
  === '["ig_emprest","ig_parada","tt_nova"]',
  'paradas: a ociosa, a emprestada ociosa e a que nunca agiu -- sem as quebradas');
t(JSON.stringify(comFiltro({ estado: 'problema' }).sort())
  === '["ig_banida","x_check"]',
  'com problema: banida e em suspensao');

t(JSON.stringify(comFiltro({ rede: 'tiktok' })) === '["tt_nova"]',
  'o filtro de rede isola o tipo de conta');
t(comFiltro({ rede: 'instagram' }).length === 4, 'e conta certo no Instagram');

// estado + rede ao mesmo tempo
t(JSON.stringify(comFiltro({ estado: 'parada', rede: 'instagram' }).sort())
  === '["ig_emprest","ig_parada"]',
  'estado e rede se SOMAM, um nao anula o outro');
t(comFiltro({ estado: 'in_use', rede: 'tiktok' }).length === 0,
  'combinacao sem resultado devolve lista vazia, sem explodir');

// busca
t(JSON.stringify(comFiltro({ busca: 'emprest' })) === '["ig_emprest"]',
  'a busca casa pelo username');
t(JSON.stringify(comFiltro({ busca: 'EMPREST' })) === '["ig_emprest"]',
  'e ignora maiusculas');
t(comFiltro({ busca: '   ' }).length === 6,
  'busca so com espacos nao filtra nada');
t(JSON.stringify(comFiltro({ busca: 'parada', estado: 'parada' })) === '["ig_parada"]',
  'busca soma com os outros facetas');

// ── o catalogo de estados esta coerente com o que a tela oferece ─────────
t(CT_ESTADOS[0].id === '' && !CT_ESTADOS[0].fn, 'o primeiro chip e "Todas", sem predicado');
t(CT_ESTADOS.every(e => e.rotulo && e.dica), 'todo chip tem rotulo E dica explicando o criterio');
t(CT_ESTADOS.find(e => e.id === 'parada').dica.includes(String(CT_DIAS_PARADA)),
  'a dica do "paradas" diz o numero de dias -- o filtro nao pode ser caixa-preta');

console.log('OK — ' + ok + ' assercoes');
