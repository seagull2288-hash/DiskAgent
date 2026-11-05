"""Add bounded measured feedback through the existing search client extension.

No engine files are replaced or monkeypatched. Existing plans/centers remain
authoritative; a cached legacy decision is never relabeled as feedback-guided.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from python_control import runtime
from python_control.sampling import signature
from python_control.search import SearchProposer, parse_center
from .feedback import build_feedback, feedback_prompt_context, validate_feedback
from .result_analysis import analyze_run
from .schemas import TaskSpec, TaskValidationError
from .state import WorkflowStore
from .validation import read_checkpoint


POLICY_SCHEMA = 'disk-agent.feedback-policy.v1'
ADAPTER_VERSION = 'disk-agent.feedback-adapter.v1'
REF_SCHEMA = 'disk-agent.feedback-ref.v1'
DECISION_SCHEMA = 'disk-agent.search-decision.v1'


def _code_signature():
    root=Path(__file__).parent
    return signature({name:hashlib.sha256((root/name).read_bytes()).hexdigest()
                      for name in ('feedback.py','feedback_search.py','result_analysis.py')})


def default_feedback_policy(*,enabled=True):
    if type(enabled) is not bool:
        raise TaskValidationError('FEEDBACK_POLICY','enabled must be a boolean.')
    return {'schema_version':POLICY_SCHEMA,'enabled':enabled,
            'feedback_schema':'disk-agent.batch-feedback.v1','max_recent':3,
            'implementation_version':ADAPTER_VERSION,'code_signature':_code_signature()}


def validate_policy(value):
    if not isinstance(value,dict) or set(value)!=set(default_feedback_policy()):
        raise TaskValidationError('FEEDBACK_POLICY','Unsupported feedback policy fields.')
    expected=default_feedback_policy(enabled=value.get('enabled'))
    if value!=expected:
        raise TaskValidationError('FEEDBACK_VERSION','Feedback implementation changed; create a reviewed new workflow.')
    return dict(value)


def _inside(path,root):
    path=Path(path).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise TaskValidationError('FEEDBACK_PATH','Feedback/decision path escapes its allowed directory.')
    return path


def _json_file(path,root):
    return runtime.read_json(_inside(path,root))


def _save(path,value,root):
    runtime.write_json(_inside(path,root),value)


class FeedbackSearchProposer:
    def __init__(self,config,*,settings,context,on_decision=None,client_factory=None):
        if not isinstance(context,dict) or set(context)!={'workflow_id','policy','target_total_candidates'}:
            raise TaskValidationError('FEEDBACK_CONTEXT','Invalid internal feedback context.')
        if on_decision is not None and not callable(on_decision):
            raise TaskValidationError('FEEDBACK_OBSERVER','on_decision must be callable.')
        self.config,self.settings=dict(config),settings
        self.context=dict(context)
        self.policy=validate_policy(context['policy'])
        if not self.policy['enabled'] or config['strategy']=='direct':
            raise TaskValidationError('FEEDBACK_POLICY','Batch feedback supports enabled center-search workflows only.')
        self.store=WorkflowStore(settings)
        self.folder=self.store.directory(context['workflow_id'])
        self.on_decision,self.client_factory=on_decision,client_factory
        self._audited=set()
        self._state()

    def _state(self):
        state=self.store.load(self.context['workflow_id'])
        if state.feedback_policy!=self.policy or state.target_total_candidates!=self.context['target_total_candidates']:
            raise TaskValidationError('FEEDBACK_CONTEXT','Workflow policy/budget differs from the internal context.')
        validate_policy(state.feedback_policy)
        return state

    def validate_binding(self,run_id):
        state=self._state()
        if state.run_id!=run_id:
            raise TaskValidationError('FEEDBACK_RUN','Feedback context is bound to another optimizer run.')
        if (state.pending_batch is None or
                state.pending_batch.get('target_total_candidates')!=self.settings.candidate_count):
            raise TaskValidationError('FEEDBACK_BUDGET','Feedback context has no matching scheduled child target.')
        return state

    def _source(self,run,generation,basis_history):
        state=self._state()
        if state.run_id!=run.name:
            raise TaskValidationError('FEEDBACK_RUN','Feedback must belong to the bound workflow run.')
        saved=read_checkpoint(run)
        if saved['history']!=basis_history or saved['nCompleted']!=len(basis_history)-1:
            raise TaskValidationError('FEEDBACK_STALE','Measured history changed or an uncached partial batch cannot be replanned.')
        if saved['nLLM']!=self.settings.candidate_count or saved['nLLM']>state.target_total_candidates:
            raise TaskValidationError('FEEDBACK_BUDGET','Engine target differs from the scheduled workflow budget.')
        if state.pending_batch is None or state.pending_batch['target_total_candidates']!=saved['nLLM']:
            raise TaskValidationError('FEEDBACK_BUDGET','Feedback has no matching scheduled workflow batch.')
        completed=saved['nCompleted']
        initial,size=self.config['initial_lhs'],self.config['batch_size']
        since=max(0,initial+(generation-2)*size) if generation>1 else 0
        result=analyze_run(run.name,settings=self.settings,since_candidate=since,verify_native=True)
        if not result.ok:
            raise TaskValidationError('FEEDBACK_EVIDENCE',result.error['message'])
        if result.data['completed_candidates']!=completed:
            raise TaskValidationError('FEEDBACK_STALE','Analysis does not match the search-center history.')
        feedback=build_feedback(result.data,workflow_id=state.workflow_id,generation=generation,
                                history_signature=signature(basis_history),
                                target_total_candidates=state.target_total_candidates)
        return feedback,result.to_dict()

    def _feedback_path(self,folder,generation,feedback_id):
        return _inside(folder/'feedback'/('gen_%03d_%s.json'%(generation,feedback_id)),folder)

    def _load_ref(self,attempt,run,generation,basis_history):
        if attempt is None:
            return None,None
        allowed=run/'search'/('generation_%03d'%generation)/'center_api'
        attempt=_inside(attempt,allowed)
        marker=attempt/'feedback_ref.json'
        if not marker.exists():
            prompt_file=attempt/'prompt.json'
            if prompt_file.exists():
                try:
                    old_prompt=_json_file(prompt_file,allowed)
                    old_context=runtime.bridge.parse_json(old_prompt['user'])
                except (ValueError,KeyError,TypeError):
                    old_context={}
                if isinstance(old_context,dict) and 'batch_feedback' in old_context:
                    raise TaskValidationError('FEEDBACK_PROVENANCE','Augmented cached prompt lost its feedback reference.')
            return None,None
        ref=_json_file(marker,allowed)
        expected={'schema_version','workflow_id','run_id','generation','source_history_signature',
                  'source_completed_candidates','feedback_id','feedback_file','feedback_sha256','prompt_signature'}
        if (not isinstance(ref,dict) or set(ref)!=expected or ref['schema_version']!=REF_SCHEMA or
                ref['run_id']!=run.name or type(ref['generation']) is not int or ref['generation']!=generation or
                ref['source_history_signature']!=signature(basis_history) or
                type(ref['source_completed_candidates']) is not int or
                ref['source_completed_candidates']!=len(basis_history)-1):
            raise TaskValidationError('FEEDBACK_PROVENANCE','Cached feedback reference belongs to another run, batch or history.')
        if (not isinstance(ref['feedback_id'],str) or re.fullmatch(r'fdbk_[a-f0-9]{24}',ref['feedback_id']) is None or
                any(not isinstance(ref[key],str) or re.fullmatch(r'[a-f0-9]{64}',ref[key]) is None
                    for key in ('feedback_sha256','prompt_signature'))):
            raise TaskValidationError('FEEDBACK_PROVENANCE','Cached feedback identifiers/digests are invalid.')
        source_folder=self.store.directory(ref['workflow_id'])
        # Validate the ID through the feedback contract before constructing a path.
        feedback_path=_inside(ref['feedback_file'],source_folder/'feedback')
        if feedback_path!=self._feedback_path(source_folder,generation,ref['feedback_id']):
            raise TaskValidationError('FEEDBACK_PATH','Cached feedback has an unexpected artifact path.')
        if hashlib.sha256(feedback_path.read_bytes()).hexdigest()!=ref['feedback_sha256']:
            raise TaskValidationError('FEEDBACK_PROVENANCE','Cached feedback file no longer matches its committed digest.')
        feedback=validate_feedback(runtime.read_json(feedback_path))
        if (feedback['feedback_id']!=ref['feedback_id'] or feedback['run_id']!=run.name or
                feedback['workflow_id']!=ref['workflow_id'] or feedback['generation']!=generation or
                feedback['source_history_signature']!=signature(basis_history) or
                feedback['source_completed_candidates']!=len(basis_history)-1):
            raise TaskValidationError('FEEDBACK_PROVENANCE','Cached feedback contents disagree with the center reference.')
        prompt=_json_file(attempt/'prompt.json',allowed)
        if (signature(prompt)!=ref['prompt_signature'] or
                runtime.bridge.parse_json(prompt['user']).get('batch_feedback')!=feedback_prompt_context(feedback)):
            raise TaskValidationError('FEEDBACK_PROVENANCE','Cached prompt does not contain its claimed feedback.')
        return feedback,ref

    def __call__(self,api,problem,history,output,*,live):
        if live is not True:
            raise TaskValidationError('FEEDBACK_LIVE','Search feedback does not authorize execution.')
        output=Path(output).resolve()
        if output.parent.name!='api' or output.parents[2].name!='iterations':
            raise TaskValidationError('FEEDBACK_PATH','Unexpected controller proposal output path.')
        run=output.parents[3]
        TaskSpec.from_dict({'operation':'status','run_id':run.name})
        if run.parent!=self.settings.output_root.resolve():
            raise TaskValidationError('FEEDBACK_PATH','Proposal run is outside the configured output root.')
        self.validate_binding(run.name)
        completed=len(history)-1
        initial,size=self.config['initial_lhs'],self.config['batch_size']
        warmup=completed<initial
        generation=0 if warmup else (completed-initial)//size+1
        start=0 if warmup else initial+(generation-1)*size
        basis_history=history[:start+1]
        local={'feedback':None,'analysis':None,'sent':False}
        owner=self

        class Client:
            def __init__(self,cfg,*,live):
                self.inner=(owner.client_factory or runtime.bridge.Client)(cfg,live=live)

            @property
            def http_attempts(self):
                return self.inner.http_attempts

            def generate(self,prompt,log_dir,schema=None):
                state=owner._state()
                if read_checkpoint(run)['history']!=basis_history:
                    raise TaskValidationError('FEEDBACK_STALE','Search history changed before the provider request.')
                if local['feedback'] is None:
                    local['feedback'],local['analysis']=owner._source(run,generation,basis_history)
                feedback=local['feedback']
                context=runtime.bridge.parse_json(prompt['user'])
                if (not isinstance(context,dict) or 'batch_feedback' in context or
                        context.get('search_state',{}).get('generation')!=generation or
                        context.get('search_state',{}).get('measured_designs')!=len(basis_history)):
                    raise TaskValidationError('FEEDBACK_PROMPT','Base prompt does not match this search batch.')
                context['batch_feedback']=feedback_prompt_context(feedback)
                enriched={'system':prompt['system']+' Use batch_feedback as measured evidence for this center decision. '
                          'Keep all existing output keys and bounds. Unknown constraints remain unassessed. '
                          'Give a concise search hypothesis, not a hotspot or causal claim; do not extend any budget.',
                          'user':json.dumps(context,ensure_ascii=False,allow_nan=False)}
                log_dir=_inside(log_dir,run/'search'/('generation_%03d'%generation)/'center_api')
                feedback_file=owner._feedback_path(owner.folder,generation,feedback['feedback_id'])
                if feedback_file.exists() and runtime.read_json(feedback_file)!=feedback:
                    raise TaskValidationError('FEEDBACK_PROVENANCE','Existing feedback artifact differs from this input.')
                _save(feedback_file,feedback,owner.folder)
                _save(owner.folder/'feedback'/('evidence_'+feedback['feedback_id']+'.json'),local['analysis'],owner.folder)
                # The original request_center wrote a base prompt. Replace only this
                # new attempt's log with the exact object sent through its client.
                _save(log_dir/'prompt.json',enriched,run)
                ref={'schema_version':REF_SCHEMA,'workflow_id':state.workflow_id,'run_id':run.name,
                     'generation':generation,'source_history_signature':signature(basis_history),
                     'source_completed_candidates':completed,'feedback_id':feedback['feedback_id'],
                     'feedback_file':str(feedback_file),
                     'feedback_sha256':hashlib.sha256(feedback_file.read_bytes()).hexdigest(),
                     'prompt_signature':signature(enriched)}
                _save(log_dir/'feedback_ref.json',ref,run)
                local['sent']=True
                return self.inner.generate(enriched,log_dir,schema=schema)

        # The original engine may consume api_output/candidate.json directly on
        # restart. Keep that path absent until provenance AND workflow state have
        # committed, including if this process dies between any two writes.
        # Keep the private staging name short: the bridge adds UUID temporary
        # suffixes and Windows may still enforce the legacy MAX_PATH limit.
        staging=_inside(output.parent/('request_s'+signature(str(output))[:16]),run)
        candidate=SearchProposer(self.config,client_factory=Client)(api,problem,history,staging,live=live)
        self.audit_candidate(candidate,problem,history,run,request_sent=local['sent'])
        output.mkdir(parents=True,exist_ok=False)
        _save(output/'selection.json',candidate,run)
        _save(output/'candidate.json',candidate,run)  # Publish last, after audit.
        return candidate

    def ensure_candidate(self,candidate,problem,history,run):
        self.validate_binding(Path(run).name)
        if signature(candidate) not in self._audited:
            self.audit_candidate(candidate,problem,history,Path(run))

    def audit_candidate(self,candidate,problem,history,run,*,request_sent=False):
        self.validate_binding(run.name)
        if candidate.get('history_signature')!=signature(history):
            raise TaskValidationError('FEEDBACK_STALE','Cached candidate no longer matches committed history.')
        generation=candidate['generation']
        start=candidate['basis_history_length']-1
        basis_history=history[:start+1]
        warmup=candidate['source']=='initial_design'
        plan_file=_inside(candidate['planFile'],run/'search')
        plan=runtime.read_json(plan_file)
        if signature(plan)!=candidate['planSignature'] or plan['history_signature']!=signature(basis_history):
            raise TaskValidationError('FEEDBACK_PROVENANCE','Decision plan no longer matches its measured basis.')
        feedback,ref=self._load_ref(plan.get('apiAttemptDirectory'),run,generation,basis_history) if not warmup else (None,None)
        if request_sent and feedback is None:
            raise TaskValidationError('FEEDBACK_PROVENANCE','New center request lost its committed feedback reference.')
        center=plan.get('center')
        if center is not None:
            checked=parse_center(json.dumps({key:center[key] for key in ('center','rationale','candidate_type')}),problem)
            if checked['x']!=center['x']:
                raise TaskValidationError('FEEDBACK_PROVENANCE','Decision center differs from the parsed plan.')
        identity={'workflow_id':self.context['workflow_id'],'run_id':run.name,'generation':generation,
                  'plan_signature':candidate['planSignature']}
        decision_id='dec_'+signature(identity)[:24]
        decision_file=_inside(self.folder/'decisions'/(decision_id+'.json'),self.folder)
        old=runtime.read_json(decision_file) if decision_file.exists() else None
        trace={'schema_version':DECISION_SCHEMA,'decision_id':decision_id,**identity,
               'feedback_id':feedback['feedback_id'] if feedback else None,
               'feedback_summary':feedback['program_advice'] if feedback else
                    ('初始采样不调用中心模型。' if warmup else '复用已有批次；没有可核验的结构化反馈注入记录。'),
               'feedback_source_completed_candidates':len(basis_history)-1,
               'feedback_source_workflow_id':ref['workflow_id'] if ref else None,
               'source_kind':'initial_lhs' if warmup else 'feedback_augmented' if feedback else 'cached_legacy',
               'center':dict(center['center']) if center else None,
               'candidate_type':center['candidate_type'] if center else 'explore',
               'rationale':center['rationale'] if center else '程序生成初始LHS；未调用LLM。',
               'sigma_fraction':plan.get('sigma_fraction'),
               'basis_completed_candidates':start,'selected_candidates':len(plan['selected']),
               'evaluation_target_total_candidates':min(start+len(plan['selected']),self.context['target_total_candidates']),
               'plan_file':str(plan_file),'feedback_file':ref['feedback_file'] if ref else None,
               'api_attempt_directory':plan.get('apiAttemptDirectory'),
               'recorded_at':old.get('recorded_at') if isinstance(old,dict) else runtime.local_time(),
               'request_status':'new_request' if ref and ref['workflow_id']==self.context['workflow_id'] else 'reused_plan',
               'cache_reused':not request_sent,
               'outcome':old.get('outcome') if isinstance(old,dict) else
                    {'status':'pending','completed_candidates':0,'candidate_iterations':[],
                     'best_stress_mpa':None,'best_improvement_mpa':None,
                     'stress_reduction_percent':None,'area_change_percent':None}}
        _save(decision_file,trace,self.folder)
        if self.on_decision is not None:
            self.on_decision(trace)
        self._audited.add(signature(candidate))
        return candidate


def update_decision_outcomes(traces,analysis):
    """Attach committed outcomes to decisions, keeping hypotheses and facts separate."""
    result=[]
    records=([analysis['baseline']] if analysis.get('baseline') else [])+analysis.get('candidates',[])
    done=analysis['completed_candidates']
    for trace in traces:
        item=dict(trace)
        start=trace['basis_completed_candidates']
        end=min(start+trace['selected_candidates'],trace.get('evaluation_target_total_candidates',start+trace['selected_candidates']))
        window=[p for p in records if start<p['iteration']<=min(end,done)]
        before=[p for p in records if p['iteration']<=start]
        best=min(before+window,key=lambda p:p['stress_mpa']) if before else None
        previous=min(before,key=lambda p:p['stress_mpa']) if before else None
        item['outcome']={'status':'completed' if done>=end else 'partial' if window else 'pending',
                         'completed_candidates':len(window),'candidate_iterations':[p['iteration'] for p in window],
                         'best_stress_mpa':best['stress_mpa'] if best else None,
                         'best_improvement_mpa':previous['stress_mpa']-best['stress_mpa'] if best else None,
                         'stress_reduction_percent':best['stress_reduction_from_baseline_percent'] if best else None,
                         'area_change_percent':best['approximate_area_change_from_baseline_percent'] if best else None}
        result.append(item)
    return result


def render_decision_report(traces):
    if not traces:
        return ''
    lines=['','## 批次反馈与搜索决策','',
           '以下将已测反馈、模型搜索假设与后续实测结果分开。反馈沿用原中心请求，无额外反思模型调用。','']
    for item in traces:
        outcome=item['outcome']
        lines += ['### 第'+str(item['generation'])+'批','',
                  '- 反馈来源：`'+item['source_kind']+'`；反馈ID：`'+str(item['feedback_id'])+'`。',
                  '- 来源已提交候选数：'+str(item['feedback_source_completed_candidates'])+'。',
                  '- 实测摘要：'+item['feedback_summary'],
                  '- 搜索假设（未经因果验证）：'+item['rationale'].replace('<','&lt;').replace('>','&gt;'),
                  '- 程序控制采样尺度：'+str(item['sigma_fraction'])+'。',
                  '- 后续实测状态：`'+outcome['status']+'`；已提交候选数：'+str(outcome['completed_candidates'])+'。',
                  '- 后续历史最优应力：'+str(outcome['best_stress_mpa'])+' MPa。',
                  '- 决策计划：`'+item['plan_file']+'`。','']
    return '\n'.join(lines)
