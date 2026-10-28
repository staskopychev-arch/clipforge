(() => {
  const find = id => document.getElementById(id);
  if (!find('editingForm')) return;
  const defaults = {automatic:false,start_seconds:0,end_seconds:null,speed:1,quality:'1080p',music_id:null};
  let loaded=false, dirty=false, busy=false, saving=null, latest=null, signature='';
  const read = () => ({automatic:find('editAutomatic').checked,start_seconds:Number(find('editStart').value),
    end_seconds:find('editEnd').value === '' ? null : Number(find('editEnd').value),
    speed:Number(find('editSpeed').value),quality:find('editQuality').value,music_id:null});
  function buttons() {
    for (const element of find('editingForm').querySelectorAll('input,select,button')) element.disabled=busy;
    const automatic=find('editAutomatic').checked;
    find('manualEditingFields').hidden=automatic;
    find('editAutomaticSummary').hidden=!automatic;
    for (const input of find('manualEditingFields').querySelectorAll('input,select')) input.disabled=busy || automatic;
    find('editSave').setAttribute('aria-busy',String(busy));
    find('editStatus').textContent=busy ? 'Сохраняем и проверяем…' : dirty ? 'Правки сохранятся при запуске монтажа или партии.' : automatic ? (latest?.tracks?.length ? `Автомонтаж включён. Трек выбирается автоматически из ${latest.tracks.length} доступных; при их отсутствии обрабатывается исходный звук.` : 'Автомонтаж включён. Исходный звук обрабатывается автоматически. Загружать или выбирать музыку не нужно.') : 'Настройки сохранены. Уже готовые клипы не изменяются.';
  }
  window.renderEditing = data => {
    latest=data.editing || {settings:defaults,tracks:[]};
    const next=JSON.stringify(latest.settings);
    if (!loaded || (!dirty && next !== signature)) {
      const edit={...defaults,...latest.settings};
      find('editAutomatic').checked=edit.automatic;
      find('editStart').value=edit.start_seconds;find('editEnd').value=edit.end_seconds ?? '';
      const speed=find('editSpeed');
      if (![...speed.options].some(o => Number(o.value) === edit.speed)) speed.append(new Option(`${edit.speed}×`,edit.speed));
      speed.value=String(edit.speed);find('editQuality').value=edit.quality;
      signature=next;loaded=true;
    }
    buttons();
  };
  for (const input of find('editingForm').querySelectorAll('input,select')) input.addEventListener('input',()=>{dirty=true;find('editError').textContent='';buttons();});
  async function save() {
    if (!dirty) return;
    if (saving) return saving;
    const edit=read();
    if (!edit.automatic && (!Number.isFinite(edit.start_seconds) || edit.start_seconds < 0 || edit.end_seconds !== null && edit.end_seconds <= edit.start_seconds)) throw new Error('Конец фрагмента должен быть позже начала. Проверьте время фрагмента.');
    saving=(async()=>{
      busy=true;buttons();find('editError').textContent='';
      try {
        state.editing=await api('/api/editing',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(edit)});
        dirty=false;window.renderEditing(state);
      } catch(e) {find('editError').textContent=e.message;throw e;}
      finally {busy=false;saving=null;buttons();}
    })();
    return saving;
  }
  window.ensureEditingSaved=async () => { return save(); };
  find('editingForm').addEventListener('submit',async event=>{
    event.preventDefault();try {await save();toast('Настройки монтажа сохранены.');} catch(e) {find('editError').textContent=e.message;}
  });
  window.renderEditing(state);
})();
