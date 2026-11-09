"""Durable browser conversations with a worker independent of the engineering queue."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler

from python_control import runtime
from .schemas import TaskValidationError
from .engineering_results import result_cards


SESSION_ID = re.compile(r'chat_[a-f0-9]{24}')
REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')
EXECUTE_PREVIEW = re.compile(r'^(?:请)?(?:确认)?(?:执行(?:当前|这份|上次|上一个|上一次)?预览|'
                             r'按(?:当前|上次|上一次)预览(?:执行|计算)|执行这组尺寸)[。！!\s]*$')


class ChatService:
    def __init__(self, settings, app_url, job_lookup):
        self.settings, self.app_url, self.job_lookup = settings, app_url, job_lookup
        self.root = Path(settings.root) / 'agent_chats'
        self.python = Path(settings.root) / '.venv-mcp' / 'Scripts' / 'python.exe'
        self.instance = uuid.uuid4().hex
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.monitored = set()

    def directory(self, session_id):
        if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
            raise TaskValidationError('CHAT_ID', '对话编号格式无效。')
        path = self.root / session_id
        if path.resolve() != path:
            raise TaskValidationError('CHAT_PATH', '对话目录不能重定向。')
        if not path.is_dir():
            raise TaskValidationError('CHAT_NOT_FOUND', '没有找到对应对话。')
        return path

    def create(self):
        session_id = 'chat_' + uuid.uuid4().hex[:24]
        folder = self.root / session_id
        folder.mkdir(parents=True, exist_ok=False)
        state = {'schema_version': 'disk-agent.chat.v1', 'session_id': session_id, 'status': 'ready',
                 'created_at': runtime.local_time(), 'messages': [], 'events': [], 'turns': {},
                 'context': {'run_id': None, 'optimization_policy': None, 'auto_recover': True},
                 'pending_preview': None, 'jobs': [], 'result_cards': [], 'active_request_id': None,
                 'owner_instance': self.instance, 'owner_url': self.app_url, 'token_budget': 12000}
        runtime.write_json(folder / 'state.json', state)
        return self.snapshot(session_id)

    def sessions(self):
        records = []
        for path in self.root.glob('chat_*/state.json'):
            try:
                state = self._read(path.parent.name)
                title = next((item['text'] for item in state['messages'] if item['role'] == 'user'), '新对话')
                records.append({'session_id': state['session_id'], 'title': title[:60],
                                'updated_at': state.get('updated_at', state['created_at']), 'status': state['status']})
            except (OSError, ValueError, runtime.bridge.BridgeError, TaskValidationError):
                continue
        return {'ok': True, 'sessions': sorted(records, key=lambda item: item['updated_at'], reverse=True)[:20]}

    def _read(self, session_id):
        for attempt in range(4):
            try:
                with self.lock:
                    state = runtime.read_json(self.directory(session_id) / 'state.json')
                break
            except runtime.bridge.BridgeError as exc:
                sharing_conflict = sys.platform == 'win32' and getattr(exc.__cause__, 'winerror', None) in (5, 32, 33)
                if not sharing_conflict or attempt == 3:
                    raise
                time.sleep(.01 * (attempt + 1))
        if state.get('session_id') != session_id:
            raise TaskValidationError('CHAT_RECORD', '对话记录与编号不一致。')
        return state

    def _save(self, state):
        state['updated_at'] = runtime.local_time()
        runtime.write_json(self.directory(state['session_id']) / 'state.json', state)

    def snapshot(self, session_id):
        state = self._read(session_id)
        status = state['status']
        if status == 'working' and state.get('owner_instance') != self.instance:
            status = 'working_elsewhere' if self._live_owner(state) else 'interrupted'
        requests = sum(turn.get('model_requests', 0) for turn in state['turns'].values())
        tokens = [usage.get('total_tokens') for turn in state['turns'].values()
                  for usage in turn.get('usage', []) if isinstance(usage, dict)]
        known = [value for value in tokens if type(value) is int and value >= 0]
        complete = len(known) == requests
        jobs = []
        cards = {card['card_id']: card for card in state.get('result_cards', [])}
        for saved in state['jobs']:
            try:
                job = self.job_lookup(saved['job_id'])
            except Exception as exc:
                job = {'status': 'unknown', 'error': runtime.safe_text(exc)}
            for card in result_cards({'job': job}):
                cards[card['card_id']] = card
            jobs.append(dict(saved, status=job.get('status', 'unknown') if job else 'unknown',
                             ok=job.get('ok') if job else None,
                             error=(job.get('error') or job.get('recovery_error')) if job else None))
        return {'ok': True, 'session': {'session_id': session_id, 'status': status,
            'messages': state['messages'][-60:], 'events': state['events'][-80:],
            'context': state['context'], 'pending_preview': state['pending_preview'], 'jobs': jobs,
            'result_cards': list(cards.values()),
            'owner_url': state.get('owner_url'),
            'token_budget': state.get('token_budget', 12000),
            'error': state.get('error'), 'active_request_id': state['active_request_id'],
            'costs': {'model_requests': requests, 'total_tokens': sum(known) if known or complete else None,
                      'token_accounting_status': 'complete' if complete else 'partial' if known else 'unknown'}}}

    def _live_owner(self, state):
        owner = state.get('owner_url')
        if not owner or owner == self.app_url:
            return False
        try:
            parsed = urlsplit(owner)
            if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path or
                    parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port is None):
                return False
            opener = build_opener(ProxyHandler({}))
            with opener.open(owner + '/api/config', timeout=1) as response:
                config = json.load(response)
            return config.get('conversation', {}).get('instance') == state.get('owner_instance')
        except (OSError, ValueError, AttributeError):
            return False

    def recover(self, session_id):
        from .mcp_jobs import JobStore
        with self.lock, runtime.project_lock(self.directory(session_id)):
            state = self._read(session_id)
            if state['status'] == 'working' and (state.get('owner_instance') == self.instance or self._live_owner(state)):
                return self.snapshot(session_id)
            preview = state.get('pending_preview')
            if preview and preview.get('execution_request_id') and not preview.get('job_id'):
                mapping = JobStore(self.settings.root).request_path(preview['execution_request_id'])
                if mapping.is_file():
                    job_id = runtime.read_json(mapping)['job_id']
                    job = self.job_lookup(job_id)
                    if job is not None:
                        preview['job_id'] = job_id
                        if not any(saved['job_id'] == job_id for saved in state['jobs']):
                            state['jobs'].append({'job_id': job_id, 'workflow_id': job.get('workflow_id'),
                                                  'request_id': preview['execution_request_id']})
            previous = state.get('active_request_id')
            if previous and state['turns'].get(previous, {}).get('status') == 'working':
                state['turns'][previous]['status'] = 'interrupted'
            state.update(status='ready', active_request_id=None, owner_instance=self.instance,
                         owner_url=self.app_url, error=None)
            self._save(state)
        for job in state['jobs']:
            self._start_watch(session_id, job['job_id'])
        return self.snapshot(session_id)

    def submit(self, session_id, value, *, internal=False):
        allowed = {'message', 'request_id', 'context_run_id', 'optimization_policy', 'action', 'preview_id',
                   'token_budget', 'auto_recover'}
        if internal:
            allowed.add('job_id')
        if not isinstance(value, dict) or set(value) - allowed:
            raise TaskValidationError('CHAT_FIELDS', '对话请求包含未知字段。')
        action = value.get('action', 'message')
        if action not in (('message', 'execute', 'job_result') if internal else ('message', 'execute')):
            raise TaskValidationError('CHAT_ACTION', '对话操作无效。')
        token_budget = value.get('token_budget')
        if token_budget is not None and (type(token_budget) is not int or not 1000 <= token_budget <= 100000):
            raise TaskValidationError('CHAT_TOKEN_BUDGET', '每条消息的token预算应为1000至100000的整数。')
        request_id = value.get('request_id')
        if not isinstance(request_id, str) or REQUEST_ID.fullmatch(request_id) is None:
            raise TaskValidationError('CHAT_REQUEST_ID', '请提供稳定的消息请求编号。')
        message = value.get('message', '')
        if not isinstance(message, str) or len(message) > 4000 or '\0' in message or action == 'message' and not message.strip():
            raise TaskValidationError('CHAT_MESSAGE', '请输入1至4000字的消息。')
        context = {}
        if 'auto_recover' in value:
            if type(value['auto_recover']) is not bool:
                raise TaskValidationError('CHAT_RECOVERY', '自动恢复设置应为布尔值。')
            context['auto_recover'] = value['auto_recover']
        if 'context_run_id' in value:
            context['run_id'] = value['context_run_id']
        if value.get('context_run_id') is not None:
            from .schemas import TaskSpec
            from .validation import resolve_run
            TaskSpec.from_dict({'operation': 'status', 'run_id': value['context_run_id']})
            resolve_run(self.settings, value['context_run_id'])
            context['run_id'] = value['context_run_id']
        if 'optimization_policy' in value:
            from python_control.objectives import validate_policy
            context['optimization_policy'] = (validate_policy(value['optimization_policy'])
                                               if value['optimization_policy'] is not None else None)
        if not self.python.is_file():
            raise TaskValidationError('CHAT_ENVIRONMENT', '请先安装项目的.venv-mcp环境。')
        folder = self.directory(session_id)
        digest = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.lock, runtime.project_lock(folder):
            state = self._read(session_id)
            previous = state['turns'].get(request_id)
            if previous is not None:
                if previous['digest'] != digest:
                    raise TaskValidationError('CHAT_REQUEST_CONFLICT', '该消息编号已经用于另一条请求。')
                return self.snapshot(session_id)
            if state['status'] == 'working' and (state.get('owner_instance') == self.instance or self._live_owner(state)):
                raise TaskValidationError('CHAT_BUSY', '这条对话正在处理上一条消息。')
            if len(state['turns']) >= 200:
                raise TaskValidationError('CHAT_LIMIT', '当前对话已达到200条消息，请新建对话。')
            if token_budget is not None:
                state['token_budget'] = token_budget
            history = [{'role': item['role'], 'content': item['text'][:2000]} for item in state['messages'][-4:]]
            previous_preview = state.get('pending_preview')
            execute_from_message = action == 'message' and EXECUTE_PREVIEW.fullmatch(message.strip()) is not None
            if execute_from_message:
                action = 'execute'
            if action == 'execute':
                preview = state.get('pending_preview')
                if not preview or not execute_from_message and value.get('preview_id') != preview['preview_id']:
                    raise TaskValidationError('CHAT_PREVIEW', '请执行当前已保存的预览。')
                if preview.get('job_id'):
                    return self.snapshot(session_id)
                selection = preview.get('selection_context', state['context'])
                if any(context[key] != selection.get(key) for key in context):
                    raise TaskValidationError('CHAT_PREVIEW_CONTEXT', '运行或任务设置已改变，请重新生成任务预览。')
                # Persist a stable execution request before the subprocess can submit.
                preview.setdefault('execution_request_id', 'chat_exec_' + uuid.uuid4().hex)
                message = '执行已保存预览 ' + preview['preview_id']
            elif action == 'job_result':
                if not any(job['job_id'] == value['job_id'] for job in state['jobs']):
                    raise TaskValidationError('CHAT_JOB', '该工程任务没有绑定到当前对话。')
                message = '读取工程任务 ' + value['job_id'] + ' 的完成结果'
            else:
                # A new request invalidates an earlier executable preview immediately.
                state['pending_preview'] = None
                state['context'].update(context)
            state['messages'].append({'role': 'user', 'text': message, 'request_id': request_id})
            state.update(status='working', active_request_id=request_id, owner_instance=self.instance,
                         owner_url=self.app_url, error=None)
            state['turns'][request_id] = {'digest': digest, 'action': action, 'status': 'working',
                                          'model_requests': 0, 'usage': []}
            turn_folder = folder / 'turns' / request_id
            turn_folder.mkdir(parents=True, exist_ok=False)
            request = {'app_url': self.app_url, 'settings': {'api_config': str(self.settings.api_config),
                'key_environment_variable': self.settings.key_environment_variable},
                'log_dir': str(turn_folder / 'model'), 'action': action, 'message': message,
                'context': dict(state['context'], previous_preview=({key: previous_preview[key] for key in (
                    'preview_id', 'tool', 'arguments', 'budget') if key in previous_preview} if previous_preview else None)),
                'history': history, 'token_budget': state.get('token_budget', 12000)}
            if action == 'message':
                from .chat_worker import rule_plan
                planned = rule_plan(message, self.settings, state['context'])
                if planned is not None:
                    request['rule_plan'] = planned
            if action == 'execute':
                request.update(approved_preview=state['pending_preview'],
                               execution_request_id=state['pending_preview']['execution_request_id'])
            if action == 'job_result':
                request['job_id'] = value['job_id']
            runtime.write_json(turn_folder / 'request.json', request)
            self._save(state)
        try:
            threading.Thread(target=self._work, args=(session_id, request_id, request, turn_folder),
                             name='disk-chat-' + request_id, daemon=True).start()
        except Exception as exc:
            self._event(session_id, request_id, {'type': 'error', 'message': runtime.safe_text(exc)})
            self._finish(session_id, request_id)
            raise
        return self.snapshot(session_id)

    def _event(self, session_id, request_id, event):
        with self.lock, runtime.project_lock(self.directory(session_id)):
            state = self._read(session_id)
            turn = state['turns'][request_id]
            event = dict(event, request_id=request_id, time=runtime.local_time())
            # Full events are also written once each in the turn directory.
            count = turn.get('event_count', 0) + 1
            turn['event_count'] = count
            runtime.write_json(self.directory(session_id) / 'turns' / request_id / ('event_%03d.json' % count), event)
            displayed = dict(event)
            if len(json.dumps(event, ensure_ascii=False)) > 24000:
                displayed['result'] = {'detail': '完整返回已保存在本条消息的工具记录中。',
                                       'excerpt': json.dumps(event.get('result'), ensure_ascii=False)[:16000]}
            state['events'] = (state['events'] + [displayed])[-100:]
            kind = event['type']
            if kind == 'model_started':
                turn['model_requests'] += 1
            elif kind == 'model_result':
                turn['usage'].append(event.get('usage'))
            elif kind == 'preview':
                preview = dict(event['preview'])
                preview['preview_id'] += '_' + hashlib.sha256(request_id.encode()).hexdigest()[:8]
                preview['selection_context'] = dict(state['context'])
                state['pending_preview'] = preview
            elif kind == 'answer':
                state['messages'].append({'role': 'assistant', 'text': event['text'], 'request_id': request_id})
            elif kind == 'job_submitted':
                if not any(job['job_id'] == event['job_id'] for job in state['jobs']):
                    state['jobs'].append({key: event[key] for key in ('job_id', 'workflow_id', 'request_id')})
                if state.get('pending_preview'):
                    state['pending_preview']['job_id'] = event['job_id']
            elif kind == 'tool_result':
                cards = {card['card_id']: card for card in state.get('result_cards', [])}
                for card in result_cards(event.get('result')):
                    cards[card['card_id']] = card
                state['result_cards'] = list(cards.values())[-20:]
            elif kind == 'error':
                state['error'] = event['message']
                turn['error'] = event['message']
            self._save(state)

    def _finish(self, session_id, request_id):
        with self.lock, runtime.project_lock(self.directory(session_id)):
            state = self._read(session_id)
            turn = state['turns'][request_id]
            turn['status'] = 'failed' if turn.get('error') else 'completed'
            state.update(status='failed' if turn.get('error') else 'ready', active_request_id=None)
            self._save(state)

    def _work(self, session_id, request_id, request, turn_folder):
        jobs = []
        done = False
        process = None
        try:
            with (turn_folder / 'worker.stderr.log').open('w', encoding='utf-8') as errors:
                process = subprocess.Popen([str(self.python), '-X', 'utf8', '-B',
                    str(Path(__file__).with_name('chat_worker.py'))], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8',
                    cwd=str(self.settings.root), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                process.stdin.write(json.dumps(request, ensure_ascii=False, allow_nan=False) + '\n')
                process.stdin.close()
                for line in process.stdout:
                    if len(line) > 2 * 1024 * 1024:
                        raise ValueError('对话工具返回超过大小限制。')
                    event = runtime.bridge.parse_json(line)
                    self._event(session_id, request_id, event)
                    if event['type'] == 'job_submitted':
                        jobs.append(event['job_id'])
                    done = done or event['type'] == 'done'
                process.stdout.close()
                code = process.wait(timeout=15)
                if code or not done:
                    state = self._read(session_id)
                    if not state['turns'][request_id].get('error'):
                        raise ValueError('对话进程提前结束，已取得的工具结果可在调用记录中查看。')
        except BaseException as exc:
            self._event(session_id, request_id, {'type': 'error', 'message': runtime.safe_text(exc)})
        finally:
            try:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            finally:
                self._finish(session_id, request_id)
        for job_id in jobs:
            self._start_watch(session_id, job_id)

    def _start_watch(self, session_id, job_id):
        with self.lock:
            key = session_id, job_id
            if key in self.monitored:
                return
            self.monitored.add(key)
        threading.Thread(target=self._watch_job, args=(session_id, job_id),
                         name='disk-chat-result-' + job_id, daemon=True).start()

    def _watch_job(self, session_id, job_id):
        try:
            while not self.stop.is_set():
                try:
                    job = self.job_lookup(job_id)
                    if not job:
                        return
                    if job.get('status') == 'completed':
                        self.submit(session_id, {'action': 'job_result', 'request_id': 'result_' + job_id,
                                                 'job_id': job_id}, internal=True)
                        return
                except (OSError, ValueError, runtime.ControllerError, TaskValidationError):
                    pass
                self.stop.wait(2)
        finally:
            with self.lock:
                self.monitored.discard((session_id, job_id))
