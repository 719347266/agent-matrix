"""One global mobile budget and exactly one outbox slot per edition/day."""
from datetime import datetime, date, time, timedelta
import re
from zoneinfo import ZoneInfo
from .agents.daily_brief import _calendar, _capacity, _item
from .brief_migration import single, number
from .delivery import send_once, message_id
from .models import MatrixError
from .workflow import Gateway, day, visible, digest, notices, roll_forward, DEFAULT_PROGRESS


def visible_text(text):
    return re.sub(r'\[([^\]]*)\]\(https?://[^\s)]+\)', r'\1', text)


def short(value, length):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    text = re.sub(r'https?://\S+', '链接', text)
    return text if len(text) <= length else text[:length-1]+'…'


def signature(row):
    return digest({k:row.get(k) for k in ['状态','进度','卡点','交付物链接','退回意见']})


def build(config, rows, events, target, edition, *, previous=None, catchup=False, summaries=(), rolls=(), board_url=None):
    previous=previous or {}
    tz=ZoneInfo(config.timezone)
    active=[r for r in rows if visible(r) and single(r.get('状态')) not in {'完成','取消'}]
    for r in active:
        number(r.get('编号'))
        # A missing executor is only allowed while the item waits for the owner's decision.
        if single(r.get('执行方')) not in {'我','AI','别人'} and single(r.get('状态'))!='待确认':
            raise MatrixError(f"#{r.get('编号')} 缺少执行方；未发送不完整简报。")
        if not r.get('进度') or len(str(r['进度']))>15:
            r['进度']=DEFAULT_PROGRESS.get(single(r.get('状态')),'进度待更新')
        if single(r['状态'])=='卡住' and not r.get('卡点'):
            r['卡点']='卡点未写明'
    cal, intervals=_calendar(events,target,tz)
    planned=[]
    for r in active:
        if single(r.get('执行方'))=='我' and single(r.get('状态')) in {'未开始','进行中'}:
            item=_item({**r,'状态':['待办']},config,target,tz); item['mine']=True; planned.append(item)
    capacity=_capacity(config,target,tz,intervals,planned)
    def sort(r):
        due=day(r.get('截止日'))
        return (0 if due and due<target else 1 if due==target else 2,
                0 if single(r.get('优先级'))=='P0' else 1, due or date.max, number(r['编号']))
    active.sort(key=sort)
    today=target-timedelta(days=edition=='evening')
    completed=[r for r in rows if visible(r) and single(r.get('状态'))=='完成' and day(r.get('完成时间'))==today]
    head=f"{target:%m/%d} {'早报' if edition=='morning' else '明日计划'}"
    if edition=='evening': head=f"{today:%m/%d} 晚报 · 今日完成{len(completed)} · 明天{target:%m/%d}"
    head+=f" · 会{len(cal['accepted'])}场{cal['confirmed_meeting_minutes']/60:g}h" if cal['accepted'] else ''
    if capacity['configured']:
        head+=f" · 可用{capacity['available_minutes']/60:g}h"
        if capacity['exceeds_planning_limit']: head+=' ⚠ 排不下'
    if catchup: head+=' · 补发'
    if not active and not cal['accepted'] and not summaries and not completed:
        return {'text':('明天' if edition=='evening' else '今天')+'没有要做和要验收的事。','line_count':1,'visible_chars':17,'ai_signatures':{},'included_notices':[]}
    groups=[]
    mine=[r for r in active if single(r['执行方'])=='我' and single(r['状态'])!='待确认']
    review=[r for r in active if single(r['执行方'])=='AI' and single(r['状态'])=='待验收']
    decisions=[r for r in active if single(r['状态'])=='待确认']
    ai=[r for r in active if single(r['执行方'])=='AI' and single(r['状态']) not in {'待确认','待验收'}]
    others=[r for r in active if single(r['执行方'])=='别人' and single(r['状态']) not in {'待确认','待验收'} and day(r.get('截止日')) and day(r['截止日'])<=target]
    other_review=[r for r in active if single(r['执行方'])=='别人' and single(r['状态'])=='待验收']
    roll_map={r['number']:r for r in rolls}
    def line(r, decision=False):
        n=number(r['编号']); st=single(r['状态']); ex=single(r.get('执行方'))
        title=short(r.get('标题'),16)
        if decision:
            tail=('请你指定负责人' if not r.get('负责人') and ex!='别人' else '请定日期' if not r.get('截止日') else '谁来做：我/AI')
        elif st=='卡住':
            tail='卡住：'+short(r.get('卡点'),16)
        elif st=='待验收':
            tail=short(r.get('预做结果摘要'),14)
        elif ex in {'AI','别人'}:
            tail=short(r.get('进度'),15)
        else:
            tail=''
        tags=[]
        due=day(r.get('截止日'))
        count=int(r.get('顺延次数') or 0)
        if n in roll_map:
            rr=roll_map[n]; tags.append(f"{rr['from'][5:]}→{rr['to'][5:]} 顺延{count}次"+(' ⚠改计划?' if count>=2 else ''))
        else:
            if due and st!='待验收' and not decision:
                late=(target-due).days
                if late>0: tags.append(f'逾期{late}天')
                elif late==0: tags.append('今天到期')
                elif late==-1: tags.append('明天到期')
            if count>=2: tags.append(f'⚠顺延{count}次')
        url=r.get('交付物链接')
        from .prework import _draft_url
        url=_draft_url(url)
        suffix=f' [打开]({url})' if st=='待验收' and url else ''
        if ex=='我' and r.get('能否AI预做') and st in {'未开始','进行中'}:
            tags.append('AI可先起草')
        return ' · '.join([f"#{n} {title}",*[x for x in [tail,*tags] if x]])+suffix
    for label, values, cap in [('你要做',mine,3),('等你验收',review,5),('等你拍板',decisions,3),('AI 在做',ai,100),('别人在做',others,100)]:
        entries=[]
        unchanged=[]
        for r in values:
            if label=='AI 在做' and previous.get(r['record_id'])==signature(r): unchanged.append(r);continue
            entries.append((line(r,label=='等你拍板'),[r['record_id']],None))
        if unchanged: entries.append((f'AI 另有{len(unchanged)}件进行中，无变化',[r['record_id'] for r in unchanged],None))
        if label=='等你验收' and other_review:
            entries.insert(0,(f'别人 · 待验收{len(other_review)}件 · 已交付，请验收',[r['record_id'] for r in other_review],None))
        if entries:groups.append((label,entries[:cap]))
    for key,notice in summaries:
        groups.append(('',[(short(notice['text'],65),[],key)]))
    board=board_url or config.base_url
    lines=[head]; included=set();included_notices=[]
    sample=next((number(r['编号']) for r in review),None) or next((number(r['编号']) for r in active),12)
    def footer(ids):
        rest=len(active)-len(ids)
        return (f'其余{rest}件 · ' if rest else '')+f'[看板]({board}) · 回复：{sample} 过 / {sample} 退 意见'
    # Keep a footer reserved throughout; sections share the same line/character budget.
    for label, entries in groups:
        heading_added=False
        for text,ids,key in entries:
            extra=([label] if label and not heading_added else [])+[text]
            proposed=lines+extra+[footer(included|set(ids))]
            if len(proposed)>15 or len(visible_text('\n'.join(proposed)))>400:continue
            lines+=extra;heading_added=True;included.update(ids)
            if key:included_notices.append(key)
    lines.append(footer(included))
    result='\n'.join(lines)
    assert len(lines)<=15 and len(visible_text(result))<=400
    return {'text':result,'line_count':len(lines),'visible_chars':len(visible_text(result)),
            'ai_signatures':{r['record_id']:signature(r) for r in ai if r['record_id'] in included},
            'included_notices':included_notices,'completed_today':len(completed),'included_records':sorted(included)}


