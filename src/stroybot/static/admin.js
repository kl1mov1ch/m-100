/* Only the server grants permissions. These controls never receive API keys. */
let accessContext={is_admin:false,projects:[]}, adminData=null;
function renderAccess(){
  const box=document.getElementById('project-switcher');
  box.hidden=!accessContext.is_admin;
  document.getElementById('select-project').innerHTML=accessContext.projects.map(p=>`<option value="${esc(p.id)}" ${p.id===accessContext.project_id?'selected':''}>${esc(p.name)}</option>`).join('');
  document.getElementById('select-project').disabled=accessContext.projects.length<2;
  document.getElementById('admin-open').hidden=!accessContext.is_admin;
}
function adminPage(){
  if(!adminData)return heading('Управление','Загружаем настройки…');
  const options=adminData.projects.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  return heading('Управление','Объекты, сотрудники и подключения')+projectMembers()+crmPanel()+
    `<section class="admin-card"><h2>Добавить проект из таблицы</h2><p class="note">Загрузите файл с любыми названиями и порядком колонок. Перед созданием проекта вы увидите данные и выберете заголовки.</p><label class="upload-file">Загрузить таблицу<input type="file" id="import-file" accept=".xlsx,.xls,.xlsm,.ods,.csv,.tsv,.txt,.json"></label><p class="note">Excel, ODS, CSV, TSV или JSON · до 12 МБ.</p></section>`+importPanel()+
    `<details class="admin-card"><summary>Подключить Google Таблицу</summary><p class="note">Рабочая смета подключается для записи подтверждённых объёмов прямо в Google. Для других структур можно создать отдельную копию. Откройте доступ редактора для <strong>${esc(adminData.google_service_account||'сервисного аккаунта из вашего JSON-ключа')}</strong>.</p><form id="add-project-form" class="admin-form"><label>Название проекта<input name="name" maxlength="80" required placeholder="Например: Офис, Минск"></label><label>Ссылка на Google-таблицу<input name="url" type="url" required placeholder="https://docs.google.com/spreadsheets/d/…"></label><label>Название листа<input name="sheet" maxlength="100" required value="Лист1"></label><label>Режим работы<select name="mode"><option value="work">Рабочая смета — запись в Google</option><option value="copy">Копия произвольной таблицы в приложении</option></select></label><p class="note">Для сметы: B — название, C — единица, D — план, строка 3 — даты. Копия работает с любыми полями.</p><button class="primary" type="submit">Подключить таблицу</button></form></details>`+
    `<section class="admin-card" id="account-editor"><h2>Выдать или изменить доступ</h2><p class="note">Пользователь узнает свой Telegram ID командой /id в боте. Сотрудник видит только назначенный объект и сам не может его менять. Администратор управляет всеми подключёнными объектами и доступами.</p><form id="add-account-form" class="admin-form"><label>Telegram ID<input name="id" inputmode="numeric" pattern="[0-9]+" required></label><label>Имя пользователя<input name="name" maxlength="80" required></label><label>Роль<select name="role"><option value="worker">Мастер</option><option value="admin">Администратор</option></select></label><label>Основной объект<select name="project">${options}</select></label><button class="primary" type="submit">Сохранить доступ</button></form></section>`+
    `<section class="admin-card"><h2>Пользователи</h2>${adminData.accounts.map(a=>`<div class="account-row"><div><strong>${esc(a.name)}</strong><p>${esc(a.id)} · ${a.role==='admin'?'Администратор — все объекты':esc(adminData.projects.find(p=>p.id===a.project)?.name||'Объект не найден')} · ${a.enabled?'Доступ открыт':'Отключён'}</p></div>${!adminData.owner_ids.includes(a.id)&&a.id!==adminData.actor_id?`<button class="text-button" data-edit-account="${a.id}">Изменить объект / роль</button>`:''}${!adminData.owner_ids.includes(a.id)&&a.id!==adminData.actor_id&&a.enabled?`<button class="text-button danger" data-disable-account="${esc(a.id)}">Отключить</button>`:'<span class="note">'+(adminData.owner_ids.includes(a.id)?'Владелец':'')+'</span>'}</div>`).join('')}</section>`;
}
async function loadAdmin(){if(busy)return;lock(true);try{adminData=await api('/api/admin');go('admin');}catch(e){toast(e.message);}finally{lock(false);applyDisabled();}}
async function saveAccess(body){if(busy)return;lock(true);try{const result=await api('/api/access',body);accessContext=await api('/api/access');renderAccess();adminData=await api('/api/admin');render();toast(result.message);}catch(e){toast(e.message);}finally{lock(false);applyDisabled();}}
document.addEventListener('click',event=>{const b=event.target.closest('button');if(!b||busy)return;if(b.dataset.page==='admin')loadAdmin();if(b.dataset.disableAccount)saveAccess({action:'disable',id:b.dataset.disableAccount});});
document.addEventListener('submit',async event=>{
  if(!['add-project-form','add-account-form','crm-form'].includes(event.target.getAttribute('id')))return;
  event.preventDefault();const fields=Object.fromEntries(new FormData(event.target));
  if(event.target.getAttribute('id')==='add-account-form'){saveAccess({...fields,action:'account'});return;}
  if(event.target.getAttribute('id')==='crm-form'){saveAccess({...fields,action:'crm_connect'});return;}
  if(busy)return;lock(true);let connected=false;
  try{
    if(fields.mode==='copy'){importPreview=await api('/api/import',{action:'google',url:fields.url,sheet:fields.sheet});importName=fields.name;render();}
    else{const result=await api('/api/access',{action:'project',name:fields.name,url:fields.url,sheet:fields.sheet});connected=true;page='today';tablePageData=null;tableEvents=null;tableQuery='';toast(result.message);}
  }catch(e){toast(e.message);}finally{lock(false);applyDisabled();}
  if(connected)await refresh();
});
document.addEventListener('change',async event=>{if(event.target.getAttribute('id')!=='select-project'||busy||!accessContext.is_admin)return;lock(true);try{await api('/api/access',{action:'select',project:event.target.value});reportLoaded=false;adminData=null;draftText='';mode='add';tablePageData=null;tableEvents=null;tableQuery='';textRequestId=crypto.randomUUID();page='today';}catch(e){toast(e.message);}finally{lock(false);}await refresh();});

