/* Local draft, technical media validation and frame extraction. No social API calls. */
let _stConteudoRestaurado = null, _stCapaRestaurada = null;
let _stDraftReady = false, _stDraftLoading = false, _stDraftTimer = null;
let _stDraftSaving = false, _stDraftDirty = false, _stDraftFailed = false;
let _stSavedMedia = {arquivo: null, capa: null};
const ST_DRAFT_FIELDS = ['st-conta', 'st-destino', 'st-legenda', 'st-link', 'st-rotulo',
  'st-post', 'st-espalhar', 'st-adaptar', 'st-com-chip', 'st-capa-tempo'];
const ST_MAX_MEDIA = 512 * 1024 * 1024;
const _stVideoMetadata = new WeakMap();

function storyDraftStatus(text) {
  const el = document.getElementById('st-rascunho-status');
  if (el) el.textContent = text;
}
function storyDraftState() {
  const fields = {};
  for (const id of ST_DRAFT_FIELDS) {
    const el = document.getElementById(id);
    fields[id] = el.type === 'checkbox' ? el.checked : el.value;
  }
  return {fields, personalizacoes: _pbPers, redes: _pbMarcadas === null ? null : [..._pbMarcadas],
    rect: _stRect, post: _stPost};
}
function storyAgendarRascunho() {
  if (!_stDraftReady || _stDraftLoading) return;
  _stDraftDirty = true;
  clearTimeout(_stDraftTimer);
  storyDraftStatus('Alterações ainda não salvas…');
  _stDraftTimer = setTimeout(storySalvarRascunho, 600);
}
async function storySalvarRascunho() {
  if (_stDraftSaving || !_stDraftReady || !_stDraftDirty) return;
  _stDraftSaving = true;
  _stDraftDirty = false;
  const media = {arquivo: storyConteudo(), capa: storyCapa()};
  try {
    const changed = Object.keys(media).filter(k => media[k] !== _stSavedMedia[k]);
    const fd = new FormData();
    fd.append('state', JSON.stringify(storyDraftState()));
    fd.append('changed', JSON.stringify(changed));
    for (const key of changed) {
      if (media[key]) {
        if (media[key].size > ST_MAX_MEDIA) throw Error('limite local de 512 MiB por arquivo');
        fd.append(key, media[key], media[key].name);
      }
    }
    storyDraftStatus('Salvando rascunho no computador…');
    const response = await fetch('/publicacao/rascunho', {method:'POST', body:fd});
    if (!response.ok) throw Error((await response.json().catch(()=>({}))).detail || 'falha ao salvar');
    _stSavedMedia = media;
    _stDraftFailed = false;
    storyDraftStatus('Rascunho salvo no computador às ' + new Date().toLocaleTimeString());
  } catch (e) {
    _stDraftFailed = true;
    storyDraftStatus('Rascunho não salvo: ' + e.message + '. Edite novamente para tentar salvar.');
  } finally {
    _stDraftSaving = false;
    if (_stDraftDirty) storySalvarRascunho();
  }
}
async function storyRestaurarRascunho() {
  if (_stDraftReady || _stDraftLoading) return;
  _stDraftLoading = true;
  document.getElementById('page-story').inert = true;
  try {
    const r = await fetch('/publicacao/rascunho');
    if (!r.ok) throw Error('não foi possível ler o rascunho');
    const {draft} = await r.json();
    if (draft) {
      const media = {};
      for (const key of ['arquivo', 'capa']) {
        const entry = draft.media?.[key];
        if (!entry) { media[key] = null; continue; }
        const response = await fetch('/publicacao/rascunho/arquivo/' + encodeURIComponent(entry.token));
        if (!response.ok) throw Error('não foi possível restaurar ' + entry.name);
        media[key] = new File([await response.blob()], entry.name, {type:entry.type});
      }
      for (const [id, value] of Object.entries(draft.state.fields || {})) {
        if (!ST_DRAFT_FIELDS.includes(id)) continue;
        const el = document.getElementById(id);
        if (id === 'st-conta' && ![...el.options].some(o=>o.value===value))
          throw Error('a persona do rascunho não está disponível');
        if (el.type === 'checkbox') el.checked = !!value; else el.value = value;
      }
      _pbPers = draft.state.personalizacoes || {};
      _pbMarcadas = draft.state.redes === null ? null : new Set(draft.state.redes || []);
      _stRect = draft.state.rect || _stRect;
      _stPost = draft.state.post || null;
      _stConteudoRestaurado = media.arquivo;
      _stCapaRestaurada = media.capa;
      _stSavedMedia = media;
      document.getElementById('st-arquivo').value = '';
      document.getElementById('st-capa').value = '';
      document.getElementById('st-capa-nome').textContent = media.capa?.name || '';
      document.getElementById('st-arquivo-nome').textContent = media.arquivo?.name || '';
      document.getElementById('st-simular').checked = true;
      document.getElementById('st-auto-publicar').checked = false;
      storyMostrarArte(storyEhVideo(media.arquivo) ? media.capa : media.arquivo);
      storyTrocouDestino();
      storyTrocouChip();
      await storyValidarMidia();
    }
    _stDraftReady = true;
    storyDraftStatus(draft ? 'Rascunho restaurado. Simulação ligada; publicação automática desligada.' : 'Rascunho automático pronto. Vídeo e capa serão salvos no computador.');
  } catch (e) {
    storyDraftStatus('Não foi possível restaurar: ' + e.message + '. Reabra a aba Publicar para tentar novamente.');
  } finally { _stDraftLoading = false; document.getElementById('page-story').inert = false; }
}