def verify_delivery(client, receipt):
    mid=message_id(receipt)
    if not mid:raise MatrixError('消息回执为空，停止重发。')
    data=client._exec(['im','+messages-mget','--message-ids',mid,'--no-reactions','--as','bot','--format','json'])
    found=[m for m in data.get('messages',[]) if m.get('message_id')==mid]
    if len(found)!=1:raise MatrixError('发送回执尚未读回，不自动重发。')
    return {**receipt,'verified_by_readback':True,'message_readback':found[0], 'recipient':client.config.owner_open_id}


def deliver(client,state,key,text):
    frozen=state.get(key+':text')
    if frozen is None:state.set(key+':text',text)
    return send_once(state,key,lambda:verify_delivery(client,client.send_self(frozen or text,key)))


def deliver_card(client,state,key,card_json):
    frozen=state.get(key+':card')
    if frozen is None:state.set(key+':card',card_json)
    return send_once(state,key,lambda:verify_delivery(client,client.send_card(frozen or card_json,key)))


def run(config, client, state, *, edition='morning', target_date=None, preview=False, now=None):
    now=now or datetime.now(ZoneInfo(config.timezone))
    target=target_date or now.date()+timedelta(days=edition=='evening')
    if isinstance(target,str):target=date.fromisoformat(target)
    slot=datetime.combine(now.date(),time(8,30) if edition=='morning' else time(21),now.tzinfo)
    if not preview and target!=now.date()+timedelta(days=edition=='evening'):
        raise MatrixError('跨日简报仅可预览，不补发历史消息。')
    if not preview and now<slot:return {'ok':True,'sent':False,'reason':'before_slot'}
    key=f'brief:{edition}:{target.isoformat()}' # Reuse old outbox keys: no duplicate on upgrade.
    previous=state.operation(key)
    if previous and not preview:
        if previous['status']=='done' and message_id(previous['receipt']):
            return {'ok':True,'sent':False,'already_sent':True,'message_id':message_id(previous['receipt'])}
        raise MatrixError('该时段已有未确认发送，停止重发。')
    gateway=Gateway(client,state)
    rolls=roll_forward(gateway,now) if edition=='evening' and not preview else []
    start=datetime.combine(target,time(),now.tzinfo)
    events=client._exec(['calendar','+agenda','--start',start.isoformat(),'--end',(start+timedelta(days=1)).isoformat(),'--as','user','--format','json'])
    rows=gateway.rows()
    result=build(config,rows,events,target,edition,previous=state.get('brief-v1:last-ai',{}),
                 catchup=not preview and now>=slot+timedelta(minutes=1),summaries=[(k,v) for k,v in notices(state) if not v.get('delivered')],
                 rolls=rolls,board_url=state.get('brief-v1:board-url'))
    result.update(ok=True,preview=preview,sent=False,rows=rows,events=events,rolls=rolls,operation_key=key)
    result['card']=build_card(config,rows,events,target,edition,catchup=result['text'].count('补发')>0,
                              rolls=rolls,board_url=state.get('brief-v1:board-url'))
    if preview:return result
    receipt=deliver_card(client,state,key,result['card'])
    state.set('brief-v1:last-ai',result['ai_signatures'])
    for k in result['included_notices']:
        notice=state.get(k);notice['delivered']=message_id(receipt);state.set(k,notice)
    return {**result,'sent':True,'message_id':message_id(receipt),'receipt':receipt}


