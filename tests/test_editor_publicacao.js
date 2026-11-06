const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('static/index.html', 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) new vm.Script(match[1]);
function source(name, next) {
  const a = html.indexOf('function ' + name + '(');
  const start = html.slice(a-6,a)==='async ' ? a-6 : a;
  return html.slice(start, html.indexOf(next, a));
}
const elements = {};
for (const id of ['st-auto-publicar','st-simular','st-legenda','st-link','st-rotulo','st-espalhar','st-status']) elements[id]={value:'',checked:false};
elements['st-simular'].checked=true;
const calls=[];
class FormDataMock { constructor(){this.data=new Map()} append(k,v){this.data.set(k,v)} }
const ctx=vm.createContext({document:{getElementById:id=>elements[id]},
  _pbRedes:[{id:'instagram',transporte:'instagrapi'},{id:'x',tem_receita:true},{id:'youtube'}],
  _pbPlano:[{rede:'instagram',publica:true}], _pbPers:{},_stRect:{},_stPost:null,
  FormData:FormDataMock,_pbAdaptar:()=>false,_stPerfilAtual:()=>({profile_id:'p'}),
  _storyPlanoEDesenho:async()=>true,_stRedesPedidas:()=>['instagram'],
  storyConteudo:()=>({name:'video.mp4'}),storyCapa:()=>({name:'cover.png'}),storyEhVideo:()=>true,
  storyValidarMidia:async()=>true, storyComChip:()=>false,storyDestino:()=>({destino:'reels'}),confirm:()=>{throw Error('simulação pediu confirmação')},
  runBtnCountdown:()=>()=>{},storyRenderLeva:()=>{},toast:()=>{},
  apiForm:async(method,path,fd)=>{calls.push(fd.data);return {simulado:true}}
});
vm.runInContext(source('storyRedesAutomaticas','let _stVideoArquivo'),ctx);
vm.runInContext(source('storyPublicarLeva','function storyRenderLeva'),ctx);
(async()=>{
  await vm.runInContext('storyPublicarLeva({})',ctx);
  assert.equal(calls[0].get('adaptar'),'false');
  assert.equal(calls[0].get('auto_publicar'),'');
  assert.equal(calls[0].get('arquivo').name,'video.mp4');
  assert.equal(calls[0].get('capa').name,'cover.png');
  elements['st-auto-publicar'].checked=true;
  await vm.runInContext('storyPublicarLeva({})',ctx);
  assert.equal(calls[1].get('auto_publicar'),'instagram,x');
  assert.equal(calls[1].get('simular'),'true');
  console.log('OK: sintaxe e envio de adaptação, autorização, vídeo, capa e simulação');
})().catch(e=>{console.error(e);process.exitCode=1});
const render = vm.createContext({
 document:{getElementById:id=>({
   'st-previas':elements.previews, 'st-faltam':elements.missing,
   'st-img':{src:'blob:cover',style:{display:''}},
 })[id]},
 _stPerfilAtual:()=>({faltam:[]}), _stRedesPedidas:()=>['instagram','x'],
 storyComChip:()=>false, storyTextoDoChip:()=>'', storyRedesAutomaticas:()=>[],
 storyEhVideo:f=>/\.mp4$/.test(f.name), storyVideoURL:()=> 'blob:video',
 _pbPlano:[{rede:'instagram',rede_nome:'Instagram',publica:true,arquivo:'video.mp4',transporte:'instagrapi'},
           {rede:'x',rede_nome:'X',publica:true,arquivo:'video.mp4',transporte:'browser'}],
 _pbFormatos:{},_pbRedes:[],_pbAberta:null,_pbMedicoes:{},_pbOnde:{},
 escapeHtml:s=>String(s || ''),_pbPreviaTexto:()=>'',
 //  passou a reavaliar o botao de publicar no fim
 // (ele diz quantas redes vao receber, e isso muda com os cartoes).
 // Aqui nao se testa o botao, entao basta existir.
 storyAtualizarBotao:()=>{},
});
elements.previews={innerHTML:''};elements.missing={innerHTML:''};
vm.runInContext(source('_storyDesenharCartoes','/** Mede o compositor'),render);
vm.runInContext('_storyDesenharCartoes()',render);
assert.equal((elements.previews.innerHTML.match(/<video controls/g)||[]).length,2);
assert.equal((elements.previews.innerHTML.match(/alt="Capa do Instagram"/g)||[]).length,1);
assert(elements.previews.innerHTML.includes('aguarda autorização'));
assert(!elements.previews.innerHTML.includes('⚡ sozinho'));
console.log('OK: cartões exibem vídeo original, capa apenas no Instagram e autorização pendente');
