"""V2 source reporting stays separate from model hypotheses and planned quotas."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from agent_app import feedback_search, result_analysis
from agent_app import supervisor
from agent_app.schemas import TaskValidationError
from agent_app.tests import test_feedback_integration as feedback_fixture
from agent_app.tests.test_contract import FixtureCase
from python_control import controller, runtime


def selection_plan():
    return {'selection_policy': 'hybrid_local_v2',
            'pool_origins': ['llm_center', 'best_local', 'best_local', 'global_uniform'],
            'selected_pool_indices': [0, 1, 2, 3], 'selected': [[i] * 9 for i in range(4)],
            'selection_summary': {'policy_version': 'hybrid_local_v2',
                'requested_quotas': {'llm_center': 1, 'best_local': 2, 'global_uniform': 1},
                'planned_origin_counts': {'llm_center': 1, 'best_local': 2, 'global_uniform': 1},
                'local_radius_fraction': .08, 'local_radius_metric': 'normalized_Linf',
                'adaptation_action': 'hold', 'surrogate_used': False,
                'surrogate_reason': 'insufficient_measured_samples'}}


def candidate_metadata(origin):
    roles = {'llm_center': 'llm_proposal', 'best_local': 'local_refinement', 'global_uniform': 'global_exploration'}
    return {'selection_policy': 'hybrid_local_v2', 'selection_origin': origin,
            'selection_role': roles[origin], 'local_radius_fraction': .08, 'candidate_type': 'exploit'}


class SelectionTraceTests(unittest.TestCase):
    def test_tail_budget_keeps_full_plan_and_authorized_origins_distinct(self):
        plan = selection_plan()
        before = deepcopy(plan)
        summary = feedback_search._v2_selection_summary(plan, 2)
        self.assertEqual(summary['planned_origin_counts'], {'llm_center': 1, 'best_local': 2, 'global_uniform': 1})
        self.assertEqual(summary['authorized_origin_counts'], {'llm_center': 1, 'best_local': 1, 'global_uniform': 0})
        self.assertEqual(summary['authorized_origin_sequence'], ['llm_center', 'best_local'])
        summary['requested_quotas']['best_local'] = 0
        self.assertEqual(plan, before)
        continuation = feedback_search._v2_selection_summary(plan, 4, start_offset=1)
        self.assertEqual(continuation['authorized_origin_counts'],
                         {'llm_center': 0, 'best_local': 2, 'global_uniform': 1})
        self.assertEqual(continuation['authorized_origin_sequence'],
                         ['best_local', 'best_local', 'global_uniform'])

    def test_legacy_model_exploit_does_not_become_program_selection_provenance(self):
        legacy = {'selected': [[1] * 9], 'candidate_type': 'exploit', 'source': 'python_sample'}
        self.assertIsNone(feedback_search._v2_selection_summary(legacy, 1))
        self.assertEqual(result_analysis._selection_metadata(legacy), {})

    def test_invalid_signed_plan_origin_counts_radius_and_indices_are_rejected(self):
        mutations = [lambda p: p['selection_summary']['planned_origin_counts'].update(global_uniform=0),
                     lambda p: p.update(selected_pool_indices=[True, 1, 2, 3]),
                     lambda p: p.update(selected_pool_indices=[0, 1, 2, 99]),
                     lambda p: p['selection_summary'].update(local_radius_fraction=float('nan')),
                     lambda p: p['selection_summary'].update(local_radius_fraction=True),
                     lambda p: p['selection_summary'].update(local_radius_metric='unknown'),
                     lambda p: p['selection_summary']['requested_quotas'].update(best_local=-1)]
        for mutate in mutations:
            plan = selection_plan()
            mutate(plan)
            with self.subTest(mutation=mutate), self.assertRaises(TaskValidationError) as caught:
                feedback_search._v2_selection_summary(plan, 2)
            self.assertEqual(caught.exception.code, 'FEEDBACK_SELECTION')

    def test_outcome_counts_only_actual_committed_prefix_not_cached_plan(self):
        def point(iteration, origin=None):
            result = {'iteration': iteration, 'stress_mpa': 1000 - iteration * 10,
                      'stress_reduction_from_baseline_percent': iteration,
                      'approximate_area_change_from_baseline_percent': 0}
            if origin is not None:
                result.update(candidate_metadata(origin))
            return result
        trace = {'basis_completed_candidates': 0, 'selected_candidates': 4,
                 'evaluation_target_total_candidates': 2,
                 'selection_summary': feedback_search._v2_selection_summary(selection_plan(), 2)}
        analysis = {'baseline': point(0), 'candidates': [point(1, 'llm_center'), point(2, 'best_local')],
                    'completed_candidates': 2}
        result = feedback_search.update_decision_outcomes([trace], analysis)[0]
        self.assertEqual(result['outcome']['status'], 'completed')
        self.assertEqual(result['outcome']['observed_origin_counts'],
                         {'llm_center': 1, 'best_local': 1, 'global_uniform': 0})
        # Missing committed provenance remains unknown, even when the signed plan
        # tells us which origin was intended for that slot.
        analysis['candidates'][0] = point(1)
        result = feedback_search.update_decision_outcomes([trace], analysis)[0]
        self.assertEqual(result['outcome']['observed_origin_counts']['llm_center'], 0)
        self.assertEqual(result['outcome']['unknown_selection_origins'], 1)
        legacy = dict(trace)
        legacy.pop('selection_summary')
        self.assertNotIn('observed_origin_counts', feedback_search.update_decision_outcomes([legacy], analysis)[0]['outcome'])

    def test_v2_candidate_metadata_roles_and_numeric_radius_are_checked(self):
        for origin in ('llm_center', 'best_local', 'global_uniform'):
            metadata = result_analysis._selection_metadata(candidate_metadata(origin))
            self.assertEqual(metadata['selection_origin'], origin)
            self.assertNotIn('candidate_type', metadata)
        for change in ({'selection_origin': 'exploit'}, {'selection_role': 'exploit'},
                       {'local_radius_fraction': True}, {'local_radius_fraction': 0},
                       {'local_radius_fraction': float('inf')}, {'local_radius_fraction': 2}):
            with self.subTest(change=change), self.assertRaises(TaskValidationError):
                result_analysis._selection_metadata(dict(candidate_metadata('best_local'), **change))


class SelectionAnalysisTests(FixtureCase):
    def test_analysis_and_report_count_committed_origins_without_changing_old_records(self):
        run, state = self.save_run(done=4, target=4)
        for record, origin in zip(state['records'], ('llm_center', 'best_local', 'global_uniform')):
            record['proposal'] = candidate_metadata(origin)
        state['records'][3]['proposal'] = {'candidate_type': 'exploit', 'source': 'python_sample'}
        runtime.write_json(run / 'checkpoint.json', state)
        before = (run / 'checkpoint.json').read_bytes()
        with (patch.object(runtime.bridge.Client, 'generate', side_effect=AssertionError('no provider')),
              patch.object(controller, 'run', side_effect=AssertionError('no optimizer'))):
            result = result_analysis.analyze_run(run.name, settings=self.settings, since_candidate=2)
        self.assertTrue(result.ok, result.to_dict())
        data = result.data
        self.assertEqual(data['candidates'][0]['selection_origin'], 'llm_center')
        self.assertNotIn('selection_origin', data['baseline'])
        self.assertNotIn('selection_origin', data['candidates'][3])
        summary = data['search_selection_summary']
        self.assertEqual(summary['origin_counts'], {'llm_center': 1, 'best_local': 1, 'global_uniform': 1})
        self.assertEqual(summary['unknown_candidates'], 1)
        self.assertEqual(summary['window']['origin_counts'], {'llm_center': 0, 'best_local': 0, 'global_uniform': 1})
        self.assertEqual(summary['window']['unknown_candidates'], 1)
        report = result_analysis.render_report(data)
        self.assertIn('实际选点来源', report)
        self.assertIn('来源未记录：1', report)
        self.assertIn('强度判定、质量约束与寿命判定均未认证', report)
        self.assertEqual((run / 'checkpoint.json').read_bytes(), before)


class SelectionDOMTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node runtime is needed for the isolated DOM branch check.')
    def test_actual_quota_panel_is_not_inferred_from_llm_exploit_text(self):
        page = Path(__file__).resolve().parents[1] / 'static' / 'index.html'
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8'),code=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(code);assert(!html.includes('innerHTML'));
class Element{constructor(tag='div'){this.tag=tag;this.children=[];this.textContent='';}append(...nodes){this.children.push(...nodes)}replaceChildren(...nodes){this.children=[...nodes]}}
const nodes=new Map(),tags=[],get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id)};
const context={document:{createElement:tag=>{tags.push(tag);return new Element(tag)}},$:get,text:(id,value)=>{get(id).textContent=String(value??'—')},state:{}};
vm.createContext(context);vm.runInContext(code.split('\n').find(x=>x.startsWith('const num=')),context);
vm.runInContext(code.slice(code.indexOf('function decisionElement'),code.indexOf('function renderParameters')),context);
const flatten=e=>[e.textContent,...e.children.map(flatten)].join(' ');
context.workflow={feedback_policy:{enabled:true},decision_trace:[{generation:1,candidate_type:'exploit',rationale:'<img onerror=alert(1)>',outcome:{status:'completed'},selection_summary:{policy_version:'hybrid_local_v2',requested_quotas:{llm_center:1,best_local:2,global_uniform:1},planned_origin_counts:{llm_center:1,best_local:2,global_uniform:1},authorized_origin_counts:{llm_center:1,best_local:1,global_uniform:0},local_radius_fraction:.08,local_radius_metric:'normalized_Linf',surrogate_used:false,surrogate_reason:'insufficient_measured_samples'}}]};
vm.runInContext('renderDecisions(workflow,"workflow")',context);
let rendered=flatten(get('decision-trace'));assert(rendered.includes('程序实际选点计划'));assert(rendered.includes('本工作流授权'));assert(rendered.includes('LLM声明倾向（仅搜索假设）：exploit'));assert(rendered.includes('normalized_Linf'));assert(rendered.includes('GP筛选：未使用'));assert(!tags.includes('img'));
delete context.workflow.decision_trace[0].selection_summary;
vm.runInContext('renderDecisions(workflow,"workflow")',context);
rendered=flatten(get('decision-trace'));assert(!rendered.includes('程序实际选点计划'));assert(!rendered.includes('本工作流授权'));
console.log('selection DOM branches passed');
'''
        completed = subprocess.run([shutil.which('node'), '-e', script, str(page)],
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)


class SelectionWorkflowIntegrationTests(unittest.TestCase):
    # Reuse the fixture boundaries without inheriting and rediscovering all its
    # legacy test methods in this module.
    fake_proposal = feedback_fixture.FeedbackIntegrationTests.fake_proposal
    fake_evaluate = feedback_fixture.FeedbackIntegrationTests.fake_evaluate
    transport = feedback_fixture.FeedbackIntegrationTests.transport
    start = feedback_fixture.FeedbackIntegrationTests.start
    state = feedback_fixture.FeedbackIntegrationTests.state
    run_dir = feedback_fixture.FeedbackIntegrationTests.run_dir

    def setUp(self):
        feedback_fixture.FeedbackIntegrationTests.setUp(self)
        config_path = self.root / 'python_control' / 'search_config.json'
        config = runtime.read_json(config_path)
        config['selection_policy'] = 'hybrid_local_v2'
        runtime.write_json(config_path, config)
        problem = runtime.bridge.load_problem(self.settings.problem_file)
        self.response_sequence = []
        for first in (21.1111, 21.2222, 21.3333):
            center = {variable['name']: variable['reference'] for variable in problem['variables']}
            center['X1'] = first
            self.response_sequence.append(json.dumps({'center': center,
                'candidate_type': 'exploit', 'rationale': 'FIXTURE hypothesis, requires evaluation.'}))

    def test_twelve_candidates_use_three_feedback_requests_and_committed_quota_counts(self):
        preview = self.start(total=12, live=False)
        self.assertTrue(preview.ok, preview.to_dict())
        self.require_decision_before_candidate = self.state(preview).workflow_id
        result = supervisor.resume_workflow(self.require_decision_before_candidate,
                                            live=True, settings=self.settings)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (3, 13))
        state = self.state(result)
        self.assertEqual(state.status, 'completed')
        self.assertEqual(len(state.decision_trace), 3)
        checkpoint = runtime.read_json(self.run_dir(result) / 'checkpoint.json')
        self.assertEqual(checkpoint['nCompleted'], 12)
        expected = {'llm_center': 1, 'best_local': 2, 'global_uniform': 1}
        for trace in state.decision_trace:
            self.assertEqual(trace['selection_summary']['planned_origin_counts'], expected)
            self.assertEqual(trace['selection_summary']['authorized_origin_counts'], expected)
            self.assertEqual(trace['outcome']['observed_origin_counts'], expected)
            self.assertEqual(trace['outcome']['unknown_selection_origins'], 0)
            self.assertEqual(trace['outcome']['status'], 'completed')
        for body, completed in zip(self.requests, (0, 4, 8)):
            context = json.loads(body['input'])
            self.assertEqual(context['batch_feedback']['source_completed_candidates'], completed)
            self.assertEqual(context['search_selection']['policy'], 'hybrid_local_v2')
            self.assertIn('local_radius_fraction_of_variable_range', context['search_state'])
            self.assertNotIn('python_sampling_sigma_fraction_of_variable_range', context['search_state'])
        self.assertEqual(state.analysis['search_selection_summary']['origin_counts'],
                         {'llm_center': 3, 'best_local': 6, 'global_uniform': 3})
        self.assertEqual(state.analysis['costs']['llm']['observed_http_attempts'], 3)
        self.assertEqual(state.analysis['costs']['llm']['total_tokens'], 90)

    def test_six_candidate_tail_reports_two_authorized_and_observed_origins(self):
        result = self.start(total=6)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (2, 7))
        state = self.state(result)
        last = state.decision_trace[-1]
        self.assertEqual(sum(last['selection_summary']['planned_origin_counts'].values()), 4)
        self.assertEqual(sum(last['selection_summary']['authorized_origin_counts'].values()), 2)
        self.assertEqual(last['outcome']['status'], 'completed')
        self.assertEqual(sum(last['outcome']['observed_origin_counts'].values()), 2)
        self.assertEqual(last['outcome']['candidate_iterations'], [5, 6])

    def test_new_workflow_appending_three_excludes_preexisting_candidate_from_its_counts(self):
        self.stop_after = 1
        first = self.start(total=4)
        self.assertFalse(first.ok, first.to_dict())
        original = self.state(first)
        self.assertEqual(original.start_completed_candidates, 0)
        self.assertEqual((len(self.requests), self.evaluations), (1, 2))
        self.stop_after = None
        continued = supervisor.run_workflow(task={'operation': 'optimize',
            'run_id': original.run_id, 'additional_candidate_budget': 3},
            live=True, settings=self.settings)
        self.assertTrue(continued.ok, continued.to_dict())
        state = self.state(continued)
        self.assertNotEqual(state.workflow_id, original.workflow_id)
        self.assertEqual(state.start_completed_candidates, 1)
        self.assertEqual((len(self.requests), self.evaluations), (1, 5))
        trace = state.decision_trace[0]
        self.assertEqual(trace['basis_completed_candidates'], 0)
        self.assertEqual(trace['authorization_start_completed_candidates'], 1)
        self.assertEqual(trace['evaluation_target_total_candidates'], 4)
        self.assertEqual(trace['selection_summary']['authorized_origin_counts'],
                         {'llm_center': 0, 'best_local': 2, 'global_uniform': 1})
        self.assertEqual(trace['outcome']['observed_origin_counts'],
                         {'llm_center': 0, 'best_local': 2, 'global_uniform': 1})
        self.assertEqual(trace['outcome']['candidate_iterations'], [2, 3, 4])
        self.assertEqual(trace['outcome']['completed_candidates'], 3)
        self.assertEqual(trace['outcome']['best_improvement_mpa'], 30.0)
        # The original workflow still owns its whole four-candidate batch when
        # resumed, even though another workflow committed the remaining tasks.
        resumed = supervisor.resume_workflow(original.workflow_id, live=True, settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        original_trace = self.state(resumed).decision_trace[0]
        self.assertEqual(original_trace['authorization_start_completed_candidates'], 0)
        self.assertEqual(sum(original_trace['selection_summary']['authorized_origin_counts'].values()), 4)
        self.assertEqual(original_trace['outcome']['candidate_iterations'], [1, 2, 3, 4])
        self.assertEqual((len(self.requests), self.evaluations), (1, 5))


if __name__ == '__main__':
    unittest.main()