def batch(config,client,state,now=None):
    now=now or datetime.now(ZoneInfo(config.timezone))
    # Missed result slots wait for the next permitted batch; no third catch-up notification.
    if now.hour not in {12,17} or now.minute>10:return {'ok':True,'sent':False,'reason':'outside_batch'}
    key=f'results:{now.date()}:{now.hour}'
    if state.operation(key):return {'ok':True,'sent':False,'reason':'existing_slot'}
    rows=[r for r in Gateway(client,state).rows() if visible(r) and single(r.get('执行方'))=='AI'
          and single(r.get('状态'))=='待验收' and state.get('result-notified:'+r['record_id'])!=signature(r)]
    if not rows:return {'ok':True,'sent':False,'reason':'empty'}
    built=build(config,rows,[],now.date(),'morning',board_url=state.get('brief-v1:board-url'))
    text=built['text'].replace(f'{now:%m/%d} 早报',f'{now:%m/%d} {now.hour}:00 成果待验收',1)
    if len(visible_text(text))>400: text=built['text']
    receipt=deliver(client,state,key,text)
    from .prework_store import WorkQueue
    queue=WorkQueue(state)
    for r in rows:
        if r['record_id'] in built.get('included_records',[]):
            state.set('result-notified:'+r['record_id'],signature(r))
            for entry in queue.list():
                if entry['record_id']==r['record_id'] and entry['status']=='prepared':
                    queue.done(entry['record_id'],entry['input_version'],receipt)
    return {'ok':True,'sent':True,'receipt':receipt,'text':text}


