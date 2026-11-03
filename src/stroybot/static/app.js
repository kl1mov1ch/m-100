"use strict";
const tg = window.Telegram?.WebApp;
tg?.ready(); tg?.expand();
const preview = new URLSearchParams(location.hash.slice(1)).get("preview") || "";
if (preview) history.replaceState(null, "", location.pathname);
let data, page = "today", filter = "all", search = "", mode = "add", busy = false, editing = null, lastSuccess = "", textOpen = false, draftText = "", textRequestId = crypto.randomUUID(), selectIndex = null;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = value => value===null||value===undefined?'—':Number(value || 0).toLocaleString("ru-RU", {maximumFractionDigits:6});
let journal=null, reportStart="", reportEnd="", reportLoaded=false;
function reportQuery(){return "?"+new URLSearchParams({start:reportStart,end:reportEnd});}
function reportPage(){
  if(!reportEnd){reportEnd=data.day;const d=new Date(data.day+"T12:00:00Z");d.setUTCDate(d.getUTCDate()-30);reportStart=d.toISOString().slice(0,10);}
  return heading("Отчёты","Записи и исправления по объекту")+
    `<section class="report-controls"><label>С даты<input id="report-start" type="date" value="${esc(reportStart)}"></label><label>По дату<input id="report-end" type="date" value="${esc(reportEnd)}"></label><button class="primary" data-load-report>Сформировать</button></section>`+
    `<p class="note">Здесь изменения, подтверждённые через бота или приложение. Правки напрямую в Google-таблице и неподтверждённые черновики в журнал не входят.</p>`+
    (!reportLoaded?'<div class="empty"><h2>Выберите период</h2><p>Нажмите «Сформировать», чтобы увидеть изменения.</p></div>':
      `<div class="report-export"><button class="secondary" data-download-report>Скачать CSV для Excel</button><button class="secondary" data-send-report>Получить в Telegram</button></div><p class="note">${esc(journal.start)} — ${esc(journal.end)} · Изменений: ${journal.rows.length}. CSV сохраняет русский текст и десятичную запятую.</p>`+
      (journal.rows.length?`<div class="table-scroll"><table class="changes-table"><thead><tr><th>Дата</th><th>Работа</th><th>Было</th><th>Изменение</th><th>Стало</th><th>Статус</th></tr></thead><tbody>${journal.rows.map(r=>`<tr><td>${esc(r.date)}<small>${esc(r.mode)} · ID ${esc(r.author)}</small></td><td>${esc(r.name)}<small>№ ${esc(r.code)} · ${esc(r.unit)}</small></td><td>${num(r.before)}</td><td>${Number(r.delta)>0?"+":""}${num(r.delta)}</td><td>${num(r.after)}</td><td>${esc(r.status)}</td></tr>`).join("")}</tbody></table></div>`:'<div class="empty"><h2>Изменений за этот период нет</h2><p>После подтверждения отчёта записи появятся здесь.</p></div>'));
}
async function loadReport(){if(busy)return;lock(true);try{journal=await api("/api/report"+reportQuery());reportLoaded=true;render();}catch(e){toast(e.message);}finally{lock(false);applyDisabled();}}
async function exportReport(send){if(busy||!reportLoaded)return;lock(true);try{
  const query="?"+new URLSearchParams({start:journal.start,end:journal.end});
  if(send){const r=await api("/api/report/send",{start:journal.start,end:journal.end});toast(r.message);}
  else{const response=await fetch("/api/report.csv"+query,{headers:{"X-Telegram-Init-Data":tg?.initData||"","X-Preview-Token":preview}});if(!response.ok){const r=await response.json();throw new Error(r.error);}const url=URL.createObjectURL(await response.blob());const a=document.createElement("a");a.href=url;a.download=`M100-changes-${journal.start}-${journal.end}.csv`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);toast("Если Telegram не сохраняет файл, нажмите «Получить в Telegram».");}
}catch(e){toast(e.message);}finally{lock(false);applyDisabled();}}
const icon = name => ({mic:'<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/></svg>',stop:'<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>',link:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 15 6-6m-7 3-3 3a4 4 0 0 0 6 6l3-3m2-6 3-3a4 4 0 0 0-6-6l-3 3"/></svg>',download:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/></svg>',calendar:'<svg viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="16" rx="3"/><path d="M8 3v4m8-4v4M4 11h16m-12 4h3"/></svg>',search:'<svg viewBox="0 0 24 24"><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/></svg>',back:'<svg viewBox="0 0 24 24"><path d="m15 6-6 6 6 6"/></svg>'})[name];
function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toast.timer); toast.timer=setTimeout(()=>$("toast").hidden=true, 5500); }
async function api(path, body) {
  if(body&&path==='/api/action')body={...body,context_project:data?.project_id};
  let response;try{response = await fetch(path, {method:body ? "POST":"GET", headers:{"X-Telegram-Init-Data":tg?.initData || "", "X-Preview-Token":preview, ...(body ? {"Content-Type":"application/json"}: {})}, ...(body ? {body:JSON.stringify(body)} : {})});}catch{throw new Error(navigator.onLine?'Не удалось связаться с приложением. Текст в форме сохранён, попробуйте ещё раз.':'Нет подключения к интернету. Вернитесь к действию после восстановления сети.');}
  let result;try{result=await response.json();}catch{throw new Error('Приложение временно недоступно. Повторите действие немного позже.');}if(!response.ok)throw new Error(result.error||'Не удалось завершить действие.');return result;
}
function lock(value) { busy=value; document.body.classList.toggle("is-busy",value); $("busy-status").hidden=!value; document.querySelectorAll("button").forEach(b=>{if(!b.dataset.disabled)b.disabled=value;});if($('select-project'))$('select-project').disabled=value||accessContext.projects.length<2; }
async function refresh() {
  if(busy) return; lock(true);
  try { accessContext=await api('/api/access');renderAccess();const next=await api("/api/state");if(next.project_id!==data?.project_id){tablePageData=null;tableEvents=null;tableQuery='';}data=next; mode=data.draft?.mode || mode; render(); }
  catch(e){$("main").innerHTML=`<div class="error-box"><div class="empty-icon">${icon("calendar")}</div><h2>Не удалось открыть отчёт</h2><p>${esc(e.message)}</p><button class="secondary" data-reload>Попробовать ещё раз</button></div>`;}
  finally{lock(false); applyDisabled();}
}
async function mutate(body, after) {
  if(busy) return; lock(true);
  try {
    const result=await api("/api/action",body); data=result.state; mode=data.draft?.mode || (['confirm','cancel','retry'].includes(body.action)?'add':mode); reportLoaded=false;
    if(after) after(result); else render(); toast(result.message); return true;
  } catch(e){toast(e.message); return false;}
  finally{lock(false);applyDisabled();}
}
function applyDisabled(){document.querySelectorAll("[data-disabled]").forEach(b=>b.disabled=true);}
function dateLabel(){return (data?.day?new Date(data.day+"T12:00:00"):new Date()).toLocaleDateString("ru-RU",{day:"numeric",month:"long"});}
const helpTexts={
  source:['Подключение таблицы','Администратор вставляет ссылку Google или Битрикс24, загружает данные и проверяет лист и заголовки. Импорт создаёт копию: изменения в ней сохраняются в приложении. Для записи прямо в Google используйте «Рабочая смета».'],
  bitrix:['Битрикс24: как включить доступ','В портале откройте «Разработчикам → Другое → Входящий вебхук». Назовите его M-100 и выберите права CRM и Диск. Пользователь вебхука должен иметь право редактировать нужный файл. Вставьте полный адрес в защищённое поле подключения. Доступность REST API зависит от тарифа. Проверка Диска подтверждает доступ к API; запись в исходную книгу пока не включена. Отчёты CRM — отдельные дневные сделки.'],
  users:['Кто может работать с проектом','Сотрудник отправляет /id боту. Администратор вводит его Telegram ID, имя и выбирает объект. Сотрудник видит только назначенный объект. Изменить назначение или выдать права может только администратор.'],
  voice:['Как вводить голосом','Нажмите микрофон, разрешите доступ и начните запись. Говорите по-русски: текст появляется во время речи. Остановите запись, проверьте слова и нажмите «Использовать текст». Сохранение выполняется отдельно. Если микрофон недоступен, введите текст вручную.'],
  save:['Как сохраняются данные','Форма записи сохраняется кнопкой «Сохранить». Объёмы работ сначала попадают в черновик и записываются после подтверждения. В импортированном проекте меняется копия в приложении. В подключённой рабочей смете Google меняются значения исходной таблицы.'],
  headers:['Выбор заголовков','Выберите лист и номер строки с названиями полей. Если заголовков нет, укажите 0. Проверьте предпросмотр перед созданием. Пустые строки пропускаются; названия и порядок колонок могут быть любыми.']
};
function helpButton(key){return `<button type="button" class="help-button" data-help="${key}" aria-label="Справка: ${esc(helpTexts[key]?.[0]||'помощь')}">i</button>`;}
document.addEventListener('click',event=>{const b=event.target.closest('[data-help]');if(!b)return;const content=helpTexts[b.dataset.help];if(!content)return;let d=$('help-dialog');if(!d){d=document.createElement('dialog');d.id='help-dialog';d.innerHTML='<div class="help-content"><div class="sheet-head"><h2 id="help-title"></h2><button type="button" class="icon-button" aria-label="Закрыть справку">×</button></div><p id="help-text"></p></div>';document.body.append(d);d.querySelector('button').onclick=()=>d.close();}d.querySelector('#help-title').textContent=content[0];d.querySelector('#help-text').textContent=content[1];d.showModal();});
function heading(title, subtitle){return `<div class="page-heading"><h1>${title}</h1><span class="date-chip">${esc(dateLabel())}</span></div><p class="intro">${subtitle}</p>`;}
function metadata(item){return `№ ${esc(item.code || "без номера")} · ${esc(item.section)} · ${esc(item.unit)}`;}
function details(item){return `<details><summary>Наименование по смете</summary><p>${esc(item.name)}</p></details>`;}
function today(){
  const items=data.items.filter(i=>Number(i.today)!==0);
  const sectionCount=new Set(items.map(i=>i.section)).size;
  return heading("Сегодня", "Выполненные работы по объекту") + (lastSuccess?`<div class="success">${esc(lastSuccess)}</div>`:"")+
    `<div class="stats"><div class="stat"><strong>${items.length}</strong><span>Позиций за сегодня</span></div><div class="stat"><strong>${sectionCount}</strong><span>Разделов с работами</span></div></div>`+
    `<div class="section-label"><span>ВЫПОЛНЕННЫЕ РАБОТЫ</span>${items.length?'<button class="text-button" data-go="add">Добавить</button>':""}</div>`+
    (items.length?items.map(i=>`<article class="card"><div class="card-top"><div><div class="card-title">${esc(i.label)}</div><div class="card-meta">${metadata(i)}</div></div><div class="volume">${num(i.today)}<small>${esc(i.unit)}</small></div></div><div class="card-actions"><span class="compact-count">Внесено за день</span><button class="text-button" data-correct="${i.id}">Изменить</button></div>${details(i)}</article>`).join(""):
      `<div class="empty"><div class="empty-icon">${icon("calendar")}</div><h2>Пока нет выполненных работ</h2><p>Добавьте позиции из сметы и укажите, сколько выполнили сегодня.</p><button class="primary" data-go="add">Добавить работы</button></div>`)+
    (data.sheet_url?`<a class="saved-link" href="${esc(data.sheet_url)}" target="_blank" rel="noopener noreferrer">Открыть Google-таблицу ↗</a>`:"");
}
function selectedItems(){return M100Search.rank(data.items.filter(i=>filter==="all" || i.section.replace(/\s/g,"").toUpperCase()===filter),search);}
function textPanel(){return `<div class="input-methods"><button class="secondary compact" data-text-open>${textOpen?'Скрыть текст':'Написать текст'}</button><button class="secondary compact" data-voice-live>${icon("mic")} Записать голос</button></div>`+(textOpen?`<section class="text-report"><label for="report-text">Что выполнили?</label><textarea id="report-text" maxlength="6000" placeholder="Медь 6-ка — 15 м, кабель 3×2,5 — 56 м">${esc(draftText)}</textarea><div class="composer-actions"><button class="text-button" data-voice-live data-voice-field="report-text">${icon("mic")} Голосовой ввод</button><button class="primary compact" data-send-text ${!data.ai_available?'disabled data-disabled="true"':''}>Подготовить отчёт</button></div><p class="note">Перед записью вы проверите позиции и объёмы.</p></section>`:'');}
function catalog(){
  const remaining=page==="remaining", items=selectedItems();
  return heading(remaining?"Остатки":"Добавить работы",remaining?"Объёмы по смете и фактическое выполнение":"Выберите позицию и укажите выполненный объём")+
    (!remaining ? textPanel() : "")+
    (selectIndex ? `<div class="service-notice">Выберите работу для строки ${selectIndex} из черновика.</div>` : "")+
    (!remaining?`<div class="mode-switch"><button data-mode="add" class="${mode==="add"?"active":""}">Добавить объём</button><button data-mode="set" class="${mode==="set"?"active":""}">Исправить итог дня</button></div><p class="note">${mode==="set"?"Укажите новый итог за сегодня. Остальные позиции сохранятся.":"Объём прибавится к уже внесённому за сегодня."}</p>`:"")+
    `<div class="search">${icon("search")}<input id="search" aria-label="Поиск работ" placeholder="Название, размер или номер позиции" value="${esc(search)}" autocomplete="off"><button class="search-clear" data-clear-search aria-label="Очистить поиск">×</button></div><p class="input-note">Поиск понимает «медь 6-ка», «кабель 3 на 2,5» и небольшие опечатки.</p><div class="filters"><button data-filter="all" class="filter ${filter==="all"?"active":""}">Все работы</button><button data-filter="ОВ3" class="filter ${filter==="ОВ3"?"active":""}">Кондиционирование</button><button data-filter="ЭМ3" class="filter ${filter==="ЭМ3"?"active":""}">Электромонтаж</button></div><div class="list-caption">Найдено позиций: ${items.length}</div><div id="catalog-list">${catalogRows(items,remaining)}</div>`;
}
function catalogRows(items, remaining){return items.length?items.map(i=>remaining?
  `<article class="card"><div class="card-top"><div><div class="card-title">${esc(i.label)}</div><div class="card-meta">${metadata(i)}</div></div><div class="volume">${num(i.remaining)}<small>${esc(i.unit)}</small></div></div><div class="remaining-figures"><span>Выполнено <strong>${num(i.completed)}</strong></span><span>По смете <strong>${num(i.planned)}</strong></span></div><progress class="progress" max="100" value="${Math.min(100,Math.max(0,Number(i.completed)/Math.max(1,Number(i.planned))*100))}" aria-label="Выполнение по смете"></progress>${details(i)}</article>`:
  `<article class="card catalog-card"><div class="card-top"><div><div class="card-title">${esc(i.label)}</div><div class="card-meta">${metadata(i)}</div><div class="card-meta">Осталось: ${num(i.remaining)} ${esc(i.unit)}</div></div><button class="add-circle" data-add="${i.id}" aria-label="Добавить: ${esc(i.label)}">+</button></div>${details(i)}</article>`).join(""):
  `<div class="empty"><h2>Ничего не найдено</h2><p>Попробуйте часть названия, диаметр или номер позиции.</p><button class="secondary" data-reset>Сбросить поиск</button></div>`;}
