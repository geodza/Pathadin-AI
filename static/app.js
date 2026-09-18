'use strict';
const $ = id => document.getElementById(id);
const fmtMpp = v => v == null ? 'missing' : String(Number(Number(v).toPrecision(4)));
const token = document.querySelector('meta[name="zsendo-token"]').content;
let accountIds=[];const activities=new Map();
let config, slide, job, viewer, pollTimer, currentGeo, geoKey, noticeTimer;
let previewPlanHash=null,previewBatch=0,previewSource='proposed',previewOverlays=[];
let tissueMask=null, maskCanvas=null, maskDirty=false, maskMode="pan", maskUndo=null, maskLoading=false;
function notice(text) { $('message').textContent=text; $('message').hidden=false; clearTimeout(noticeTimer); noticeTimer=setTimeout(()=>$('message').hidden=true,9000); }
async function api(path, data) {
  const r=await fetch(path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','x-zsendo-token':token},body:JSON.stringify(data)});
  if(!r.ok){let error;try{error=await r.json();}catch{error={detail:r.statusText};}throw Error(typeof error.detail==='string'?error.detail:JSON.stringify(error.detail));}
  return r.json();
}
function action(id,fn){$(id).addEventListener('click',async()=>{try{setActivity(id,$(id).textContent+'…');await fn();}catch(e){notice(e.message);}finally{setActivity(id,null);}});}
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function addModel(provider='openai',model='') {
  const box=el('div',undefined,'model-entry');
  const row=el('div',undefined,'row');const check=document.createElement('input');check.type='checkbox';check.checked=true;check.className='enabled';check.setAttribute('aria-label','Include model');
  const select=document.createElement('select');select.setAttribute('aria-label','Model provider');
  for(const p of ['openai','anthropic','gemini','demo']){const opt=el('option',p==='demo'?'Synthetic demo':p[0].toUpperCase()+p.slice(1));opt.value=p;select.append(opt);}select.value=provider;
  const remove=el('button','×','remove-model');remove.title='Remove model';remove.onclick=()=>box.remove();row.append(check,select,remove);
  const input=document.createElement('input');input.className='model-id';if(provider==='openai')input.setAttribute('list','accountmodels');input.value=model;input.placeholder='Exact model ID';input.setAttribute('aria-label','Exact model ID');
  const status=el('div',undefined,'key-status');const update=()=>{status.textContent=select.value==='demo'?'Simulated output · no API calls':config.providers[select.value]?.configured?'● API key configured':'○ API key missing · set in .env';};select.onchange=()=>{if(select.value==='openai')input.setAttribute('list','accountmodels');else input.removeAttribute('list');update();};update();box.append(row,input,status);$('models').append(box);attachModelPicker(box);
}
function selectedModels(){return [...document.querySelectorAll('.model-entry')].filter(b=>b.querySelector('.enabled').checked).map(b=>({provider:b.querySelector('select').value,model:b.querySelector('.model-id').value.trim()}));}
async function refreshConfig(){config=await api('/api/config');$('systemstatus').textContent=config.openslide_error?'OpenSlide unavailable: '+config.openslide_error:'OpenSlide is ready. API keys are read from .env or environment.';const previous=$('history').value;$('history').replaceChildren(new Option('Select a saved run…',''));for(const r of config.runs)$('history').add(new Option(`${r.organ} · ${r.status} · ${r.created_at.slice(0,16)}`,r.id));$('history').value=previous;}
function activateSlide(info){
  previewPlanHash=null;previewBatch=0;$('previewimages').replaceChildren();previewOverlays=[];
  slide=info;$('slidename').textContent=info.name;$('slidebadge').textContent=info.demo?'SYNTHETIC DEMO':'WSI OPEN';
  if(info.pixel_only){$('fieldmode').value='relative';setFieldSizes();}
  $('magnification').value='native';
  $('magnification').querySelector('[value="native"]').textContent=info.mpp.every(Boolean)?`Native · ${info.mpp.map(fmtMpp).join(' × ')} µm/px`:info.pixel_only?'Native pixels · uncalibrated':'Native · calibration missing';
  for(const option of $('magnification').options)option.disabled=Boolean(info.pixel_only&&option.value!=='native');
  $('geometry').textContent=`${info.dimensions.map(n=>n.toLocaleString()).join(' × ')} px · MPP ${info.mpp.map(fmtMpp).join(' × ')} (${info.mpp_source})${info.objective?' · objective '+info.objective+'×':''}`;
  tissueMask=null;maskCanvas=null;maskDirty=false;maskUndo=null;maskMode='pan';
  $('usemask').checked=!job||Boolean(job.settings?.mask_revision);
  updateMaskControls();
  if(viewer)viewer.destroy();$('viewer').replaceChildren();
  if(!window.OpenSeadragon){notice('Viewer library missing. Run setup.py to restore local assets.');return;}
  viewer=OpenSeadragon({id:'viewer',tileSources:`/api/slides/${info.id}/slide.dzi`,showNavigationControl:false,showNavigator:true,navigatorPosition:'BOTTOM_RIGHT',animationTime:.4,blendTime:.1,maxZoomPixelRatio:3,visibilityRatio:.6,constrainDuringPan:true,gestureSettingsMouse:{scrollToZoom:true,clickToZoom:false},gestureSettingsTouch:{scrollToZoom:true},gestureSettingsPen:{scrollToZoom:true},gestureSettingsUnknown:{scrollToZoom:true}});
  viewer.addHandler('open',drawOverlays);installMaskBrush();if(typeof installRegionTools==='function')installRegionTools();if(typeof loadRegions==='function')loadRegions().catch(e=>notice(e.message));
  loadTissueMask(info.id,job?.settings?.mask_revision).catch(e=>notice(e.message));
  viewer.addHandler('open-failed',e=>notice('Viewer could not open slide: '+e.message));
}
function rectForFeature(f){const p=f.geometry.coordinates[0];const xs=p.map(v=>v[0]),ys=p.map(v=>v[1]);return [Math.min(...xs),Math.min(...ys),Math.max(...xs)-Math.min(...xs),Math.max(...ys)-Math.min(...ys)];}
function fly(rects){if(!viewer?.world.getItemCount()||!rects.length)return;const x=Math.min(...rects.map(r=>r[0])),y=Math.min(...rects.map(r=>r[1]));const x1=Math.max(...rects.map(r=>r[0]+r[2])),y1=Math.max(...rects.map(r=>r[1]+r[3]));viewer.viewport.fitBounds(viewer.world.getItemAt(0).imageToViewportRectangle(x,y,x1-x,y1-y));}
function drawOverlays(){
  if(!viewer?.world.getItemCount())return;viewer.clearOverlays();const item=viewer.world.getItemAt(0);
  const put=(rect,cls,title)=>{const node=el('div',undefined,cls);node.title=title||'';viewer.addOverlay({element:node,location:item.imageToViewportRectangle(...rect),checkResize:false});};
  // Canvas draws large grids without creating thousands of DOM overlay nodes.
  if(job?.manifest&&($('showgrid').checked||$('showrejected').checked)){
    const canvas=document.createElement('canvas');const [w,h]=job.manifest.slide.dimensions;canvas.width=2048;canvas.height=Math.max(1,Math.round(2048*h/w));const ctx=canvas.getContext('2d');ctx.scale(2048/w,canvas.height/h);ctx.lineWidth=w/2048;
    if($('showgrid').checked){ctx.strokeStyle='#327958aa';for(const t of job.manifest.tiles)ctx.strokeRect(...t.rect);}
    if($('showrejected').checked){ctx.fillStyle='#88888828';for(const r of job.manifest.rejected_rects)ctx.fillRect(...r);}
    canvas.style.pointerEvents='none';viewer.addOverlay({element:canvas,location:item.imageToViewportRectangle(0,0,w,h),checkResize:false});
  }
  if(maskCanvas&&$('showmask').checked){maskCanvas.style.pointerEvents='none';maskCanvas.style.opacity='0.35';viewer.addOverlay({element:maskCanvas,location:item.imageToViewportRectangle(0,0,...slide.dimensions),checkResize:false});}
  if(currentGeo){const features=currentGeo.features.filter(f=>$('overlaystage').value==='all'||f.properties.source==='final');
    // Deduplicate same-tile/source geometry for a readable overlay.
    const seen=new Set();for(const f of features){const key=f.properties.tile_id+f.properties.role;if(seen.has(key))continue;seen.add(key);put(rectForFeature(f),'region-overlay '+f.properties.role,f.properties.morphology);}}
  if(typeof drawRegions==='function')drawRegions();
}
async function loadGeo(){
  const index=$('overlaymodel').value;currentGeo=null;
  if(job&&index!==''){currentGeo=await api(`/api/runs/${job.id}/geojson/${index}`);}drawOverlays();
}
function renderCards(){
  if(!job.models.length)return;
  $('reads').replaceChildren();const previous=$('overlaymodel').value;$('overlaymodel').replaceChildren(new Option('No model annotations',''));
  job.models.forEach((r,index)=>{
    const card=el('article',undefined,'card');card.append(el('div',r.key,'model-label'));
    const final=r.final;card.append(el('h3',final?final.primary_dx.replaceAll('_',' '):r.status==='failed'?'Analysis failed':r.phase||r.status));
    if(r.provider==='demo')card.append(el('p','SIMULATED · no LLM called','warning'));
    if(final){
      card.append(el('div',`Self-reported confidence ${Math.round(final.confidence*100)}% · ${r.latency_s||0}s`,'metrics'));
      card.append(el('p',final.explanation));
      if(final.grade)card.append(el('p','Provisional architectural grade: '+final.grade));
      if(r.flags?.length)card.append(el('p','Protocol flags: '+r.flags.join(', '),'warning'));
      for(const region of final.regions){const b=el('button',region.morphology,'region-button');b.append(el('small',region.role.toUpperCase()+' · '+region.tile_ids.join(', ')));b.onclick=async()=>{try{$('overlaymodel').value=String(index);await loadGeo();const ids=new Set(region.tile_ids);fly(job.manifest.tiles.filter(t=>ids.has(t.id)).map(t=>t.rect));}catch(e){notice(e.message);}};card.append(b);}
      if(final.differential.length)card.append(el('p','Differential: '+final.differential.join('; ')));
      for(const e of final.count_based_estimates)card.append(el('p',`ESTIMATE — NOT A COUNT: ${e.label}: ${e.estimate}. ${e.note}`,'warning'));
      for(const f of final.requested_followups)card.append(el('p',`Requested ${f.type}: ${f.ask}\nWould resolve: ${f.would_resolve}`));
      if(final.limitations.length){const list=el('ul');for(const limit of final.limitations)list.append(el('li',limit));card.append(list);}
      if(r.usage?.length){const details=el('details');details.append(el('summary','Provider-reported usage'));details.append(el('p',JSON.stringify(r.usage,null,2)));card.append(details);}
      if(r.status==='complete'){const link=el('a','Download GeoJSON ↓');link.href=`/api/runs/${job.id}/geojson/${index}`;link.download='';card.append(link);$('overlaymodel').add(new Option(r.key,String(index)));}
    }else card.append(el('p',r.error||`${r.completed_batches||0} of ${job.manifest.batches.length} batches complete. Findings are saved as requests finish.`));
    $('reads').append(card);
  });
  if([...$('overlaymodel').options].some(o=>o.value===previous))$('overlaymodel').value=previous;
}
function renderJob(){
  const active=['queued','preparing','running'].includes(job.status);$('phase').textContent=job.status==='prepared'?'Whole slide prepared':job.status==='complete'?'Analysis complete':job.status[0].toUpperCase()+job.status.slice(1);
  $('analyze').textContent=job.manifest&&job.models.length?'Resume / analyze selected models →':'Analyze with selected models →';$('loadprepared').hidden=true;$('cancel').disabled=!active;$('prepare').disabled=active||!slide;$('analyze').disabled=active||!job.manifest||job.cache_cleared;
  $('exporttiles').href=`/api/runs/${job.id}/tiles.zip`;$('exporttiles').classList.toggle('disabled',!job.manifest||job.cache_cleared);
  $('export').href=`/api/runs/${job.id}/export`;$('export').classList.remove('disabled');
  if(job.manifest){const m=job.manifest;const completed=job.models.reduce((n,r)=>n+(r.completed_calls||0),0);const total=job.estimated_calls_per_model*Math.max(1,job.models.length);
    $('progressbar').style.width=(job.models.length?100*completed/total:100)+'%';$('progresslabel').textContent=job.models.length?`${completed}/${total} reports saved (image batches + synthesis)`:`${m.tiles.length.toLocaleString()} tiles`;
    const resolution=m.context_experiment?'selected-location context views (per-image scale in manifest)':m.field_settings?`architecture ${m.field_settings.output_edge||'native'} px; ${m.field_settings.detail?'sampled native center detail':'no extra detail'}`:m.slide.pixel_only?'native pixels (uncalibrated)':m.target_mpp==='native'?`native resolution (${m.analysis_mpp.map(fmtMpp).join(' × ')} µm/px)`:`${m.target_mpp} µm/px`;
    $('coverageinfo').textContent=`${m.tiles.length.toLocaleString()} retained tiles · ${m.coverage.rejected_tiles.toLocaleString()} excluded tiles · ${m.batches.length.toLocaleString()} batches/model · ${m.coverage.retained_area_mm2==null?'Physical area not summed' :m.coverage.retained_area_mm2.toFixed(2)+' mm² retained grid area' }. All retained tiles scheduled at ${resolution}.`;
    $('fingerprint').textContent='Prepared bundle · SHA-256 '+m.fingerprint;
    if(job.models.length&&job.models.every(r=>r.status==='complete'&&r.bundle_fingerprint===m.fingerprint))$('fingerprint').textContent='✓ Identical submitted image bundle · '+m.fingerprint;
    $('callestimate').textContent=job.estimated_calls_per_model.toLocaleString();
  }else{const p=job.progress;$('progresslabel').textContent=`${p.done.toLocaleString()}/${p.total.toLocaleString()} grid positions`;$('progressbar').style.width=(100*p.done/Math.max(1,p.total))+'%';if(p.tiles_per_second)$('coverageinfo').textContent=`${p.tiles_per_second} grid positions/sec · about ${Math.ceil(p.eta_s/60)} min remaining (estimate). Selected resolution is preserved; every tile touching the reviewed mask is included when mask use is enabled.`;}
  $('agreement').textContent=job.agreement||'ANALYSIS PENDING';renderCards();
  if(job.error)notice(job.error);
  updateMaskControls();
  if(!active)drawOverlays();
  setActivity('run',active?$('phase').textContent+' · '+$('progresslabel').textContent+' · '+(job.models||[]).map(m=>m.phase||'').filter(Boolean).join(' / '):null);
}
async function poll(){if(!job)return;try{job=await api(`/api/runs/${job.id}`);renderJob();if(['queued','preparing','running'].includes(job.status)){pollTimer=setTimeout(poll,1200);}else{await refreshConfig();}}catch(e){notice(e.message);}}
action('browse',async()=>{const button=$('browse');button.disabled=true;try{notice('Select your WSI in the Windows file dialog.');const result=await api('/api/browse',{});if(result.path){$('path').value=result.path;await open(false);$('message').hidden=true;}else{$('message').hidden=true;}}finally{button.disabled=false;}});
// An empty Open WSI field should open the picker, not attempt to decode the current folder.
async function open(demo){$('message').hidden=true;clearTimeout(pollTimer);job=null;currentGeo=null;$('phase').textContent='Ready to review tissue mask';$('progresslabel').textContent='—';$('progressbar').style.width='0%';$('coverageinfo').textContent='Review and save the tissue mask before preparation.';$('fingerprint').textContent='Bundle fingerprint appears after preparation';$('callestimate').textContent='—';$('agreement').textContent='AWAITING ANALYSIS';$('cancel').disabled=true;$('reads').replaceChildren(el('div','Prepare the slide to start a new analysis.','read-empty'));$('analyze').disabled=true;$('export').classList.add('disabled');$('overlaymodel').replaceChildren(new Option('No model annotations',''));const manual=$('mppx').value&&$('mppy').value?[Number($('mppx').value),Number($('mppy').value)]:null;const info=await api('/api/slides',{path:$('path').value,manual_mpp:manual,demo});activateSlide(info);if(demo){if(!$('organ').value)$('organ').value='Uterine corpus';$('models').replaceChildren();addModel('demo','synthetic-fixture-v1');}else if(document.querySelector('.model-entry select')?.value==='demo'){$('models').replaceChildren();addModel();}await refreshConfig();$('loadprepared').hidden=!config.runs.some(r=>r.slide_id===slide?.id&&['prepared','partial','cancelled','interrupted','complete'].includes(r.status));}
action('open',()=>{$('path').value.trim()?open(false).catch(e=>notice(e.message)):$('browse').click();});action('demo',()=>open(true));action('addmodel',()=>addModel());
action('prepare',async()=>{if(!slide)return;if(!$('organ').value.trim()){ $('organ').focus();throw Error('Enter the organ before preparing.'); }if($('usemask').checked&&(!tissueMask?.reviewed||maskDirty))throw Error('Review the tissue mask and click Save reviewed mask first.');if(!previewPlanHash)throw Error('Preview proposed batch first.');const result=await api('/api/prepare',{field_settings:fieldSettings(),preview_plan_hash:previewPlanHash,slide_id:slide.id,organ:$('organ').value,menu:$('menu').value,target_mpp:$('magnification').value==='native'?'native':Number($('magnification').value),batch_size:Number($('batchsize').value),filter_blank:$('blank').checked,mask_revision:$('usemask').checked?tissueMask.revision:null});job={id:result.id};currentGeo=null;await poll();});
action('analyze',async()=>{const cap=Number($('budget').value);const planned=job.estimated_calls_per_model||0;if(!Number.isInteger(cap)||cap<Math.max(1,planned)){$('budgethelp').textContent=`This slide needs ${planned} calls per model. Enter a cap of at least ${Math.max(1,planned)}; allow extra for retries.`;$('budget').scrollIntoView({block:'center'});$('budget').focus();$('budget').select();throw Error('Increase the attempt cap beside the Analyze button. No new preparation is needed.');}const models=selectedModels();if(!models.length||models.some(m=>!m.model))throw Error('Select models and enter each exact model ID.');await api(`/api/runs/${job.id}/start`,{models,max_calls:Number($('budget').value),max_output_tokens:8000,concurrency:$('parallel').checked?Number($('concurrency').value):1,reuse_completed:$('reuse').checked});await poll();});
action('cancel',async()=>{await api(`/api/runs/${job.id}/cancel`,{});notice('Cancellation requested. An in-flight API request may finish and incur cost.');});
action('home',()=>viewer?.viewport.goHome());action('zoomin',()=>viewer?.viewport.zoomBy(1.5));action('zoomout',()=>viewer?.viewport.zoomBy(1/1.5));
for(const id of ['showgrid','showrejected','overlaystage'])$(id).addEventListener('change',drawOverlays);
$('overlaymodel').addEventListener('change',()=>loadGeo().catch(e=>notice(e.message)));
$('history').addEventListener('change',async()=>{if(!$('history').value)return;try{clearTimeout(pollTimer);job=await api(`/api/runs/${$('history').value}`);currentGeo=null;activateSlide(await api(`/api/slides/${job.slide_id}`));$('organ').value=job.organ;$('menu').value=job.menu;$('magnification').value=String(job.settings.target_mpp);$('batchsize').value=job.settings.batch_size;if(job.settings.field_settings){const fs=job.settings.field_settings;$('fieldmode').value=fs.mode;setFieldSizes();$('fieldsize').value=fs.size;$('fieldoutput').value=fs.output_edge;$('fielddetail').checked=fs.detail;$('fieldoverlap').value=fs.overlap||0;updateFieldHint();}$('blank').checked=job.settings.filter_blank;$('budget').value=job.max_calls_per_model||500;$('parallel').checked=(job.concurrency||1)>1;$('concurrency').value=String(job.concurrency>1?job.concurrency:4);$('reuse').checked=true;if(job.models.length){$('models').replaceChildren();for(const m of job.models)addModel(m.provider,m.model);}await poll();}catch(e){notice(e.message);}});
(async()=>{try{await refreshConfig();let count=0;for(const [p,info]of Object.entries(config.providers))for(const model of info.models){addModel(p,model);count++;}if(!count&&config.runs.length){const last=await api(`/api/runs/${config.runs[0].id}`);for(const m of last.models||[])if(m.provider!=='demo'){addModel(m.provider,m.model);count++;}}if(!count){addModel('openai');}}catch(e){notice(e.message);}})();
function updateMaskControls(){
  const active=job&&['queued','preparing','running'].includes(job.status);
  const ready=Boolean(tissueMask&&maskCanvas&&!maskLoading);
  const required=$('usemask').checked;
  $('prepare').disabled=!slide||active||(required&&(!ready||maskDirty||!tissueMask.reviewed));
  $('blank').disabled=required;
  for(const id of ['maskpan','maskinclude','maskexclude','masksave'])$(id).disabled=!ready||Boolean(active);
  $('maskreset').disabled=!slide||maskLoading||Boolean(active);
  $('maskundo').disabled=!ready||!maskUndo||Boolean(active);
  $('maskstatus').textContent=maskLoading?'Loading or saving tissue mask…':!ready?'Open a slide to detect tissue':maskDirty?'Unsaved edits — review and save':`${tissueMask.reviewed?'Reviewed':'Automatic — review needed'} · ${(100*tissueMask.selected_fraction).toFixed(1)}% of overview pixels`;
}
async function loadTissueMask(sid,revision,regenerate=false){
  maskLoading=true;updateMaskControls();
  try{
    let metadata;
    if(regenerate){metadata=await api(`/api/slides/${sid}/mask/generate`,{});}
    else{
      const response=await fetch(`/api/slides/${sid}/mask`+(revision?`?revision=${revision}`:''));
      if(response.status===404)metadata=await api(`/api/slides/${sid}/mask/generate`,{});
      else{if(!response.ok)throw Error('Could not load tissue mask');metadata=await response.json();}
    }
    const im=new Image();im.src=`/api/slides/${sid}/mask.png?revision=${metadata.revision}`;await im.decode();
    if(slide?.id!==sid)return;
    tissueMask=metadata;maskCanvas=document.createElement('canvas');maskCanvas.id='tissue-mask-overlay';
    [maskCanvas.width,maskCanvas.height]=metadata.mask_dimensions;
    maskCanvas.getContext('2d').drawImage(im,0,0);maskDirty=false;maskUndo=null;
    setMaskMode('pan');drawOverlays();
  }finally{if(slide?.id===sid){maskLoading=false;updateMaskControls();}}
}
function setMaskMode(mode){
  maskMode=mode;
  if(viewer)viewer.setMouseNavEnabled(mode==='pan');
  for(const [id,m]of [['maskpan','pan'],['maskinclude','include'],['maskexclude','exclude']])$(id).classList.toggle('active-tool',m===mode);
  if(viewer)viewer.canvas.style.cursor=mode==='pan'?'':'crosshair';
}
function installMaskBrush(){
  let last=null,painting=false;
  const surface=viewer.canvas;
  function position(e){
    const rect=viewer.container.getBoundingClientRect(),item=viewer.world.getItemAt(0);
    const imagePoint=item.viewportToImageCoordinates(viewer.viewport.pointFromPixel(new OpenSeadragon.Point(e.clientX-rect.left,e.clientY-rect.top)));
    const radiusPoint=item.viewportToImageCoordinates(viewer.viewport.pointFromPixel(new OpenSeadragon.Point(e.clientX-rect.left+Number($('maskradius').value),e.clientY-rect.top)));
    return {x:imagePoint.x*maskCanvas.width/slide.dimensions[0],y:imagePoint.y*maskCanvas.height/slide.dimensions[1],radius:Math.max(1,Math.abs(radiusPoint.x-imagePoint.x)*maskCanvas.width/slide.dimensions[0])};
  }
  function paint(e){
    const point=position(e),ctx=maskCanvas.getContext('2d');
    ctx.save();ctx.globalCompositeOperation=maskMode==='exclude'?'destination-out':'source-over';ctx.fillStyle='#2aab7f';ctx.strokeStyle='#2aab7f';ctx.lineCap='round';ctx.lineJoin='round';ctx.lineWidth=point.radius*2;
    if(last){ctx.beginPath();ctx.moveTo(last.x,last.y);ctx.lineTo(point.x,point.y);ctx.stroke();}
    ctx.beginPath();ctx.arc(point.x,point.y,point.radius,0,Math.PI*2);ctx.fill();ctx.restore();
    last=point;maskDirty=true;updateMaskControls();
  }
  surface.addEventListener('pointerdown',e=>{
    if(maskMode==='pan'||!maskCanvas||e.button!==0||maskLoading||job&&['queued','preparing','running'].includes(job.status))return;
    e.preventDefault();e.stopImmediatePropagation();
    maskUndo=maskCanvas.toDataURL('image/png');painting=true;last=null;surface.setPointerCapture(e.pointerId);paint(e);
  },true);
  surface.addEventListener('pointermove',e=>{if(!painting)return;e.preventDefault();e.stopImmediatePropagation();paint(e);},true);
  function finish(e){if(!painting)return;painting=false;last=null;if(surface.hasPointerCapture(e.pointerId))surface.releasePointerCapture(e.pointerId);e.preventDefault();e.stopImmediatePropagation();}
  surface.addEventListener('pointerup',finish,true);surface.addEventListener('pointercancel',finish,true);
}
action('maskpan',()=>setMaskMode('pan'));
action('maskinclude',()=>{setMaskMode('include');$('showmask').checked=true;drawOverlays();});
action('maskexclude',()=>{setMaskMode('exclude');$('showmask').checked=true;drawOverlays();});
action('masksave',async()=>{
  const sid=slide.id,revision=tissueMask.revision,png_base64=maskCanvas.toDataURL('image/png').split(',')[1];
  maskLoading=true;updateMaskControls();
  try{
    const metadata=await api(`/api/slides/${sid}/mask/review`,{revision,png_base64});
    if(slide?.id!==sid)return;
    await loadTissueMask(sid,metadata.revision);notice('Reviewed mask saved. Preparation will include every tile touching it.');
  }finally{if(slide?.id===sid){maskLoading=false;updateMaskControls();}}
});
action('maskreset',()=>loadTissueMask(slide.id,null,true));
action('maskundo',async()=>{
  const im=new Image();im.src=maskUndo;await im.decode();const ctx=maskCanvas.getContext('2d');ctx.clearRect(0,0,maskCanvas.width,maskCanvas.height);ctx.drawImage(im,0,0);maskUndo=null;maskDirty=true;updateMaskControls();
});
$('usemask').addEventListener('change',updateMaskControls);
$('showmask').addEventListener('change',drawOverlays);


action('fetchmodels',async()=>{const result=await api('/api/models/openai',{});accountIds=result.models;$('accountmodels').replaceChildren(...accountIds.map(id=>new Option(id,id)));document.querySelectorAll('.model-entry').forEach(attachModelPicker);$('modelnote').textContent=`Loaded ${accountIds.length} account models. Use Choose account model below, or type an ID. Availability does not confirm image support.`;});
action('loadprepared',async()=>{const saved=config.runs.find(r=>r.slide_id===slide?.id&&['prepared','partial','cancelled','interrupted','complete'].includes(r.status));if(saved){$('history').value=saved.id;$('history').dispatchEvent(new Event('change'));}});

function fieldSettings(){return {mode:$('fieldmode').value,overlap:Number($('fieldoverlap').value),size:Number($('fieldsize').value),output_edge:Number($('fieldoutput').value),detail:$('fielddetail').checked,batch_size:Number($('batchsize').value)};}
function setFieldSizes(){const mode=$('fieldmode').value;const values=mode==='relative'?[10,25,50,100]:mode==='magnification'?[5,10,20,40]:[250,500,750,1000,1500,2000];$('fieldsize').replaceChildren(...values.map(n=>new Option(n+(mode==='relative'?'%':mode==='magnification'?'× nominal':' µm'),n)));$('fieldsize').value=mode==='relative'?'50':mode==='magnification'?'10':'500';if(mode==='magnification'&&$('fieldoutput').value==='0')$('fieldoutput').value='1024';updateFieldHint();}
function updateFieldHint(){previewPlanHash=null;previewBatch=0;$('previewstatus').textContent='Settings changed — preview proposed batch';const mode=$('fieldmode').value;$('fieldhint').textContent=mode==='relative'?`${$('fieldsize').value}% of each fragment's width and height (not area).`:mode==='magnification'?`Nominal ${$('fieldsize').value}×, based on scanner objective (${slide?.objective||'missing'}×). Lower magnification covers more tissue at the selected output size. Preview shows actual microns and pixels. Native detail remains optional. Broader context does not guarantee diagnostic accuracy.`:`${$('fieldsize').value} µm per side; calibrated X/Y spacing. Preview starts near the middle of the largest tissue fragment.`;}
$('fieldmode').addEventListener('change',setFieldSizes);
for(const id of ['fieldsize','fieldoutput','fielddetail','fieldoverlap','batchsize','usemask'])$(id).addEventListener('change',updateFieldHint);
function showBatchPreview(data,saved){
 previewBatch=data.batch_index;$('previewstatus').textContent=`${saved?'Prepared images — exact saved bytes':'Proposed images'} · batch ${previewBatch+1}/${data.batch_count}`;
 if(!saved){previewPlanHash=data.payload_ok?data.plan_hash:null;$('previewsummary').textContent=`${data.field_count} fields · ${data.batch_count} image batches · ${data.planned_calls} proposed calls/model (preparation may split larger batches). This batch: ${(data.encoded_bytes/1e6).toFixed(1)} MB encoded. ${data.payload_ok?'':'Too large: reduce fields/request or architectural image size.'}`;}
 else $('previewsummary').textContent='These are the verified PNG bytes used by this saved run. Changing proposed settings does not change this run.';
 $('previewimages').replaceChildren();
 for(const item of data.images){const figure=el('figure');const img=new Image();img.src='data:image/png;base64,'+item.png_base64;img.alt=item.id;const f=data.fields.find(f=>f.id===item.id||f.id+'-detail'===item.id);const detail=item.id.endsWith('-detail');let text=item.id;if(f){const rect=detail?f.detail_rect:f.rect;text+=` · ${rect[2]} × ${rect[3]} source px`;if(!detail&&f.physical_size_um)text+=` · ${f.physical_size_um.map(n=>n.toFixed(1)).join(' × ')} µm`;}
 const caption=el('figcaption',text);img.onload=()=>caption.append(document.createTextNode(` → sent ${img.naturalWidth} × ${img.naturalHeight} px`));img.onclick=()=>{const dialog=document.createElement('dialog');const close=el('button','Close');close.onclick=()=>{dialog.close();dialog.remove();};const full=img.cloneNode();full.style='max-width:none;width:auto';dialog.append(close,full);document.body.append(dialog);dialog.showModal();};figure.append(img,caption);$('previewimages').append(figure);}
 for(const overlay of previewOverlays)viewer?.removeOverlay(overlay);previewOverlays=[];
 const source=viewer?.world.getItemAt(0);if(source)for(const f of data.fields){for(const [rect,detail] of [[f.rect,false],...(f.detail_rect?[[f.detail_rect,true]]:[])]){const box=el('div',undefined,detail?'preview-detail':'preview-field');viewer.addOverlay({element:box,location:source.imageToViewportRectangle(...rect)});previewOverlays.push(box);}}
 if(data.fields.length)fly(data.fields.map(f=>f.rect));
 $('previewprev').disabled=previewBatch===0;$('previewnext').disabled=previewBatch>=data.batch_count-1;
}
async function previewFields(index=-1,saved=false){
 if(saved){if(!job?.manifest)throw Error('Load or prepare a run first');previewSource='saved';showBatchPreview(await api(`/api/runs/${job.id}/batch/${index}`),true);return;}
 if(!slide)throw Error('Open an image first');if($('usemask').checked&&(!tissueMask?.reviewed||maskDirty))throw Error('Save the reviewed mask first');previewSource='proposed';$('previewstatus').textContent='Rendering exact model inputs…';
 const data=await api('/api/v2/preview',{slide_id:slide.id,field_settings:fieldSettings(),mask_revision:$('usemask').checked?tissueMask.revision:null,batch_index:index});showBatchPreview(data,false);
}
action('previewfields',()=>previewFields());action('previewsaved',()=>previewFields(0,true));action('previewprev',()=>previewFields(Math.max(0,previewBatch-1),previewSource==='saved'));action('previewnext',()=>previewFields(previewBatch+1,previewSource==='saved'));

action('repairbatches',async()=>{if(!job?.manifest)throw Error('Load the saved run first');if(['queued','preparing','running'].includes(job.status))throw Error('Stop the active run first');notice('Copying prepared images and checking every batch…');const result=await api(`/api/runs/${job.id}/repair-batches`,{});await refreshConfig();$('history').value=result.id;$('history').dispatchEvent(new Event('change'));notice(`New run has ${result.batch_count} safe batches. Keep Reuse completed batches checked; unchanged requests can be reused.`);});

function setActivity(key,message){if(message)activities.set(key,message);else activities.delete(key);$('activity').hidden=!activities.size;$('activity').textContent=[...activities.values()].join(' · ');}
function attachModelPicker(box){let picker=box.querySelector('.account-picker');if(!picker){picker=document.createElement('select');picker.className='account-picker';picker.setAttribute('aria-label','Choose account model');box.querySelector('.model-id').before(picker);picker.onchange=()=>{if(picker.value)box.querySelector('.model-id').value=picker.value;};box.querySelector('select').addEventListener('change',()=>attachModelPicker(box));}picker.hidden=box.querySelector('select').value!=='openai';picker.replaceChildren(new Option(accountIds.length?'Choose account model…':'Refresh model list to choose…',''),...accountIds.map(id=>new Option(id,id)));}
action('cacheinspect',async()=>{const data=await api('/api/cache');$('cachenote').textContent=data.note;$('cachelist').replaceChildren();for(const r of data.runs){const row=el('div',undefined,'cache-row');const button=el('button',`Clear ${(r.bytes/1e6).toFixed(1)} MB`);row.append(el('p',`${r.organ} · ${r.status} · ${r.id.slice(0,8)} · ${r.files} images`),button);button.onclick=async()=>{if(!confirm(`Remove ${r.files} cached images (${(r.bytes/1e6).toFixed(1)} MB) from run ${r.id.slice(0,8)}? Reports stay available. Resuming and tile export will require a new preparation.`))return;button.disabled=true;try{await api('/api/cache/clear',{run_id:r.id,snapshot:r.snapshot});row.remove();if(job?.id===r.id){job.cache_cleared=true;renderJob();}notice('Image cache cleared; reports preserved.');}catch(e){notice(e.message);button.disabled=false;}};$('cachelist').append(row);}if(!data.runs.length)$('cachelist').textContent='No removable image caches.';});