function projectMembers(){
  return '<section class="admin-card"><h2>Объекты и сотрудники</h2>'+adminData.projects.map(p=>`<div class="account-row"><div><strong>${esc(p.name)}</strong><p>${adminData.accounts.filter(a=>a.enabled&&a.role==='worker'&&a.project===p.id).map(a=>esc(a.name)).join(', ')||'Сотрудники не назначены'}</p></div><button class="text-button" data-project-members="${p.id}">Назначить сотрудника</button></div>`).join('')+'</section>';
}
function crmPanel(){
  const c=adminData.crm||{};
  return `<details class="admin-card"><summary>Битрикс24 · ${c.configured?'Подключён':'Требует подключения'}</summary><p class="note">${esc(c.portal||'')}. Одна дневная сделка на объект. Подтверждённые объёмы обновляют ту же сделку. Сбои CRM не мешают сохранять объёмы в таблицу.</p><p class="note">В портале: «Разработчикам → Другое → Входящий вебхук». Разрешите CRM и вставьте полный адрес ниже. REST API должен быть доступен на тарифе портала. Ключ не появится в списках и сообщениях.</p><form id="crm-form" class="admin-form"><label>Входящий вебхук<input name="webhook" type="password" autocomplete="off" required placeholder="https://…bitrix24.ru/rest/…/…/"></label><label>ID воронки<input name="category" inputmode="numeric" pattern="[0-9]+" value="${esc(c.category??0)}" required></label><p class="note">0 — основная воронка. Проверяется доступ к полям CRM; проверочная сделка не создаётся.</p><button type="submit" class="primary">Проверить и подключить</button></form>${c.configured?'<button class="secondary compact" data-crm-sync>Отправить отчёты из очереди</button>':''}</details>`;
}
document.addEventListener('click',event=>{
  const b=event.target.closest('button');if(!b||busy)return;
  if(b.dataset.editAccount){
    const a=adminData.accounts.find(a=>String(a.id)===b.dataset.editAccount),f=$('add-account-form');
    if(!a)return;for(const key of ['id','name','role','project'])f.elements[key].value=a[key];f.elements.id.readOnly=true;
    $('account-editor').scrollIntoView({block:'start'});f.elements.name.focus();
  }
  if(b.dataset.projectMembers){const f=$('add-account-form');f.reset();f.elements.id.readOnly=false;f.elements.project.value=b.dataset.projectMembers;$('account-editor').scrollIntoView({block:'start'});f.elements.id.focus();}
  if(b.hasAttribute('data-crm-sync'))saveAccess({action:'crm_sync'});
});