function review(){
  const d=data.draft;if(!d)return heading("Проверить отчёт","Черновик пуст")+'<button class="primary wide" data-go="add">Добавить работы</button>';
  const byId=Object.fromEntries(data.items.map(i=>[i.id,i]));
  return `<button class="back" data-go="add">${icon("back")}К списку работ</button>`+heading("Проверить отчёт", "Проверьте позиции и количество перед записью")+
    `<div class="review-summary"><strong>${d.mode==="set"?"Исправление итогов дня":"Добавление выполненных объёмов"}</strong><br>${d.mode==="set"?"Указанные значения заменят объёмы выбранных позиций за сегодня.":"Указанные значения прибавятся к уже внесённым за сегодня."}</div>`+
    d.lines.map((l,n)=>{const i=byId[l.item_id];return `<article class="card ${l.question?"unresolved":""}"><div class="card-top"><div><div class="card-title">${n+1}. ${esc(i?.label || l.source)}</div><div class="card-meta">${i?metadata(i):"Нужно выбрать позицию"}</div></div><div class="volume">${l.quantity===null?"?":num(l.quantity)}<small>${esc(l.unit||"")}</small></div></div>${l.question?`<div class="warning-inline">${esc(l.question)}</div>`:""}${!i?`<div class="pick-buttons">${l.candidates.map(id=>byId[id]?`<button class="secondary" data-pick="${n+1}" data-item="${id}">${esc(byId[id].label)} · № ${esc(byId[id].code)}</button>`:"").join("")}</div>`:""}<div class="card-actions">${!i?`<button class="text-button" data-select="${n+1}">Выбрать работу</button>`:""}<button class="text-button" data-quantity="${n+1}">Изменить объём</button><button class="text-button danger" data-delete="${n+1}">Удалить</button></div>${i&&d.totals?.[i.id]!==undefined?`<p class="note">После записи за день: ${num(d.totals[i.id])} ${esc(i.unit)}</p>`:""}</article>`;}).join("")+
    (d.over_budget||[]).map(w=>`<div class="warning">${esc(w)}</div>`).join("")+
    (d.warning?`<div class="warning">${esc(d.warning)}</div>`:"")+
    (d.status==="pending"?'<div class="pending">Запись началась, но подтверждение получено не от всех систем. Повторите синхронизацию — объёмы повторно начислены не будут.</div>':"")+
    `<div class="review-actions">${d.status==="pending"?'<button class="primary wide" data-retry>Повторить запись</button>':`<button class="primary wide" data-confirm ${!d.ready?'disabled data-disabled="true"':""}>${data.mode==="local"?"Подтвердить в тестовом режиме":"Подтвердить запись"}</button><button class="secondary" data-go="add">Добавить ещё работу</button><button class="text-button danger" data-cancel>Отменить черновик</button>`}</div>`+
    `<p class="note">${data.mode==='table'?"Объёмы сохранятся в проекте в приложении. Исходный файл не изменится.":data.mode==="local"?"Сейчас данные сохраняются в тестовом хранилище приложения. Google Sheets и Битрикс24 не обновляются.":data.mode==="google"?"Объёмы сохранятся в Google Таблицу. Отчёт для Битрикс24 сохранится в очереди до подтверждения CRM.":data.mode==="setup"?"Черновик можно подготовить. Запись недоступна, пока не подключён Битрикс24.":"Запись будет выполнена в Google Sheets и Битрикс24."}</p>`;
}
function render(){
  if(!data){if(page==='admin')$('main').innerHTML=adminPage();return;}$("project").textContent=data.project;$("mode-banner").hidden=['live','table','google'].includes(data.mode);
  $("mode-banner").textContent=data.mode==="setup"?"Google-таблица подключена · запись ждёт подключения Битрикс24":"Тестовый режим · изменения не отправляются в рабочую таблицу";
  $("connection-state").textContent=data.mode==='table'?'Проект в приложении':data.mode==="local"?"Тестовый стенд":"Google Sheets";
  const tabular=data.type==='table';
  document.querySelector('[data-page="today"] span').textContent=tabular?'Данные':'Сегодня';document.querySelector('[data-page="remaining"] span').textContent=tabular?'Объёмы':'Остатки';document.querySelector('[data-page="reports"] span').textContent=tabular?'История':'Отчёты';
  $("main").innerHTML=page==='admin'?adminPage():page==='review'?review():page==='table-settings'?tableSettings():page==='work-reports'?reportPage():tabular&&page==='today'?tableView():tabular&&page==='add'?tableAddPage():tabular&&page==='reports'?tableHistory():tabular&&page==='remaining'&&!data.table.mapping?.name?tableSettings():page==='today'?today():page==='reports'?reportPage():catalog();
  if(tabular&&page==='remaining')$('main').insertAdjacentHTML('afterbegin','<div class="inline-tools"><button class="text-button" data-table-settings>Назначить колонки</button><button class="primary compact" data-go="work-add">Внести объём</button></div>');
  if(tabular){document.querySelectorAll('[data-filter]:not([data-filter="all"])').forEach(b=>b.hidden=true);if(!data.table.mapping?.planned)document.querySelectorAll('progress').forEach(p=>p.hidden=true);}
  document.querySelectorAll("[data-page]").forEach(b=>b.classList.toggle("active",b.dataset.page===(page==="review"?"add":page)));
  if(data.crm&&page!=='admin')$('main').insertAdjacentHTML('afterbegin',`<p class="note crm-status">Битрикс24: ${data.crm.configured?(data.crm.pending?'в очереди '+data.crm.pending+' отчётов':'подключён'):'не подключён — отчёты сохраняются в очереди'}${data.crm.last_error?' · '+esc(data.crm.last_error):''}</p>`);
  const d=data.draft;$("draft-bar").hidden=!d || page==="review" || (!d.lines.length&&d.status!=="pending");
  $("open-draft").textContent=d?.status==="pending"?"Продолжить запись":`Проверить отчёт · ${d?.lines.length||0}`;
  applyDisabled();
}
function go(next){if(window.M100Voice?.recording){toast('Сначала остановите голосовую запись.');return;}if(next!=="add")selectIndex=null;page=next;search="";filter="all";render();window.scrollTo(0,0);if(next==='reports'&&data?.type==='table')loadTableHistory();}
function openQuantity(item,index=null,correction=false){
  if(data.draft?.status==="pending"){go("review");toast("Сначала завершите начатую запись.");return;}
  editing={item,index,correction,request_id:crypto.randomUUID()};
  $("dialog-tag").textContent=index?"ИЗМЕНИТЬ ОБЪЁМ":correction?"ИСПРАВИТЬ ИТОГ ДНЯ":"ДОБАВИТЬ РАБОТУ";
  $("dialog-title").textContent=item.label;$("dialog-meta").textContent=`№ ${item.code || "без номера"} · ${item.section}`;
  $("quantity-unit").textContent=item.unit;
  $("quantity").value=index?(data.draft.lines[index-1].quantity||""):correction?num(item.today).replace(/\s/g,""):"";
  $("dialog-hint").textContent=`По смете осталось ${num(item.remaining)} ${item.unit}. ${correction||mode==="set"?"Введите новый итог за сегодня.":"Введите объём, который нужно добавить."}`;
  $("save-quantity").textContent=index?"Сохранить изменение":"Добавить в отчёт";
  $("dialog").showModal();setTimeout(()=>$("quantity").focus(),80);
}
function draftAction(action,extra={}){const d=data.draft;return {action,draft_id:d.id,revision:d.revision,...extra};}
document.addEventListener("click",async event=>{
  const b=event.target.closest("button");if(!b||busy)return;
  if(b.dataset.page)go(b.dataset.page);
  if(b.hasAttribute("data-load-report"))await loadReport();
  if(b.hasAttribute("data-download-report"))await exportReport(false);
  if(b.hasAttribute("data-send-report"))await exportReport(true);
  if(b.dataset.go)go(b.dataset.go);
  if(b.hasAttribute("data-reload"))refresh();
  if(b.dataset.filter){filter=b.dataset.filter;render();}
  if(b.dataset.mode){if(data.draft&&data.draft.mode!==b.dataset.mode){toast("Сначала завершите или отмените открытый черновик.");return;}mode=b.dataset.mode;render();}
  if(b.dataset.add){if(selectIndex){await mutate(draftAction("pick",{index:selectIndex,item_id:b.dataset.add}),()=>{selectIndex=null;go("review");});}else openQuantity(data.items.find(i=>i.id===b.dataset.add));}
  if(b.hasAttribute("data-text-open")){textOpen=!textOpen;render();}
  if(b.hasAttribute("data-send-text")){if(!draftText.trim()){toast("Опишите выполненные работы.");return;}await mutate({action:"text",text:draftText,request_id:textRequestId,mode},()=>{draftText="";textRequestId=crypto.randomUUID();textOpen=false;go("review");});}
  if(b.hasAttribute("data-voice")){if(tg?.initData){tg.close();}else window.open(data.bot_url,"_blank","noopener");}
  if(b.dataset.select){selectIndex=Number(b.dataset.select);go("add");}
  if(b.hasAttribute("data-clear-search")){search="";render();$("search")?.focus();}
  if(b.dataset.correct){if(data.draft&&data.draft.mode!=="set"){go("review");toast("Сначала завершите открытый отчёт.");return;}mode="set";openQuantity(data.items.find(i=>i.id===b.dataset.correct),null,true);}
  if(b.dataset.quantity){const n=Number(b.dataset.quantity),l=data.draft.lines[n-1],i=data.items.find(i=>i.id===l.item_id);if(!i){toast("Сначала выберите позицию для этой строки.");return;}openQuantity(i,n);}
  if(b.dataset.delete)await mutate(draftAction("delete",{index:Number(b.dataset.delete)}));
  if(b.dataset.pick)await mutate(draftAction("pick",{index:Number(b.dataset.pick),item_id:b.dataset.item}));
  if(b.hasAttribute("data-confirm")){await mutate(draftAction("confirm"),result=>{lastSuccess=data.mode==='table'?'Объёмы сохранены в проекте.':data.mode==="google"?"Объёмы записаны в Google Таблицу. Отчёт для CRM сохранён в очереди.":data.mode==="local"?"Отчёт сохранён в тестовом режиме.":"Отчёт записан в Google Sheets и Битрикс24.";go("today");});}
  if(b.hasAttribute("data-retry"))await mutate({action:"retry"},()=>{lastSuccess="Запись завершена.";go("today");});
  if(b.hasAttribute("data-cancel")&&confirm("Отменить черновик? Внесённые ранее объёмы сохранятся."))await mutate(draftAction("cancel"),()=>go("today"));
  if(b.hasAttribute("data-reset")){search="";filter="all";render();}
});
document.addEventListener("input",event=>{if(event.target.id==="report-text"){draftText=event.target.value;textRequestId=crypto.randomUUID();return;}if(event.target.id!=="search")return;search=event.target.value;const items=selectedItems();$("catalog-list").innerHTML=catalogRows(items,page==="remaining");document.querySelector(".list-caption").textContent=`Найдено позиций: ${items.length}`;});
$("quantity-form").addEventListener("submit",async event=>{
  event.preventDefault();if(busy||!editing)return;
  const value=$("quantity").value.trim().replace(",",".");
  if(!/^\d+(?:\.\d{1,6})?$/.test(value)||Number(value)>1e9){toast("Введите неотрицательное число, до 6 знаков после запятой.");return;}
  const {item,index,correction,request_id}=editing;
  const body=index?draftAction("quantity",{index,quantity:value}):{action:"add",item_id:item.id,quantity:value,request_id,mode:correction?"set":mode};
  await mutate(body,()=>{$("dialog").close();render();});
});
$("close-dialog").addEventListener("click",()=>{if(!busy)$("dialog").close();});
$("dialog").addEventListener("cancel",event=>{if(busy)event.preventDefault();});
$("refresh").addEventListener("click",refresh);
$("open-draft").addEventListener("click",()=>go("review"));
tg?.BackButton?.onClick(()=>go("today"));
window.addEventListener('DOMContentLoaded',refresh);

document.addEventListener("visibilitychange",()=>{if(!document.hidden&&!busy&&!$("dialog").open&&!$('voice-dialog').open&&!$('record-dialog').open)refresh();});
document.addEventListener("change",event=>{if(event.target.id==="report-start"||event.target.id==="report-end"){if(event.target.id==="report-start")reportStart=event.target.value;else reportEnd=event.target.value;reportLoaded=false;render();}});