# ---- Feishu card version (what the owner actually reads) ---------------------

def _clean_summary(value, limit=30):
    text = re.sub(r'\s+', '', str(value or ''))
    text = re.sub(r'https?://\S+', '', text)
    first = re.split(r'[。；;！!]', text)[0]
    return first if len(first) <= limit else first[:limit-1] + '…'


def describe(r, target, by_number):
    """Plain words for one item: (bold line, grey hint, hint colour, url-to-open)."""
    from .prework import _draft_url
    n = number(r['编号']); st = single(r['状态']); ex = single(r.get('执行方'))
    title = short(r.get('标题'), 22)
    due = day(r.get('截止日'))
    url = _draft_url(r.get('交付物链接'))
    when, color = '', 'grey'
    if due and st not in {'待验收', '完成', '取消'}:
        late = (target - due).days
        when, color = ((f'逾期 {late} 天', 'red') if late > 0 else ('今天到期', 'orange') if late == 0
                       else ('明天到期', 'grey') if late == -1 else (f'{due:%m-%d} 截止', 'grey'))
    hint = when
    if st == '待确认':
        if not ex:
            hint, color = f'谁来做？回「{n} 我来」「{n} AI做」或「{n} 给某人」', 'grey'
        elif not r.get('截止日'):
            hint, color = f'什么时候做完？回「{n} 周五」，不做回「{n} 不做」', 'grey'
        else:
            hint, color = f'要做吗？回「{n} 我来」或「{n} 不做」', 'grey'
    elif st == '卡住':
        blocker = str(r.get('卡点') or '')
        wait = re.search(r'#(\d+)', blocker)
        if wait and ex == '我':
            m = int(wait.group(1))
            hint = f'AI 已备好 #{m}，先看一眼，没问题回「{m} 过」就能开始' + (f' · {when}' if when else '')
        elif ex == 'AI':
            hint, color = f'AI 卡住：{short(blocker, 18)}，补材料回「{n} + 材料」', 'orange'
        else:
            hint = f'卡住：{short(blocker, 20)}' + (f' · {when}' if when else '')
    elif st == '待验收':
        hint, color = _clean_summary(r.get('预做结果摘要')) or '点打开看看，没问题回「过」', 'grey'
    elif ex == 'AI':
        hint, color = 'AI 在做 · ' + short(r.get('进度'), 15), 'grey'
    elif ex == '我' and r.get('能否AI预做') and st in {'未开始', '进行中'}:
        hint = (when + ' · ' if when else '') + f'AI 可以先帮你备材料，回「{n} AI备」'
    count = int(r.get('顺延次数') or 0)
    if count >= 2 and st not in {'完成', '取消', '待验收'}:
        hint += f' · 已顺延 {count} 次'
        color = 'red'
    return f'#{n} {title}', hint, color, url if st == '待验收' else None