function storyAbrirVideo(file) {
  return new Promise((resolve, reject) => {
    const video = document.createElement('video');
    const url = URL.createObjectURL(file);
    const dispose = () => { video.pause(); video.removeAttribute('src'); video.load(); URL.revokeObjectURL(url); };
    const timer = setTimeout(() => fail(), 20000);
    const fail = () => { clearTimeout(timer); dispose(); reject(Error('O navegador não conseguiu ler o vídeo. Use um MP4 reproduzível (H.264/AAC) ou outro arquivo.')); };
    video.preload = 'auto'; video.muted = true;
    video.onerror = fail;
    video.onloadeddata = () => {
      clearTimeout(timer); video.onerror = null; video.onloadeddata = null;
      resolve({video, dispose});
    };
    video.src = url;
  });
}
function storyMedirVideo(file) {
  if (!_stVideoMetadata.has(file)) {
    const pending = storyAbrirVideo(file).then(({video, dispose}) => {
      const info = {duration:video.duration, width:video.videoWidth, height:video.videoHeight};
      dispose();
      if (!Number.isFinite(info.duration) || info.duration <= 0 || !info.width || !info.height)
        throw Error('Vídeo sem duração ou resolução válida.');
      return info;
    });
    _stVideoMetadata.set(file, pending);
    pending.catch(()=>_stVideoMetadata.delete(file));
  }
  return _stVideoMetadata.get(file);
}
function storyChecarArquivo(file) {
  if (!file || !file.size) throw Error('Escolha um arquivo não vazio.');
  if (file.size > ST_MAX_MEDIA) throw Error('Limite local: 512 MiB por arquivo. Reduza o tamanho do vídeo.');
  if (!/\.(mp4|mov|m4v|webm|avi|mkv|jpg|jpeg|png|webp|gif)$/i.test(file.name))
    throw Error('Formato não suportado. Use vídeo MP4/MOV/M4V/WebM/AVI/MKV ou imagem JPG/PNG/WebP/GIF.');
}
async function storyValidarMidia() {
  const file = storyConteudo();
  const el = document.getElementById('st-video-validacao');
  if (!file) { if(el) el.textContent = ''; return true; }
  if(el) el.textContent = 'Verificando arquivo…';
  try {
    storyChecarArquivo(file);
    if (!storyEhVideo(file)) { if(el) el.textContent='Imagem selecionada.'; return true; }
    const info = await storyMedirVideo(file);
    if (file !== storyConteudo()) return storyValidarMidia();
    const notes = [];
    if (!/\.mp4$/i.test(file.name)) notes.push('MP4 é recomendado para compartilhar entre redes');
    if (Math.min(info.width, info.height) < 720) notes.push('resolução abaixo de 720 px no menor lado');
    if (Math.abs(info.width / info.height - 9/16) > .03) notes.push('não é 9:16; será enviado na proporção original');
    if(el) el.textContent = `${info.duration.toFixed(1)} s · ${info.width} × ${info.height} px · ${(file.size/1024/1024).toFixed(1)} MiB · ${file.name.split('.').pop().toUpperCase()}. Verificação técnica OK. ${notes.join('; ')}. Os limites de cada rede ainda são conferidos no envio.`;
    document.getElementById('st-capa-tempo').max = Math.max(0, info.duration - .05);
    return true;
  } catch(e) {
    if (file !== storyConteudo()) return storyValidarMidia();
    if(el) el.textContent = 'Não é possível enviar: ' + e.message;
    return false;
  }
}
async function storyExtrairCapa(btn) {
  const file = storyConteudo();
  if (!storyEhVideo(file)) return toast('Escolha um vídeo primeiro', 'error');
  if (!await storyValidarMidia()) return;
  const seconds = Number(document.getElementById('st-capa-tempo').value);
  const info = await storyMedirVideo(file);
  if (!Number.isFinite(seconds) || seconds < 0 || seconds >= info.duration)
    return toast('Escolha um segundo dentro da duração do vídeo', 'error');
  btn.disabled = true;
  let opened;
  try {
    opened = await storyAbrirVideo(file);
    const {video} = opened;
    if (seconds > 0) await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{video.onseeked=null;reject(Error('Não foi possível buscar esse quadro'));},10000);
      video.onseeked=()=>{clearTimeout(timer);video.onseeked=null;resolve();};
      video.currentTime=seconds;
    });
    const canvas=document.createElement('canvas');
    const scale=Math.min(1,1920/Math.max(video.videoWidth,video.videoHeight));
    canvas.width=Math.round(video.videoWidth*scale);canvas.height=Math.round(video.videoHeight*scale);
    canvas.getContext('2d').drawImage(video,0,0,canvas.width,canvas.height);
    const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.9));
    if (!blob) throw Error('Não foi possível gerar a capa');
    if(file!==storyConteudo()) throw Error('O vídeo mudou; extraia a capa novamente');
    _stCapaRestaurada=new File([blob], 'capa-'+seconds.toFixed(1)+'.jpg', {type:'image/jpeg'});
    document.getElementById('st-capa').value='';
    storyCarregarCapa({target:{files:[_stCapaRestaurada]}});
    storyDraftStatus('Capa extraída. Salvando rascunho…');
    storyAgendarRascunho();
  } catch(e) { toast(e.message,'error'); }
  finally { opened?.dispose(); btn.disabled=false; }
}
document.getElementById('page-story')?.addEventListener('input', storyAgendarRascunho);
document.getElementById('page-story')?.addEventListener('change', storyAgendarRascunho);
window.addEventListener('beforeunload', event=>{
  if (_stDraftDirty || _stDraftSaving || _stDraftFailed) { event.preventDefault(); event.returnValue=''; }
});
