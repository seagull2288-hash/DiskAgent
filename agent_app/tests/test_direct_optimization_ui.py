"""Direct proposal UI, historical provenance and stale-request protection."""
import unittest

from agent_app.tests import test_recovery_ui as fixture


class DirectOptimizationMarkupTests(unittest.TestCase):
    def test_direct_process_replaces_active_prediction_tab(self):
        html = fixture.PAGE.read_text(encoding='utf-8')
        self.assertIn('data-tab="optimization">LLM 优化过程', html)
        self.assertIn('class="hidden" id="legacy-predictions-tab"', html)
        self.assertIn('class="hidden" id="legacy-feedback-tab"', html)
        self.assertIn('优化 LLM 在真实计算的每个候选轮次调用', html)


@unittest.skipUnless(fixture.NODE, 'Node is required for isolated browser logic tests.')
class DirectOptimizationDOMTests(unittest.TestCase):
    run_dom = fixture.RecoveryDOMTests.run_dom

    def test_proposals_and_results_are_rendered_as_text_with_actual_source(self):
        self.run_dom(r'''
context.trace={run_id:'run_direct',strategy:'direct',mode_label:'LLM 逐轮提出尺寸',
  completed_candidates:1,target_total_candidates:2,llm_direct_candidates:1,feasible_candidates:1,
  best_iteration:1,records:[{iteration:1,source_label:'LLM 逐轮尺寸提案',status:'committed',
  configured_status:'feasible',stress_mpa:700,mass_kg:30,frequency_hz:900,improved_incumbent:true,
  anchor_iteration:0,rationale:'<img src=x onerror=bad()>',parameters:{X1:20},parameter_changes:{X1:-.2}}]};
evaluate('renderOptimizationTrace(trace)');
assert.equal(get('optimization-direct-count').textContent,'1');
assert(get('optimization-records').textContent.includes('700.000'));
assert(get('optimization-records').textContent.includes('-0.2000'));
assert(get('optimization-records').textContent.includes('<img src=x onerror=bad()>'));
assert(get('legacy-predictions-tab').classList.contains('hidden'));
assert(!createdTags.includes('img'));
assert.equal(calls.length,0);
''')

    def test_stale_response_cannot_overwrite_a_new_run(self):
        self.run_dom(r'''
let release;
context.api=async path=>{
  calls.push({path,method:'GET'});
  if(path.includes('/old/'))return await new Promise(resolve=>{release=resolve;});
  return {ok:true,data:{run_id:'new',strategy:'direct',mode_label:'LLM 逐轮提出尺寸',
    completed_candidates:2,target_total_candidates:2,llm_direct_candidates:2,feasible_candidates:2,records:[]}};
};
evaluate('prepareOptimizationRun("old")');
evaluate('prepareOptimizationRun("new")');
await new Promise(resolve=>setImmediate(resolve));
release({ok:true,data:{run_id:'old',strategy:'center_surrogate',llm_direct_candidates:99,records:[]}});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(get('optimization-direct-count').textContent,'2');
assert(get('optimization-message').textContent.includes('new'));
assert(get('legacy-predictions-tab').classList.contains('hidden'));
assert(!get('refresh-optimization').disabled);
assert(calls.every(call=>call.method==='GET'&&call.path.endsWith('/optimization-trace')));
''')

    def test_legacy_source_only_reveals_historical_audit_controls(self):
        self.run_dom(r'''
evaluate('renderOptimizationTrace({run_id:"run_old",strategy:"center_surrogate",mode_label:"历史搜索运行",completed_candidates:100,target_total_candidates:100,llm_direct_candidates:0,other_candidates:100,records:[]})');
assert(!get('legacy-predictions-tab').classList.contains('hidden'));
assert(get('optimization-message').textContent.includes('历史搜索运行'));
assert.equal(get('optimization-direct-count').textContent,'0');
assert.equal(calls.length,0);
''')


if __name__ == '__main__':
    unittest.main()