def _row_item(r_line, button_url=None):
    head, hint, color, _ = r_line
    text = [{'tag': 'markdown', 'content': f'**{head}**'}]
    if hint:
        text.append({'tag': 'markdown', 'content': f"<font color='{color}'>{hint}</font>", 'text_size': 'notation'})
    if not button_url:
        return {'tag': 'column_set', 'flex_mode': 'none', 'columns': [
            {'tag': 'column', 'width': 'weighted', 'weight': 1, 'vertical_spacing': '2px', 'elements': text}]}
    return {'tag': 'column_set', 'flex_mode': 'none', 'horizontal_spacing': '8px', 'columns': [
        {'tag': 'column', 'width': 'weighted', 'weight': 4, 'vertical_spacing': '2px', 'elements': text},
        {'tag': 'column', 'width': 'auto', 'vertical_align': 'center', 'elements': [
            {'tag': 'button', 'text': {'tag': 'plain_text', 'content': '打开'}, 'type': 'primary', 'size': 'small',
             'behaviors': [{'type': 'open_url', 'default_url': button_url}]}]}]}


def _block(title, color, items, more=0, *, fill=None):
    elements = [{'tag': 'markdown', 'content': f"**<font color='{color}'>{title}</font>**"}, *items]
    if more:
        elements.append({'tag': 'markdown', 'content': f"<font color='grey'>还有 {more} 件，在看板里</font>", 'text_size': 'notation'})
    block = {'tag': 'interactive_container', 'width': 'fill', 'has_border': True, 'border_color': f'{color}-100' if color != 'grey' else 'grey-200',
             'corner_radius': '8px', 'padding': '12px 12px 12px 12px', 'vertical_spacing': '8px', 'elements': elements}
    if fill:
        block['background_style'] = fill
    return block


def card(header, subtitle, template, blocks, footer_hint, board_url, *, tag=None, summary=''):
    footer = [{'tag': 'column_set', 'flex_mode': 'none', 'columns': [
        {'tag': 'column', 'width': 'weighted', 'weight': 4, 'vertical_align': 'center', 'elements': [
            {'tag': 'markdown', 'content': f"<font color='grey'>{footer_hint}</font>", 'text_size': 'notation'}]},
        {'tag': 'column', 'width': 'auto', 'vertical_align': 'center', 'elements': [
            {'tag': 'button', 'text': {'tag': 'plain_text', 'content': '打开看板'}, 'type': 'default', 'size': 'small',
             'behaviors': [{'type': 'open_url', 'default_url': board_url}]}]}]}] if board_url else [
        {'tag': 'markdown', 'content': f"<font color='grey'>{footer_hint}</font>", 'text_size': 'notation'}]
    head = {'title': {'tag': 'plain_text', 'content': header}, 'template': template,
            'icon': {'tag': 'standard_icon', 'token': 'todo_colorful'}}
    if subtitle:
        head['subtitle'] = {'tag': 'plain_text', 'content': subtitle}
    if tag:
        head['text_tag_list'] = [{'tag': 'text_tag', 'text': {'tag': 'plain_text', 'content': tag}, 'color': 'orange'}]
    return {'schema': '2.0', 'config': {'width_mode': 'default', 'summary': {'content': summary or header}},
            'header': head, 'body': {'direction': 'vertical', 'padding': '12px 12px 20px 12px',
                                     'vertical_spacing': '12px', 'elements': [*blocks, *footer]}}


REPLY_HINT = '回复「编号 + 动作」就行：{n} 过 · {n} 退 要改的地方 · {n} 周五（改日期）· 展开 {n}（看详情）'


