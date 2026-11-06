"""Browser media tests using a generated local video; no social network requests."""
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parent.parent
html=(ROOT/'static/index.html').read_text(encoding='utf-8')
helpers=''
for name in ['storyEhVideo','storyConteudo','storyCapa']:
    start=html.index('function '+name+'(')
    helpers+=html[start:html.index('\n}',start)+2]+'\n'
with sync_playwright() as p:
    browser=p.chromium.launch(channel='chrome',headless=True)
    page=browser.new_page()
    fields=['st-conta','st-destino','st-legenda','st-link','st-rotulo','st-post','st-espalhar','st-adaptar','st-com-chip','st-capa-tempo','st-auto-publicar','st-simular']
    page.set_content('<div id="page-story">'+''.join('<input id="'+i+'">' for i in fields)+
      '<input type="file" id="st-arquivo"><input type="file" id="st-capa">'+
      ''.join('<div id="'+i+'"></div>' for i in ['st-rascunho-status','st-video-validacao','st-arquivo-nome','st-capa-nome'])+'</div>')
    page.add_script_tag(content='''
      let _pbPers={},_pbMarcadas=null,_stRect={},_stPost=null;
      function toast(message){throw Error(message)}
      function storyCarregarCapa(ev){_stCapaRestaurada=ev.target.files[0]}
      function storyMostrarArte(){} function storyTrocouDestino(){} function storyTrocouChip(){}
    '''+helpers)
    page.add_script_tag(content=(ROOT/'static/publication_editor.js').read_text(encoding='utf-8-sig'))
    result=page.evaluate('''async()=>{
      const canvas=document.createElement('canvas');canvas.width=320;canvas.height=180;
      const ctx=canvas.getContext('2d');
      ctx.fillStyle='red';ctx.fillRect(0,0,320,180);
      const stream=canvas.captureStream(20); const chunks=[];
      const recorder=new MediaRecorder(stream,{mimeType:'video/webm'});
      recorder.ondataavailable=e=>chunks.push(e.data);
      const stopped=new Promise(resolve=>recorder.onstop=resolve);
      recorder.start();
      for(let i=0;i<20;i++){ctx.fillStyle=i<10?'red':'blue';ctx.fillRect(0,0,320,180);await new Promise(r=>setTimeout(r,50));}
      recorder.stop();await stopped;stream.getTracks().forEach(t=>t.stop());
      // MediaRecorder WebM may have infinite duration until seeking; use metadata fixture
      // for this generated container while exercising real browser decoding and extraction.
      _stConteudoRestaurado=new File(chunks,'sample.webm',{type:'video/webm'});
      const opened=await storyAbrirVideo(_stConteudoRestaurado);
      const width=opened.video.videoWidth; opened.dispose();
      _stVideoMetadata.set(_stConteudoRestaurado,Promise.resolve({duration:1,width:320,height:180}));
      if(!await storyValidarMidia())throw Error('validation failed');
      // 0.85, nao 0.6: o video e vermelho ate ~0.5s e azul depois, mas o
      // setTimeout(50) do laco sempre atrasa um pouco, entao a virada cai
      // DEPOIS de 0.5s -- e posicionar video em currentTime nao e exato por
      // quadro. Buscar a 0.1s da fronteira pegava o quadro vermelho de vez em
      // quando (1 falha em 3 execucoes, medido em 06/11). No meio da faixa
      // azul o teste mede o que quer medir, sem depender da precisao do timer.
      document.getElementById('st-capa-tempo').value='0.85';
      await storyExtrairCapa({disabled:false});
      const bitmap=await createImageBitmap(_stCapaRestaurada);
      if(bitmap.width!==320||bitmap.height!==180)throw Error('wrong cover dimensions');
      const check=document.createElement('canvas');check.width=320;check.height=180;
      check.getContext('2d').drawImage(bitmap,0,0);
      const pixel=check.getContext('2d').getImageData(100,100,1,1).data;
      if(pixel[2]<150 || pixel[0]>80)throw Error('cover is not the selected blue frame');
      bitmap.close();
      let rejected=0;
      for(const file of [{name:'empty.mp4',size:0},{name:'big.mp4',size:ST_MAX_MEDIA+1},{name:'bad.exe',size:1}]){
        try{storyChecarArquivo(file)}catch(e){rejected++}
      }
      const requests=[];
      window.fetch=async(url,options)=>{requests.push(options.body);return {ok:true}};
      _stDraftReady=true;_stDraftDirty=true;
      document.getElementById('st-legenda').value='Minha legenda';
      await storySalvarRascunho();
      if(!requests[0].get('arquivo')||!requests[0].get('capa'))throw Error('media missing');
      _stDraftDirty=true;await storySalvarRascunho();
      if(requests[1].get('arquivo'))throw Error('video uploaded again');
      if(JSON.parse(requests[0].get('state')).fields['st-legenda']!=='Minha legenda')throw Error('caption missing');
      const snapshot={state:JSON.parse(requests[0].get('state')),media:{
        arquivo:{token:'video',name:_stConteudoRestaurado.name,type:'video/webm'},
        capa:{token:'cover',name:_stCapaRestaurada.name,type:'image/jpeg'}}};
      const savedVideo=_stConteudoRestaurado,savedCover=_stCapaRestaurada;
      const select=document.createElement('select');select.id='st-conta';
      select.innerHTML='<option value="">Persona</option>';
      document.getElementById('st-conta').replaceWith(select);
      window.fetch=async url=>({ok:true,json:async()=>({draft:snapshot}),
        blob:async()=>url.endsWith('video')?savedVideo:savedCover});
      _stDraftReady=false;_stConteudoRestaurado=null;_stCapaRestaurada=null;
      document.getElementById('st-auto-publicar').checked=true;
      document.getElementById('st-simular').checked=false;
      await storyRestaurarRascunho();
      if(!_stDraftReady||!storyConteudo()||!storyCapa())throw Error('draft not restored');
      if(document.getElementById('st-auto-publicar').checked||!document.getElementById('st-simular').checked)
        throw Error('unsafe draft restore');
      clearTimeout(_stDraftTimer);
      return {decodedWidth:width,coverType:_stCapaRestaurada.type,rejected,uploads:requests.length};
    }''')
    assert result=={'decodedWidth':320,'coverType':'image/jpeg','rejected':3,'uploads':2},result
    print('OK: video decode, selected frame to JPEG, validation and incremental autosave', result)
    browser.close()
