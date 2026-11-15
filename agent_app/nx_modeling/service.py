"""Application service shared by the workbench and MCP adapter."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid

from .nx12 import compile_journal, discover, run_journal
from .schema import DiskPlan, ModelingPlanError


class NXModelingService:
    def __init__(self, root, nx_home=None):
        self.root = Path(root)
        self.nx_home = nx_home

    def status(self):
        return {'ok': True, 'adapter': 'siemens-nx12-journal-v1', **discover(self.nx_home),
                'templates': ['disk'], 'live_default': False}

    def preview(self, raw_plan):
        plan = DiskPlan.parse(raw_plan)
        signature = hashlib.sha256(json.dumps(plan.to_dict(), sort_keys=True).encode()).hexdigest()
        return {'ok': True, 'status': 'validated', 'plan': plan.to_dict(), 'plan_sha256': signature,
                'adapter': 'siemens-nx12-journal-v1', 'live': False,
                'summary_zh': f'将创建直径 {plan.outer_diameter:g} mm、厚 {plan.thickness:g} mm 的参数化圆盘。'}

    def execute(self, raw_plan, request_id):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ModelingPlanError('live执行需要稳定的 request_id（最长128字符）。')
        preview = self.preview(raw_plan)
        plan = DiskPlan.parse(raw_plan)
        job_id = 'nx_' + uuid.uuid4().hex[:24]
        folder = self.root / 'nx_modeling_jobs' / job_id
        folder.mkdir(parents=True, exist_ok=False)
        output = folder / (plan.name + '.prt')
        journal = folder / 'model.py'
        journal.write_text(compile_journal(plan, output), encoding='utf-8')
        record = {'job_id': job_id, 'request_id': request_id, 'created_at': datetime.now().astimezone().isoformat(),
                  'plan': plan.to_dict(), 'plan_sha256': preview['plan_sha256'], 'journal': str(journal),
                  'output': str(output), 'status': 'running'}
        (folder / 'request.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        result = run_journal(journal, self.nx_home)
        record.update(result=result, status='completed' if result['returncode'] == 0 and output.is_file() else 'failed',
                      output_exists=output.is_file())
        (folder / 'result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'ok': record['status'] == 'completed', **record}