def build_card(config, rows, events, target, edition, *, catchup=False, rolls=(), board_url=None):
    tz = ZoneInfo(config.timezone)
    active = [r for r in rows if visible(r) and single(r.get('状态')) not in {'完成', '取消'}]
    by_number = {number(r['编号']): r for r in rows}
    cal, _ = _calendar(events, target, tz)
    def sort(r):
        due = day(r.get('截止日'))
        return (0 if due and due < target else 1 if due == target else 2,
                0 if single(r.get('优先级')) == 'P0' else 1, due or date.max, number(r['编号']))
    active.sort(key=sort)
    st = lambda r: single(r.get('状态')); ex = lambda r: single(r.get('执行方'))
    mine = [r for r in active if ex(r) == '我' and st(r) != '待确认']
    review = [r for r in active if ex(r) in {'AI', '别人'} and st(r) == '待验收']
    decide = [r for r in active if st(r) == '待确认']
    ai = [r for r in active if ex(r) == 'AI' and st(r) not in {'待确认', '待验收'}]
    today = target - timedelta(days=edition == 'evening')
    done_today = [r for r in rows if visible(r) and st(r) == '完成' and day(r.get('完成时间')) == today]
    blocks = []
    if rolls:
        items = [_row_item((f"#{x['number']} {short(by_number.get(x['number'], {}).get('标题'), 22)}",
                        f"{x['from'][5:]} → {x['to'][5:]} · 第 {x['count']} 次顺延" + (f"，要改计划吗？回「{x['number']} 下周一」或「{x['number']} 不做」" if x['count'] >= 2 else ''),
                        'red' if x['count'] >= 2 else 'grey', None)) for x in rolls[:4]]
        blocks.append(_block('今天没做完，已顺延到明天', 'grey', items, max(0, len(rolls) - 4)))
    if cal['accepted']:
        lines = '\n'.join(f"{e['time']}  {short(e['title'], 24)}" for e in cal['accepted'][:4])
        more = len(cal['accepted']) - 4
        blocks.append(_block(('明天' if edition == 'evening' else '今天') + f"的会 · {len(cal['accepted'])} 场", 'grey',
                             [{'tag': 'markdown', 'content': lines + (f"\n<font color='grey'>还有 {more} 场</font>" if more > 0 else '')}]))
    if mine:
        blocks.append(_block(('明天你要做' if edition == 'evening' else '今天你要做') + f' · {len(mine)} 件', 'blue',
                             [_row_item(describe(r, target, by_number)) for r in mine[:4]], max(0, len(mine) - 4), fill='blue-50'))
    if review:
        blocks.append(_block(f'AI 做好了，等你看 · {len(review)} 件', 'blue',
                             [_row_item(describe(r, target, by_number), describe(r, target, by_number)[3]) for r in review[:5]],
                             max(0, len(review) - 5)))
    if decide:
        blocks.append(_block(f'需要你定 · {len(decide)} 件', 'violet',
                             [_row_item(describe(r, target, by_number)) for r in decide[:3]], max(0, len(decide) - 3)))
    if ai and len(blocks) < 5:
        blocks.append(_block(f'AI 正在做 · {len(ai)} 件', 'grey',
                             [_row_item(describe(r, target, by_number)) for r in ai[:3]], max(0, len(ai) - 3)))
    weekday = '一二三四五六日'[target.weekday()]
    if edition == 'evening':
        header = f'明天 {target:%m/%d} 周{weekday} · 计划'
        subtitle = f'今天完成 {len(done_today)} 件 · 明天 {len(cal["accepted"])} 场会 · 你要做 {len(mine)} 件 · 等你看 {len(review)} 件'
    else:
        header = f'{target:%m/%d} 周{weekday} · 早报'
        subtitle = f'{len(cal["accepted"])} 场会 · 你要做 {len(mine)} 件 · 等你看 {len(review)} 件'
    if not blocks:
        blocks.append({'tag': 'markdown', 'content': ('明天' if edition == 'evening' else '今天') + '没有要做和要验收的事。'})
    sample = next((number(r['编号']) for r in review), None) or next((number(r['编号']) for r in active), 12)
    summary = f"{header}：你要做 {len(mine)} 件，等你看 {len(review)} 件"
    return card(header, subtitle, 'indigo' if edition == 'evening' else 'blue', blocks, REPLY_HINT.format(n=sample),
                board_url or config.base_url, tag='补发' if catchup else None, summary=summary)
