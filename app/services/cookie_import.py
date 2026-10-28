"""Parse pasted cookie exports as data, without evaluating code or logging values."""
import json,math,re


def parse_tiktok_cookie_text(text: str) -> list[dict]:
    text=text.strip().lstrip('\ufeff').strip()
    if not text or len(text.encode('utf-8'))>2*1024*1024:
        raise ValueError('Вставьте cookie одного аккаунта, не больше 2 МБ.')
    if text.startswith(('```','[' ,'{')):
        if text.startswith('```'):
            lines=text.splitlines()
            if len(lines)<3 or lines[-1].strip()!='```':
                raise ValueError('Скопируйте содержимое экспорта cookie целиком.')
            text='\n'.join(lines[1:-1])
        try: value=json.loads(text)
        except (ValueError,RecursionError):
            raise ValueError('JSON не читается. Вставьте полный экспорт cookie или строку Cookie.') from None
        if isinstance(value,dict) and 'accounts' in value:
            value=value['accounts']
        if isinstance(value,list) and len(value)==1 and isinstance(value[0],dict) and 'cookies' in value[0]:
            value=value[0]
        if isinstance(value,dict):
            value=value.get('cookies',[value] if 'name' in value else None)
        if not isinstance(value,list) or not value:
            raise ValueError('Ожидается экспорт cookie одного аккаунта.')
        cookies=value
    elif '\t' in text:
        cookies=[]
        for line in text.splitlines():
            if not line.strip() or (line.startswith('#') and not line.startswith('#HttpOnly_')): continue
            fields=line.split('\t',6)
            if len(fields)!=7:
                raise ValueError('Вставьте полный Netscape-экспорт: семь полей в строке.')
            domain,_,path,secure,expires,name,value=fields
            http_only=domain.startswith('#HttpOnly_')
            cookie={'name':name,'value':value,'domain':domain.removeprefix('#HttpOnly_'),'path':path,
                    'secure':secure.upper()=='TRUE','httpOnly':http_only}
            try:
                expiry=float(expires)
                if not math.isfinite(expiry): raise ValueError()
                if expiry>0: cookie['expires']=expiry
            except ValueError:
                raise ValueError('В экспорте cookie неверный срок действия.') from None
            cookies.append(cookie)
    else:
        if text.lower().startswith('cookie:'): text=text[7:].strip()
        if '\n' in text or '\r' in text:
            raise ValueError('Вставьте одну строку Cookie или экспорт в JSON/Netscape.')
        cookies=[]
        for part in text.split(';'):
            if not part.strip(): continue
            name,separator,value=part.strip().partition('=')
            if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+",name):
                raise ValueError('Строка Cookie должна содержать пары имя=значение через точку с запятой.')
            cookies.append({'name':name,'value':value,'domain':'.tiktok.com','path':'/','secure':True,'httpOnly':True})
    if not cookies or len(cookies)>1000:
        raise ValueError('Ожидается от 1 до 1000 cookie одного аккаунта.')
    keys=set()
    for item in cookies:
        if not isinstance(item,dict) or not all(isinstance(item.get(k),str) and item[k] for k in ('name','value','domain')):
            raise ValueError('Каждый cookie должен содержать имя, значение и домен.')
        if any('\n' in item[k] or '\r' in item[k] for k in ('name','value','domain')):
            raise ValueError('В полях cookie не должно быть переносов строк.')
        domain=item['domain'].lower().lstrip('.')
        if domain!='tiktok.com' and not domain.endswith('.tiktok.com'):
            raise ValueError('Вставьте cookie только TikTok. Для YouTube используйте импорт JSON.')
        path=item.get('path','/')
        if not isinstance(path,str) or not path.startswith('/'):
            raise ValueError('В cookie указан неверный путь.')
        for key in ('expires','expirationDate','expiration'):
            if item.get(key) not in (None,''):
                try: valid=math.isfinite(float(item[key]))
                except (ValueError,TypeError,OverflowError): valid=False
                if not valid: raise ValueError('В cookie указан неверный срок действия.')
        identity=(item['name'],item['domain'].lower(),path)
        if identity in keys: raise ValueError('Вставьте один набор cookie: найдены повторяющиеся записи.')
        keys.add(identity)
    return cookies
