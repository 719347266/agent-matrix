"""Shared v1 facts, guarded writes and owner-only state transitions.

Every caller holds exclusive_lock. Remote APIs do not provide compare-and-swap:
we re-read before writing, patch only changed cells, and verify after writing.
An uncertain write stays pending; reconciliation is read-only, never a replay.
"""
from datetime import datetime, timedelta
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from .bitable import js, require_owned
from .brief_migration import single, number, validate_registry
from .schema import SOURCE_FIELDS  # noqa: F401
from .models import MatrixError
from .records import read_records
from .schema import field, date, select, STATUSES, EXTRA, FIELDS, DEFAULT_PROGRESS  # noqa: F401


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def day(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value/1000, ZoneInfo('Asia/Shanghai')).date()
    return datetime.fromisoformat(value).date()

def normalize(key, value):
    if value in (None, '', []):
        return None
    if key in {'负责人', '父事项', '等待事项'}:
        return sorted(x.get('id', x.get('record_id', '')) for x in value)
    if key == '完成时间':
        moment = (datetime.fromtimestamp(value/1000, ZoneInfo('Asia/Shanghai')) if isinstance(value, (int, float))
                  else datetime.fromisoformat(value))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
        return moment.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%dT%H:%M')
    if key in {'截止日', '首次截止日'}:
        return day(value).isoformat()
    if key in {'执行方', '状态', '验收结果', '优先级', '类型', '来源', '方向', '预做策略', '交付情况'}:
        return [value] if isinstance(value, str) else value
    if key in {'顺延次数', '延期次数'}:
        return int(value)
    if key in {'交付物链接','AI草稿链接','来源链接','关联任务链接'}:
        from .values import draft_url as _draft_url
        return _draft_url(value)
    return value

def matches(row, patch):
    return all(normalize(k, row.get(k)) == normalize(k, v) for k, v in patch.items())

def visible(row):
    return not row.get('排除本人提醒')

class Gateway:
    def __init__(self, client, state, *, on_rows=None, progress=None):
        self.client, self.state, self.config = client, state, client.config
        self.on_rows = on_rows
        self.progress = progress or (lambda message: None)
        require_owned(self.config, state)
        self.coords = ['--base-token', self.config.base_token, '--table-id', self.config.tables['事项表']]

    def rows(self):
        rows = read_records(self.client, self.state, '事项表', FIELDS)
        key = 'brief-v1:number-registry:' + self.config.tables['事项表']
        self.state.set(key, validate_registry(rows, self.state.get(key)))
        if self.on_rows:
            self.on_rows(rows)
        return rows

    def update(self, before, patch, key, source):
        rid = before['record_id']
        previous = self.state.operation(key)
        frozen = self.state.get(key + ':intent')
        if frozen:
            if frozen['record_id'] != rid:
                raise MatrixError('操作编号对应记录已变化。')
            patch = frozen['patch']
        else:
            self.state.set(key + ':intent', {'record_id':rid, 'before':before, 'patch':patch, 'source':source})
        self.progress('正在读取最新状态')
        fresh = next((x for x in self.rows() if x['record_id'] == rid), None)
        if not fresh:
            raise MatrixError('原事项不存在，未写入。')
        if previous:
            if matches(fresh, patch):
                receipt = {'record_id':rid, 'patch':patch, 'readback':fresh, 'verified_by_readback':True, 'source':source}
                if previous['status'] != 'done':
                    self.state.confirm(key, receipt)
                return receipt
            raise MatrixError('上次写入未核实或后来被修改；未重放，请核对该事项。')
        # Do not overwrite a simultaneous human change, including ownership/due date.
        if not matches(fresh, {k:v for k,v in before.items() if k not in {'record_id', '更新时间'}}):
            raise MatrixError('事项刚被修改，请重新读取后处理。')
        def write():
            self.progress('正在保存')
            receipt = self.client.call('+record-batch-update', *self.coords, '--json',
                                      js({'update_records':{rid:patch}}), write=True)
            if receipt.get('ignored_fields'):
                raise MatrixError('写入字段被忽略，结果待核对。')
            self.progress('正在核对保存结果')
            actual = next((x for x in self.rows() if x['record_id'] == rid), {})
            if not matches(actual, patch):
                raise MatrixError('写后读回尚未一致；保留待核对，不重复写入。')
            return {'record_id':rid, 'patch':patch, 'readback':actual, 'verified_by_readback':True, 'source':source}
        return self.state.once(key, write)

    def create(self, fields, key, source):
        fields = {**fields, '去重键':key}
        existing = [x for x in self.rows() if x.get('去重键') == key]
        if len(existing) > 1:
            raise MatrixError('发现重复来源记录，停止创建。')
        previous = self.state.operation(key)
        if existing:
            if not matches(existing[0], fields):
                raise MatrixError('同源事项已变化，不覆盖或重复创建。')
            receipt = {'record_id_list':[existing[0]['record_id']], 'readback':existing[0], 'verified_by_readback':True}
            if previous and previous['status'] != 'done': self.state.confirm(key, receipt)
            return receipt
        self.state.set(key + ':intent', {'fields':fields, 'source':source})
        def write():
            receipt = self.client.call('+record-batch-create', *self.coords, '--json', js({'create_records':[fields]}), write=True)
            ids = receipt.get('record_id_list', [])
            if len(ids) != 1 or not ids[0] or receipt.get('ignored_fields'):
                raise MatrixError('创建回执不完整，停止重试。')
            row = next((r for r in self.rows() if r['record_id'] == ids[0]), {})
            if not matches(row, fields): raise MatrixError('新事项读回未一致，停止重试。')
            return {**receipt, 'readback':row, 'verified_by_readback':True}
        return self.state.once(key, write)

