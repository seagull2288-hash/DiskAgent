"""Standard-library loopback client; MCP dependency stays out of the optimizer env."""
from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request


class BridgeError(ValueError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise BridgeError('Workbench redirects are not accepted.')


class WorkbenchClient:
    def __init__(self, app_url='http://127.0.0.1:8768', timeout=20):
        parsed = urlsplit(app_url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username is not None
                or parsed.password is not None or parsed.path not in ('', '/') or parsed.query
                or parsed.fragment or parsed.port is None or not 1 <= parsed.port <= 65535):
            raise BridgeError('app_url must be http://127.0.0.1:<port> with no credentials or extra path.')
        self.url = f'http://127.0.0.1:{parsed.port}'
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _request(self, path, value=None):
        headers = {'Accept': 'application/json'}
        body = None
        if value is not None:
            config = self.config()
            if config.get('mcp_bridge', {}).get('version') != 'disk-agent.mcp.v1':
                raise BridgeError('Workbench needs the MCP update. Restart the idle app first.')
            headers.update({'Content-Type': 'application/json', 'X-CSRF-Token': config['csrf_token']})
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        try:
            with self.opener.open(Request(self.url + path, data=body, headers=headers), timeout=self.timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise BridgeError('Workbench response exceeds 8 MB.')
                result = json.loads(raw)
        except HTTPError as exc:
            try:
                result = json.loads(exc.read(65536))
            except (ValueError, OSError):
                raise BridgeError(f'Workbench returned HTTP {exc.code}.') from None
        except (URLError, TimeoutError, OSError):
            # Never retry writes: server may already have accepted the request.
            raise BridgeError('Workbench is unreachable or the request timed out. Check the app and query the same request_id; do not create a new solve automatically.') from None
        if not isinstance(result, dict):
            raise BridgeError('Workbench returned an invalid result.')
        return result

    def config(self):
        value = self._request('/api/config')
        if value.get('app_id') != 'disk-agent.workbench.v1':
            raise BridgeError('The endpoint is not a DiskAgent workbench.')
        return value

    @staticmethod
    def identifier(value, kind):
        patterns = {'run': r'run_[A-Za-z0-9][A-Za-z0-9_-]{0,180}',
                    'workflow': r'wf_[A-Za-z0-9][A-Za-z0-9_-]{0,180}',
                    'job': r'job_[a-f0-9]{24}'}
        if not isinstance(value, str) or re.fullmatch(patterns[kind], value) is None:
            raise BridgeError('Invalid ' + kind + ' ID; paths and URLs are not accepted.')
        return value

    def read_case(self):
        return self._request('/api/mcp/case')

    def mesh_study(self):
        return self._request('/api/mesh-study')

    def records(self, kind, limit=20):
        if kind not in ('runs', 'workflows') or type(limit) is not int or not 1 <= limit <= 100:
            raise BridgeError('Choose runs/workflows and a limit from 1 to 100.')
        result = self._request('/api/' + kind)
        if result.get('ok'):
            result['returned_count'] = min(limit, len(result[kind]))
            result['available_count'] = len(result[kind])
            result[kind] = result[kind][:limit]
        return result

    def action(self, name, value):
        if name not in ('evaluate-design', 'submit-optimization', 'resume-workflow'):
            raise BridgeError('Unknown action.')
        return self._request('/api/mcp/' + name, value)

    def job(self, job_id):
        return self._request('/api/jobs/' + self.identifier(job_id, 'job'))

    def workflow(self, workflow_id):
        return self._request('/api/workflows/' + self.identifier(workflow_id, 'workflow'))

    def run(self, run_id, view='status'):
        if view not in ('status', 'analysis', 'search-diagnostics'):
            raise BridgeError('Unknown run view.')
        return self._request('/api/runs/' + self.identifier(run_id, 'run') + '/' + view)


def compact_report(result):
    """Keep conclusions, recommended dimensions, constraints and costs; omit long traces."""
    if not result.get('ok'):
        return result
    result = dict(result)
    if isinstance(result.get('job'), dict):
        result['job'] = dict(result['job'])
        if isinstance(result['job'].get('result'), dict):
            result['job']['result'] = compact_report(result['job']['result'])
    data = dict(result.get('data', {}))
    for name in ('samples', 'candidates', 'observations', 'history', 'records', 'convergence_curve'):
        if isinstance(data.get(name), list):
            data[name + '_count'] = len(data.pop(name))
    if isinstance(data.get('optimization'), dict):
        data['optimization'] = dict(data['optimization'])
        for name in ('observations', 'evaluations'):
            data['optimization'].pop(name, None)
    if isinstance(data.get('analysis'), dict):
        data['analysis'] = compact_report({'ok': True, 'data': data['analysis']})['data']
    if isinstance(data.get('state'), dict):
        data['state'] = {key: value for key, value in data['state'].items() if key in (
            'workflow_id', 'run_id', 'status', 'task', 'start_completed_candidates', 'target_total_candidates', 'error')}
    if isinstance(data.get('design_selection'), dict):
        data['design_selection'] = dict(data['design_selection'])
        pareto = data['design_selection'].get('pareto', [])
        data['design_selection']['pareto_count'] = len(pareto)
        data['design_selection']['pareto'] = pareto[:10]
    if isinstance(data.get('feedback'), dict):
        data['feedback'] = dict(data['feedback'])
        if 'optimization' in data['feedback']:
            data['feedback']['optimization'] = data.get('optimization')
    if isinstance(data.get('evidence'), dict):
        data['evidence'] = dict(data['evidence'])
        if isinstance(data['evidence'].get('records'), list):
            data['evidence']['records_count'] = len(data['evidence'].pop('records'))
    result['data'] = data
    result['detail_hint'] = 'Use the browser workbench for full curves and raw evidence.'
    return result
