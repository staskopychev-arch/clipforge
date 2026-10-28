(() => {
  const panel=document.getElementById('viewsPanel'),tab=document.getElementById('viewsTab');
  if(!panel || !tab)return;
  const find=id=>document.getElementById(id),fmt=new Intl.NumberFormat('ru-RU');
  const dates=new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  let latest=null,loading=false;
  const when=value=>value && Number.isFinite(new Date(value).getTime()) ? dates.format(new Date(value)) : '—';
  function render(data){
    latest=data;
    const all=data.accounts || [],known=all.filter(a=>typeof a.views==='number');
    find('viewsAccounts').textContent=fmt.format(all.length);find('viewsChecked').textContent=`${known.length} / ${all.length}`;
    const sum=known.reduce((n,a)=>n+a.views,0);
    find('viewsTotal').textContent=known.length ? `${known.some(a=>a.approximate)?'≈ ':''}${fmt.format(sum)}` : '—';
    find('viewsStatus').textContent=data.enabled ? `Автопроверка каждый час. Сейчас проверяется: ${data.checking || 0}. Последний успешный замер указан в каждой строке.${known.length<all.length || known.some(a=>!a.complete) ? ' Общая сумма пока включает не все данные.' : ''}` : 'Автопроверка выключена на сервере.';
    const filter=find('viewsFilter').value.trim().toLowerCase(),body=find('viewsRows');body.replaceChildren();
    for(const a of all.filter(a=>`${a.display_name} ${a.profile_url || ''}`.toLowerCase().includes(filter))){
      const row=document.createElement('tr');row.dataset.accountId=a.account_id;
      function cell(text){const td=document.createElement('td');td.textContent=text;row.append(td);return td;}
      const name=cell(a.display_name);
      if(/^https:\/\/www\.tiktok\.com\/@[A-Za-z0-9_.]{1,64}$/.test(a.profile_url || '')){
        const link=document.createElement('a');link.href=a.profile_url;link.textContent=a.profile_url.split('/').pop();link.target='_blank';link.rel='noopener noreferrer';
        name.append(document.createElement('br'),link);
      }
      cell(typeof a.views==='number' ? `${a.approximate?'≈ ':''}${fmt.format(a.views)}${!a.complete?' · не все ролики':''}` : '—');
      cell(a.video_count ?? '—');cell(typeof a.delta==='number' ? `${a.delta>0?'+':''}${fmt.format(a.delta)}` : '—');
      cell(when(a.checked_at));cell(a.status==='needs_login' || a.status==='manual_check' || a.status==='unavailable' ? 'После обновления входа' : a.next_check_at ? when(a.next_check_at) : 'В очереди');
      const labels={pending:'Ожидает проверки',checking:'Проверяется…',ready:'Обновлено',partial:'Не все ролики',needs_login:'Нужен вход',manual_check:'Нужна проверка в Studio',unavailable:'Аккаунт недоступен',error:'Ошибка · данные сохранены'};
      let label=labels[a.status] || 'Ожидает проверки';
      if(a.checked_at && Date.now()-new Date(a.checked_at).getTime()>7200000 && a.status==='ready')label='Устарело · ждёт обновления';
      const status=cell(label);if(a.error){const hint=document.createElement('small');hint.textContent=a.error;status.append(document.createElement('br'),hint);}
      body.append(row);
    }
    if(!body.childElementCount){const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=7;cell.textContent=all.length?'Аккаунт не найден':'Сначала добавьте TikTok-аккаунты';row.append(cell);body.append(row);}
  }
  async function load(){
    if(loading || panel.hidden || document.hidden)return;loading=true;
    try{render(await api('/api/account-views'));find('viewsError').textContent='';}
    catch(e){find('viewsError').textContent=e.message;}
    finally{loading=false;}
  }
  function open(){panel.hidden=false;tab.setAttribute('aria-expanded','true');tab.classList.add('active');load();}
  tab.addEventListener('click',()=>{open();});
  window.addEventListener('hashchange',()=>{if(location.hash==='#viewsPanel')open();});
  if(location.hash==='#viewsPanel')open();
  find('viewsFilter').addEventListener('input',()=>{if(latest)render(latest);});
  find('viewsRefresh').addEventListener('click',async()=>{
    const button=find('viewsRefresh');button.disabled=true;
    try{render(await api('/api/account-views/refresh',{method:'POST'}));find('viewsError').textContent='';toast('Проверка поставлена в очередь. Недавно проверенные аккаунты не повторяются.');}
    catch(e){find('viewsError').textContent=e.message;}
    finally{button.disabled=false;}
  });
  document.addEventListener('visibilitychange',load);setInterval(load,15000);
})();
