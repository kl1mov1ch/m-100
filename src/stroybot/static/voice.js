/* Live PCM captions. Microphone access only follows an explicit user click. */
const M100Voice={recording:false,finishing:false,target:null,stream:null,context:null,source:null,node:null,ws:null,timer:null,started:0,prefix:'',seen:false};
window.M100Voice=M100Voice;
function voiceElements(){return {dialog:$('voice-dialog'),text:$('voice-text'),status:$('voice-status'),toggle:$('voice-toggle'),use:$('voice-use')};}
function openVoice(target){if(busy||M100Voice.recording)return;M100Voice.target=target||null;const e=voiceElements();e.text.value=target?document.getElementById(target)?.value||'':draftText;e.status.textContent='Нажмите «Начать запись». Текст будет появляться здесь.';e.toggle.innerHTML=icon('mic')+' Начать запись';e.use.disabled=false;e.text.readOnly=false;e.dialog.showModal();}
async function releaseAudio(){clearInterval(M100Voice.timer);M100Voice.stream?.getTracks().forEach(t=>t.stop());M100Voice.source?.disconnect();M100Voice.node?.disconnect();try{await M100Voice.context?.close();}catch{}M100Voice.stream=null;M100Voice.context=null;M100Voice.node=null;M100Voice.source=null;}
function voiceError(error){return ({NotAllowedError:'Доступ к микрофону запрещён. Разрешите его для Telegram или браузера в настройках устройства.',NotFoundError:'Микрофон не найден. Подключите его или введите текст.',NotReadableError:'Микрофон занят другим приложением.',SecurityError:'Микрофон недоступен в этом окне. Откройте приложение по HTTPS из Telegram.'})[error?.name]||error?.message||'Не удалось начать запись. Можно ввести текст вручную.';}
async function finishVoiceUI(message){M100Voice.recording=false;M100Voice.finishing=false;await releaseAudio();const e=voiceElements();e.text.readOnly=false;e.toggle.disabled=false;e.toggle.innerHTML=icon('mic')+' Продолжить запись';e.use.disabled=false;e.dialog.classList.remove('is-recording');if(message)e.status.textContent=message;}
async function startVoice(){
  const v=M100Voice,e=voiceElements();if(v.recording||v.finishing)return;
  if(!navigator.mediaDevices?.getUserMedia){e.status.textContent='Этот клиент не даёт доступ к микрофону. Откройте Mini App в актуальном Telegram или введите текст.';return;}
  if(!data?.speech_available){e.status.textContent='Живое распознавание ещё не подключено. Пока введите текст.';return;}
  try{
    e.toggle.disabled=true;e.status.textContent='Подключаем микрофон…';
    const Context=window.AudioContext||window.webkitAudioContext;if(!Context)throw new Error('Браузер не поддерживает запись звука.');
    v.context=new Context();await v.context.resume();
    v.stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true},video:false});
    if(!e.dialog.open){await releaseAudio();return;}
    if(!v.context.audioWorklet)throw new Error('Обновите Telegram или браузер для живого распознавания.');
    await v.context.audioWorklet.addModule('/pcm-worklet.js');
    const ws=new WebSocket(location.origin.replace(/^http/,'ws')+'/api/speech');v.ws=ws;
    await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('Сервер распознавания не ответил. Попробуйте позже.')),12000);ws.onopen=()=>ws.send(JSON.stringify({init_data:tg?.initData||'',preview_token:preview}));ws.onerror=()=>{clearTimeout(timer);reject(new Error('Не удалось подключить распознавание.'));};ws.onmessage=event=>{const result=JSON.parse(event.data);if(result.error){clearTimeout(timer);reject(new Error(result.error));}else if(result.ready){clearTimeout(timer);resolve();}};ws.onclose=()=>{clearTimeout(timer);reject(new Error('Соединение закрыто. Попробуйте ещё раз.'));};});
    v.prefix=e.text.value.trim();v.recording=true;v.finishing=false;v.started=Date.now();v.seen=false;
    e.text.readOnly=true;e.toggle.disabled=false;e.toggle.innerHTML=icon('stop')+' Остановить';e.use.disabled=true;e.dialog.classList.add('is-recording');e.status.textContent='Говорите. 0:00';
    ws.onmessage=event=>{try{const result=JSON.parse(event.data);if(result.error){finishVoiceUI(result.error);ws.close();return;}if(typeof result.text==='string'){e.text.value=[v.prefix,result.text].filter(Boolean).join(' ');v.seen ||= Boolean(result.text);e.text.scrollTop=e.text.scrollHeight;}if(result.final){finishVoiceUI(v.seen?'Готово. Проверьте текст и используйте его.':'Речь не распознана. Попробуйте говорить ближе к микрофону.');ws.close();}}catch{finishVoiceUI('Ответ распознавания не удалось прочитать. Полученный текст сохранён.');ws.close();}};
    ws.onerror=()=>finishVoiceUI('Связь прервана. Полученный текст сохранён — его можно исправить.');
    ws.onclose=()=>{if(v.recording||v.finishing)finishVoiceUI('Соединение завершено. Проверьте полученный текст.');};
    v.source=v.context.createMediaStreamSource(v.stream);v.node=new AudioWorkletNode(v.context,'m100-pcm');
    v.node.port.onmessage=event=>{if(event.data?.flushed){if(ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({eof:true}));return;}if(ws.readyState===WebSocket.OPEN){if(ws.bufferedAmount>256000){finishVoiceUI('Соединение слишком медленное. Текст сохранён.');ws.close();return;}ws.send(event.data);}};
    v.source.connect(v.node);v.node.connect(v.context.destination);
    v.timer=setInterval(()=>{const seconds=Math.floor((Date.now()-v.started)/1000);e.status.textContent=`Говорите. ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;if(seconds>=175)stopVoice();},500);
  }catch(error){v.ws?.close();await finishVoiceUI(voiceError(error));}
}
async function stopVoice(){const v=M100Voice;if(!v.recording)return;v.recording=false;v.finishing=true;clearInterval(v.timer);voiceElements().toggle.disabled=true;voiceElements().status.textContent='Завершаем распознавание…';v.stream?.getTracks().forEach(t=>t.stop());if(v.node)v.node.port.postMessage('flush');else v.ws?.send(JSON.stringify({eof:true}));setTimeout(()=>{if(v.finishing){v.ws?.close();finishVoiceUI('Проверьте текст. Последний фрагмент мог не успеть распознаться.');}},12000);}
document.addEventListener('click',event=>{
  const b=event.target.closest('button');if(!b)return;
  if(b.hasAttribute('data-voice-live'))openVoice(b.dataset.voiceField||null);
  if(b.id==='voice-toggle'){if(M100Voice.recording)stopVoice();else startVoice();}
  if(b.id==='voice-close'){M100Voice.ws?.close();finishVoiceUI();voiceElements().dialog.close();}
  if(b.id==='voice-use'&&!M100Voice.recording&&!M100Voice.finishing){
    const text=voiceElements().text.value,target=M100Voice.target&&document.getElementById(M100Voice.target);
    voiceElements().dialog.close();
    if(target){target.value=text;target.dispatchEvent(new Event('input',{bubbles:true}));}
    else if(data?.type==='table'&&page!=='work-add'){
      openRecord();const columns=data.table.columns;
      const key=data.table.mapping?.name||data.table.suggested_mapping?.name||columns.find(c=>typeof data.table.rows[0]?.values[c.id]!=='number')?.id||columns[0].id;
      const field=$('cell-'+key);if(field){field.value=text;field.dispatchEvent(new Event('input',{bubbles:true}));}
    }else{draftText=text;textRequestId=crypto.randomUUID();textOpen=true;go(data?.type==='table'?'work-add':'add');}
  }
});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&M100Voice.recording)stopVoice();});
window.addEventListener('pagehide',()=>{M100Voice.ws?.close();releaseAudio();});
document.addEventListener('DOMContentLoaded',()=>{$('voice-dialog').addEventListener('cancel',()=>{M100Voice.ws?.close();finishVoiceUI();});});
