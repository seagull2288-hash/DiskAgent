"""On-demand local/global GP diagnostics over an isolated browser DOM."""
import unittest

from agent_app.tests import test_recovery_ui as browser_fixture


@unittest.skipUnless(browser_fixture.NODE, 'Node is required for isolated browser logic tests.')
class PredictionV3DOMTests(unittest.TestCase):
    run_dom = browser_fixture.RecoveryDOMTests.run_dom

    def test_directional_plan_and_validation_are_separate_from_hypothesis(self):
        self.run_dom(r'''
context.workflow={feedback_policy:{enabled:true},decision_trace:[{generation:1,source_kind:'feedback_augmented',
  candidate_type:'exploit',rationale:'<img src=x onerror=bad()>',outcome:{status:'pending'},
  selection_summary:{policy_version:'hybrid_local_v3',local_method:'multiscale_coordinate_poll',directional_selected:2,
    global_schedule:{count:0,schedule_reason:'local_refinement',batch_number:9},
    requested_quotas:{llm_center:1,best_local:3,global_uniform:0},
    model_validation:{enabled:false,reason:'insufficient_local_training_samples'},surrogate_used:false}}]};
evaluate('renderDecisions(workflow,"workflow")');
const rendered=get('decision-trace').textContent;
assert(rendered.includes('hybrid_local_v3'));
assert(rendered.includes('多尺度坐标方向搜索'));
assert(rendered.includes('已选方向候选 2 个'));
assert(rendered.includes('动态全局探索配额'));
assert(rendered.includes('"count":0'));
assert(rendered.includes('局部 GP门控：未通过'));
assert(rendered.includes('全局GP门控：未验证'));
assert(rendered.includes('CV RMSE 未验证'));
assert(rendered.includes('LLM声明倾向（仅搜索假设）：exploit'));
assert(!createdTags.includes('img'));
''')

    def test_prediction_records_show_support_eligibility_and_uncertainty_status(self):
        self.run_dom(r'''
context.data={status:'ready',run_id:'run_case',model_scope:'local',validation:{enabled:true,
  reason:'rolling_cv_and_forward_holdout_passed',cv_rmse:2.5,cv_nrmse:.2,forward_holdout_rmse:3.2,
  unit:'MPa',local_training_samples:50,forward_holdout_samples:10,
  uncertainty_calibration:{method:'rolling_oof_residual_inflation_and_floor',samples:8,std_multiplier:2,
    std_floor:2.5,empirical_oof_coverage:1}},records:[{iteration:1,status:'pending',model_scope:'local',
    predicted_stress_mpa:700,predictive_std_mpa:2,within_validated_region:false,eligible_for_ranking:false},
    {iteration:2,status:'missing_pre_prediction',predicted_stress_mpa:null}]};
evaluate('renderPredictions(data)');
assert(get('prediction-validation').textContent.includes('局部 GP门控：通过'));
assert(get('prediction-validation').textContent.includes('全局GP门控：未验证'));
assert(get('prediction-validation').textContent.includes('滚动OOF残差校准'));
assert(get('prediction-validation').textContent.includes('覆盖率未经独立认证'));
assert(get('prediction-records').textContent.includes('局部支持域：域外；排序资格：不具备'));
assert(get('prediction-records').textContent.includes('校准未验证'));
assert(get('prediction-records').textContent.includes('无事前记录'));
evaluate('renderPredictions({status:"validation_failed",validation:{},records:[]})');
assert(get('prediction-validation').textContent.includes('当前 GP门控：未验证'));
assert(!get('prediction-validation').textContent.includes('局部 GP门控：通过'));
''')

    def test_model_comparison_is_explicit_read_only_and_repeated_clicks_are_blocked(self):
        self.run_dom(r'''
evaluate('state.predictionRun="run_case"');
assert.equal(calls.length,0);
let release;
context.api=async path=>{calls.push({path,method:'GET'});return await new Promise(resolve=>{release=resolve;});};
const first=evaluate('loadSearchDiagnostics()');
assert(get('compare-models').disabled);
const second=evaluate('loadSearchDiagnostics()');
assert.equal(calls.length,1);
assert.equal(calls[0].path,'/api/runs/run_case/search-diagnostics');
release({ok:true,data:{run_id:'run_case',models:{metric_models:{stress_mpa:{matern_ard_local:{
  model_scope:'local',fit_samples:38,forward_holdout_samples:15,unit:'MPa',cv_rmse:2.64,cv_nrmse:.17,
  forward_holdout_rmse:3.43,enabled:true,reason:'rolling_cv_and_forward_holdout_passed',
  forward_calibrated_interval_coverage:.8,uncertainty_calibration:{method:'rolling_oof_residual_inflation_and_floor'}},
  rbf_global:{model_scope:'global',enabled:false,reason:'validation_error_above_limit'}}}}}});
await first;await second;
assert(!get('compare-models').disabled);
assert(get('model-comparison').textContent.includes('matern_ard_local'));
assert(get('model-comparison').textContent.includes('局部 / 训练 38，前向 15'));
assert(get('model-comparison').textContent.includes('全局 / 训练 未记录'));
assert(get('model-comparison').textContent.includes('80.0 %'));
assert(get('model-comparison').textContent.includes('未验证'));
assert(get('comparison-evidence').textContent.includes('metric_models'));
assert(get('comparison-message').textContent.includes('独立未来确认尚未完成'));
evaluate('renderModelComparison({models:{metric_models:{}}})');
assert(get('comparison-message').textContent.includes('未返回可验证'));
''')

    def test_stale_comparison_does_not_populate_a_new_bound_run(self):
        self.run_dom(r'''
evaluate('state.predictionRun="old"');
let release;
context.api=async()=>await new Promise(resolve=>{release=resolve;});
const old=evaluate('loadSearchDiagnostics()');
get('tab-predictions').className='hidden';
evaluate('preparePredictionRun("new")');
assert(get('compare-models').disabled);
release({ok:true,data:{models:{metric_models:{stress_mpa:{old:{enabled:true}}}}}});
await old;
assert.equal(get('model-comparison').textContent,'');
assert.equal(get('comparison-evidence').textContent,'未计算');
assert(!get('compare-models').disabled);
''')


if __name__ == '__main__':
    unittest.main()