def roll_forward(gateway, now):
    """One original-due +1/day mutation, never touch AI/others or parent rows."""
    result=[]
    for row in gateway.rows():
        if (not visible(row) or single(row.get('执行方')) != '我'
            or single(row.get('状态')) not in {'未开始','进行中','卡住'}
            or not row.get('截止日') or day(row['截止日']) > now.date()
            or [x.get('id') for x in row.get('负责人') or []] != [gateway.config.owner_open_id]):
            continue
        key = f"roll:{now.date()}:{row['record_id']}"
        if gateway.state.operation(key): continue
        old, new = day(row['截止日']), day(row['截止日']) + timedelta(days=1)
        count = int(row.get('顺延次数') or 0) + 1
        entry = {'at':now.isoformat(), 'from':old.isoformat(), 'to':new.isoformat(), 'count':count,
                 'reason':'晚报时本人事项尚未完成；默认顺延', 'source':'evening'}
        history = row.get('顺延历史') or ''
        patch = {'截止日':new.isoformat(), '顺延次数':count, '顺延历史':history+'\n'+js(entry)}
        receipt = gateway.update(row, patch, key, 'evening:auto-roll')
        result.append({'number':number(row['编号']), **entry, 'receipt':receipt})
    return result

def queue_notice(state, key, text, now, **kwargs):
    state.set('notice:'+key, {'text':text, 'at':now.isoformat(), **kwargs})

def notices(state):
    return [(key,json.loads(value)) for key,value in state.db.execute("SELECT key,value FROM kv WHERE key LIKE 'notice:%' ORDER BY key")]


def ensure_v1(client, state):
    """Additive schema for v1: extra text/flag fields and the new status options.

    Status options are appended to the existing list (full PUT of the read
    definition) so rows still holding old values keep them until migrated.
    """
    require_owned(client.config, state)
    coords = ['--base-token', client.config.base_token, '--table-id', client.config.tables['事项表']]
    from .bitable import items
    actual = {x.get('name', x.get('field_name')): x for x in items(client.call('+field-list', *coords))}
    missing = [x for x in EXTRA if x['name'] not in actual]
    created = []
    if missing:
        key = 'v1:extra-fields:' + digest([x['name'] for x in missing])
        client_result = state.once(key, lambda: client.call('+field-create', *coords, '--json', js(missing),
                                                           '--i-have-read-guide', write=True))
        created = [x['name'] for x in missing]
    status = actual.get('状态')
    if not status or status.get('type') != 'select':
        raise MatrixError('状态字段缺失或不是单选，停止迁移。')
    names = [o.get('name') for o in status.get('options', [])]
    add = [x for x in STATUSES if x not in names]
    if add:
        definition = {'name': '状态', 'type': 'select', 'multiple': False,
                      'options': [*[{k: v for k, v in o.items() if k in {'name', 'hue', 'lightness'}}
                                    for o in status.get('options', [])], *[{'name': x} for x in add]]}
        argv = ['base', '+field-update', *coords, '--field-id', status.get('id', status.get('field_id', '状态')),
                '--json', js(definition), '--as', 'user', '--format', 'json']
        key = 'v1:status-options:' + digest(add)
        def write():
            client._exec([*argv, '--dry-run'])
            # Adding options to this project's own table; existing options are kept.
            return client._exec([*argv, '--yes'])
        state.once(key, write)
        after = {x.get('name', x.get('field_name')): x for x in items(client.call('+field-list', *coords))}
        have = {o.get('name') for o in after['状态'].get('options', [])}
        if not set(STATUSES) <= have or not set(names) <= have:
            raise MatrixError('状态选项读回不完整，停止迁移。')
    return {'created_fields': created, 'added_status_options': add}


def ensure_options(client, state, field_name, wanted):
    """Append select options to one field of this project's own task table; existing ones are kept."""
    require_owned(client.config, state)
    from .bitable import items
    coords = ['--base-token', client.config.base_token, '--table-id', client.config.tables['事项表']]
    actual = {x.get('name', x.get('field_name')): x for x in items(client.call('+field-list', *coords))}
    found = actual.get(field_name)
    if not found or found.get('type') != 'select':
        raise MatrixError(f'{field_name}字段缺失或不是单选。')
    names = [o.get('name') for o in found.get('options', [])]
    add = [x for x in wanted if x not in names]
    if not add:
        return []
    definition = {'name': field_name, 'type': 'select', 'multiple': False,
                  'options': [*[{k: v for k, v in o.items() if k in {'name', 'hue', 'lightness'}}
                                for o in found.get('options', [])], *[{'name': x} for x in add]]}
    argv = ['base', '+field-update', *coords, '--field-id', found.get('id', found.get('field_id', field_name)),
            '--json', js(definition), '--as', 'user', '--format', 'json']
    def write():
        client._exec([*argv, '--dry-run'])
        return client._exec([*argv, '--yes'])
    state.once(f'v1:options:{field_name}:' + digest(add), write)
    after = {x.get('name', x.get('field_name')): x for x in items(client.call('+field-list', *coords))}
    if not set(wanted) | set(names) <= {o.get('name') for o in after[field_name].get('options', [])}:
        raise MatrixError(f'{field_name}选项读回不完整。')
    return add
