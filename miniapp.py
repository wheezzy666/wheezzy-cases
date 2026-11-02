import hashlib, hmac, json, os, time
from urllib.parse import parse_qsl
from types import SimpleNamespace
from aiohttp import web
from telegram import Bot
import bot

bot.init_db()
TOKEN=bot.TOKEN
async def auth(request):
    raw=request.headers.get('X-Telegram-Init-Data','')
    pairs=dict(parse_qsl(raw,keep_blank_values=True)); supplied=pairs.pop('hash','')
    check='\n'.join(f'{k}={v}' for k,v in sorted(pairs.items()))
    secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    expected=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    try:
        age=time.time()-int(pairs['auth_date']); user=json.loads(pairs['user'])
        if not supplied or not hmac.compare_digest(expected,supplied) or not 0<=age<86400 or not isinstance(user['id'],int): raise ValueError()
        return user
    except (KeyError,ValueError,TypeError,json.JSONDecodeError): raise web.HTTPUnauthorized(text='Откройте приложение через Telegram.')

def state(uid):
    with bot.connect() as db:
        u=db.execute('SELECT balance, free_claimed, daily_at FROM users WHERE id=?',(uid,)).fetchone()
        items=[dict(id=r[0],name=r[1],value=r[2]) for r in db.execute('SELECT id,name,value FROM inventory WHERE user_id=? ORDER BY id DESC',(uid,)).fetchall()]
    return {'balance':u[0],'free_claimed':bool(u[1]),'daily_at':u[2],'inventory':items}

async def api(request):
    user=await auth(request); uid=user['id']; bot.ensure_user(SimpleNamespace(id=uid,first_name=user.get('first_name','Игрок')))
    if request.method=='GET': return web.json_response({'user':user.get('first_name','Игрок'),**state(uid)})
    try:
        data=await request.json(); action=data.get('action'); key=data.get('request_id','')
        if not isinstance(key,str) or len(key)<16 or len(key)>80: raise ValueError()
        key=f'web:{uid}:{key}'
        if action=='open' and data.get('case') in bot.CASES: message=bot.open_case(uid,data['case'],key)
        elif action=='sell': message=bot.sell(uid,int(data['item']),key)
        elif action=='upgrade' and data.get('mode') in bot.UPGRADES: message=bot.upgrade(uid,int(data['item']),data['mode'],key)
        elif action=='daily': message=bot.daily(uid,key)
        elif action=='free':
            try:
                m=await Bot(TOKEN).get_chat_member(bot.CHANNEL,uid)
                if m.status in ('creator','administrator','member') or (m.status=='restricted' and m.is_member): message=bot.claim_free(uid,key)
                else: message='Подписка не найдена. Подпишись на канал и попробуй снова.'
            except Exception: message='Не удалось проверить подписку. Администратору нужно добавить бота в канал.'
        else: raise ValueError()
        return web.json_response({'message':message,**state(uid)})
    except (ValueError,TypeError,KeyError): raise web.HTTPBadRequest(text='Некорректный запрос')

app=web.Application(client_max_size=16384)
app.router.add_get('/api/state',api); app.router.add_post('/api/action',api)
app.router.add_get('/',lambda request: web.FileResponse('static/index.html'))
app.router.add_static('/static','static')
if __name__=='__main__': web.run_app(app,host='0.0.0.0',port=int(os.getenv('PORT','8080')),access_log=None)
