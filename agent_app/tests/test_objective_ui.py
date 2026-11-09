"""Execute both workbench scripts against an isolated, event-driven DOM fixture."""
from pathlib import Path
import shutil
import subprocess
import unittest

from agent_app.tests.test_recovery_ui import HARNESS, PAGE


NODE = shutil.which('node')
SCRIPT = PAGE.parent / 'objectives.js'
CHAT = PAGE.parent / 'chat.js'

DOM_SUPPORT = r'''
const originalGet=get;
get=id=>{
  const node=originalGet(id);
  node.id=id;
  if(!node.fixtureInitialized){
    const opening=html.match(new RegExp('<[^>]*\\bid="'+id+'"[^>]*>'))?.[0]||'';
    node.type=opening.match(/type="([^"]*)"/)?.[1]||'';
    node.value=opening.match(/value="([^"]*)"/)?.[1]||'';
    node.fixtureInitialized=true;
  }
  return node;
};
context.document.getElementById=get;
for(const match of html.matchAll(/<(?:input|select)\b[^>]*\bid="([^"]+)"[^>]*>/g)){get(match[1]);}
context.window=context;
context.diskEngineeringCapabilities={mass:'axisymmetric_revolved_section_v1',modal_profile:'DiskAgent Prestressed Axisymmetric'};
context.Event=class{constructor(type){this.type=type;}};
const draftStore=new Map();
context.localStorage={setItem:(key,value)=>draftStore.set(key,String(value)),getItem:key=>draftStore.get(key)||null};
const controls=()=>[...nodes.values()].filter(node=>
  /^(role-|weight-(?!label)|constraint-|reference-|limit-|upper-|enable-objectives$|objective-template$|modal-)/.test(node.id||''));
get('objective-policy').querySelectorAll=()=>controls();
get('objective-policy').contains=node=>controls().includes(node);
Element.prototype.dispatchEvent=function(event){return this.listeners[event.type]?.({target:this,type:event.type});};
Object.defineProperty(Element.prototype,'innerHTML',{set(markup){
  for(const match of markup.matchAll(/<(input|select|output)\b([^>]*\bid="([^"]+)"[^>]*)>/g)){
    const node=get(match[3]);
    node.type=match[2].match(/type="([^"]*)"/)?.[1]||'';
    node.value=match[2].match(/value="([^"]*)"/)?.[1]||'';
    if(match[1]==='select'){
      const remaining=markup.slice(match.index+match[0].length);
      node.value=remaining.match(/<option value="([^"]*)"/)?.[1]||'';
    }
    this.append(node);
  }
}});
Element.prototype.createTHead=Element.prototype.createTBody=function(){const child=new Element('tbody');this.append(child);return child;};
Element.prototype.insertRow=function(){const child=new Element('tr');this.append(child);return child;};
Element.prototype.insertCell=function(){const child=new Element('td');this.append(child);return child;};
const tabButtons=[...html.matchAll(/<button[^>]*\bdata-tab="([^"]+)"[^>]*>/g)].map(match=>{
  const node=new Element('button');node.dataset.tab=match[1];return node;
});
context.document.querySelectorAll=selector=>selector==='[data-tab]'?tabButtons:[];
'''


@unittest.skipUnless(NODE, 'Node is required for isolated DOM tests.')
class ObjectiveDOMTests(unittest.TestCase):
    def run_dom(self, body):
        source = HARNESS.replace('vm.createContext(context);', DOM_SUPPORT + '\nvm.createContext(context);')
        source += '\nvm.runInContext(fs.readFileSync(process.argv[2],"utf8"),context);\n'
        source += r'''
async function loadAssistant(){
 context.location={origin:'http://127.0.0.1:9999'};
 context.crypto={randomUUID:()=> 'fixture-request'};
 const session={session_id:'chat_fixture',status:'ready',messages:[],events:[],context:{},
  pending_preview:null,jobs:[],costs:{model_requests:0,total_tokens:0,token_accounting_status:'complete'}};
 route('GET','/api/config',{conversation:{available:true,unified_tasks:'v1'}});
 route('GET','/api/chat/sessions',{ok:true,sessions:[]});
 route('POST','/api/chat/sessions',{ok:true,session});
 route('POST','/api/chat/sessions/chat_fixture/messages',{ok:true,session:{...session,status:'working'}});
 vm.runInContext(fs.readFileSync(process.argv[3],'utf8'),context);
 await new Promise(resolve=>setImmediate(resolve));
 calls.length=0;
}
'''
        source += '\n(async()=>{try{\n' + body + '\n}catch(error){console.error(error);process.exitCode=1;}})();'
        result = subprocess.run([NODE, '-e', source, str(PAGE), str(SCRIPT), str(CHAT)],
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preview_includes_selected_objectives_relative_limit_and_weight(self):
        self.run_dom(r'''
evaluate('refresh=async()=>{};state.token="fixture-csrf"');
await loadAssistant();
get('request').value='新建优化4个候选';
get('objective-template').value='dual';
get('objective-template').listeners.change({target:get('objective-template')});
get('constraint-mass_kg').value='<=';
get('reference-mass_kg').value='baseline_ratio';
get('limit-mass_kg').value='95';
get('weight-stress_mpa').value='0.9';
await get('chat-send').listeners.click();
const posted=calls.filter(call=>call.path.endsWith('/messages'));
assert.equal(posted.length,1);
const body=JSON.parse(posted[0].body);
assert.equal(body.action,'message');
assert.equal(body.optimization_policy.objectives.length,2);
assert.equal(body.optimization_policy.objectives[0].weight,0.9);
assert.equal(body.optimization_policy.constraints[0].value,0.95);
assert.equal(body.optimization_policy.collect_mass,true);
assert.equal(body.optimization_policy.modal,null);
assert.equal(posted[0].headers['X-CSRF-Token'],'fixture-csrf');
let invalidated=0;context.diskAssistant.invalidate=()=>invalidated++;
get('weight-stress_mpa').value='0.2';
get('objective-policy').listeners.input({target:get('weight-stress_mpa')});
assert.equal(invalidated,1);
''')

    def test_blank_enabled_limit_is_blocked_before_any_http_submission(self):
        self.run_dom(r'''
evaluate('refresh=async()=>{}');
await loadAssistant();
get('request').value='新建优化1个候选';
get('enable-objectives').checked=true;
get('constraint-mass_kg').value='<=';
get('limit-mass_kg').value='';
await get('chat-send').listeners.click();
assert.equal(calls.length,0);
assert(get('chat-error').textContent.includes('需要填写正数'));
''')

    def test_older_backend_cannot_silently_drop_enabled_objective_configuration(self):
        self.run_dom(r'''
evaluate('refresh=async()=>{}');
await loadAssistant();
get('request').value='新建优化1个候选';
get('enable-objectives').checked=true;
evaluate('globalThis.readOptimizationPolicy=undefined');
await get('chat-send').listeners.click();
assert.equal(calls.length,0);
assert(get('chat-error').textContent.includes('新版工作台'));
''')

    def test_draft_can_be_saved_incomplete_then_restored_without_api_calls(self):
        self.run_dom(r'''
get('enable-objectives').checked=true;
get('constraint-mass_kg').value='<=';
get('limit-mass_kg').value='';
get('weight-stress_mpa').value='0.8';
get('save-objective-draft').listeners.click();
get('enable-objectives').checked=false;
get('weight-stress_mpa').value='0.1';
get('load-objective-draft').listeners.click();
assert.equal(get('enable-objectives').checked,true);
assert.equal(get('weight-stress_mpa').value,'0.8');
assert.equal(get('limit-mass_kg').value,'');
assert.equal(calls.length,0);
''')

    def test_saved_policy_round_trip_preserves_multiple_constraints_and_sources(self):
        self.run_dom(r'''
const policy={schema_version:'disk.optimization-policy.v1',collect_mass:true,modal:null,
 objectives:[{metric:'stress_mpa',direction:'minimize',weight:0.9}],constraints:[
 {metric:'mass_kg',operator:'<=',reference:'baseline_ratio',value:.95,upper:null,source:'探索上限'},
 {metric:'mass_kg',operator:'>=',reference:'absolute',value:20,upper:null,source:'探索下限'}]};
context.policy=policy;
evaluate('loadOptimizationPolicy(policy)');
assert.equal(get('enable-objectives').checked,true);
assert.equal(get('limit-mass_kg').value,95);
assert.deepEqual(JSON.parse(JSON.stringify(evaluate('readOptimizationPolicy()'))),policy);
evaluate('loadOptimizationPolicy(null)');
assert.equal(get('enable-objectives').checked,false);
assert.equal(evaluate('readOptimizationPolicy()'),null);
assert.equal(calls.length,0);
''')

    def test_re_evaluation_uses_selected_run_and_displays_untrusted_source_as_text(self):
        self.run_dom(r'''
evaluate('state.token="fixture-csrf"');
context.objectiveRunId='run_saved';
get('enable-objectives').checked=true;
const data={run_id:'run_saved',policy_id:'policy_fixture',feasible_count:1,best_iteration:1,pareto_iterations:[1],
  policy:{objectives:[]},observations:[{}, {stress_mpa:900,mass_kg:9}],evaluations:[
    {iteration:1,status:'feasible',score:.9,constraint_checks:[
      {metric:'mass_kg',operator:'<=',limit:10,actual:9,status:'passed',source:'<img src=x onerror=alert(1)>'}]}]};
route('POST','/api/runs/run_saved/objectives',{ok:true,data});
await get('reevaluate-objectives').listeners.click();
assert.equal(calls.length,1);
assert.equal(calls[0].path,'/api/runs/run_saved/objectives');
assert(get('objective-local-preview').textContent.includes('目标推荐'));
assert(get('objective-local-preview').textContent.includes('<img src=x onerror=alert(1)>'));
assert(!createdTags.includes('img'));
''')

    def test_new_tab_is_connected_to_visibility_switching(self):
        self.run_dom(r'''
const button=tabButtons.find(node=>node.dataset.tab==='objectives');
assert(button);
button.listeners.click();
assert.equal(get('tab-objectives').classList.contains('hidden'),false);
assert.equal(get('tab-results').classList.contains('hidden'),true);
assert.equal(calls.length,0);
''')

    def test_frequency_settings_use_verified_name_mode_and_require_prestress(self):
        self.run_dom(r'''
get('enable-objectives').checked=true;
get('role-frequency_hz').value='maximize';
const policy=evaluate('readOptimizationPolicy()');
assert.equal(policy.modal.analysis_name,'DiskAgent Prestressed Axisymmetric');
assert.equal(policy.modal.mode_index,1);
assert.equal(policy.modal.prestress_required,true);
get('modal-order').value='7';
assert.throws(()=>evaluate('readOptimizationPolicy()'));
assert.equal(calls.length,0);
''')

    def test_overview_separates_infeasible_stress_minimum_from_feasible_recommendation(self):
        self.run_dom(r'''
const check={metric:'mass_kg',operator:'<=',limit:10,actual:12,margin:-2,margin_unit:'kg',
  status:'failed',source:'<img src=x onerror=alert(1)>'};
const minimum={iteration:1,observation:{stress_mpa:800,mass_kg:12},evaluation:{
  status:'infeasible',score:1,missing_metrics:[],constraint_checks:[check]}};
const recommended={iteration:2,observation:{stress_mpa:900,mass_kg:9,parameters:{X1:20}},evaluation:{
  status:'feasible',score:.9,missing_metrics:[],constraint_checks:[{...check,actual:9,margin:1,status:'passed'}]}};
context.renderDesignSelection({policy_id:'policy_saved',policy_source:'saved_optimization_policy',
  constraint_count:1,minimum_stress:minimum,recommended,pareto:[recommended]});
const displayed=get('design-selection').textContent;
for(const text of ['最小应力方案','当前配置推荐方案','Pareto 方案集','policy_saved','超限',
  '记录 1','记录 2','裕量 -2.00000 kg','裕量 1.00000 kg','X1=20.00000 mm',check.source]){
  assert(displayed.includes(text),text);
}
assert(!createdTags.includes('img'));
assert.equal(calls.length,0);
''')

    def test_no_recommendation_and_legacy_policy_are_explicit_and_clear_old_results(self):
        self.run_dom(r'''
const minimum={iteration:1,observation:{stress_mpa:800},evaluation:{
  status:'missing',score:null,missing_metrics:['mass_kg'],constraint_checks:[]}};
context.renderDesignSelection({policy_id:'policy_missing',policy_source:'saved_optimization_policy',
  constraint_count:0,minimum_stress:minimum,recommended:null,pareto:[]});
assert(get('design-selection').textContent.includes('暂无可行推荐'));
assert(get('design-selection').textContent.includes('缺少评价数据 mass_kg'));
assert(get('design-selection').textContent.includes('暂无可行 Pareto 方案'));
minimum.evaluation={status:'feasible',score:.8,missing_metrics:[],constraint_checks:[]};
context.renderDesignSelection({policy_id:'policy_default',policy_source:'default_stress_objective',
  constraint_count:0,minimum_stress:minimum,recommended:minimum,pareto:[minimum]});
assert(get('design-selection').textContent.includes('历史默认应力目标'));
assert(get('design-selection').textContent.includes('当前配置未启用数值约束'));
context.renderDesignSelection(null);
assert(!get('design-selection').textContent.includes('policy_default'));
assert.equal(calls.length,0);
''')

    def test_workflow_overview_uses_live_snapshot_selection_instead_of_cached_analysis(self):
        self.run_dom(r'''
const design={iteration:5,observation:{stress_mpa:900,mass_kg:9},evaluation:{
  status:'feasible',score:.9,missing_metrics:[],constraint_checks:[]}};
const selections={policy_id:'policy_current',policy_source:'saved_optimization_policy',
  constraint_count:0,minimum_stress:design,recommended:design,pareto:[design]};
context.detail={ok:true,data:{state:{status:'running'},analysis:{
  design_selection:{...selections,policy_id:'policy_cached'}},
  run_snapshot:{ok:true,data:{completed_candidates:5,target_total_candidates:6,design_selection:selections}}}};
evaluate('render(detail,"workflow","wf_live")');
assert(get('design-selection').textContent.includes('policy_current'));
assert(!get('design-selection').textContent.includes('policy_cached'));
assert.equal(calls.length,0);
''')

    def test_archive_row_can_simultaneously_be_minimum_recommended_and_pareto(self):
        self.run_dom(r'''
context.renderObjectiveArchive({policy_id:'policy_same',feasible_count:1,best_iteration:0,
  pareto_iterations:[0],policy:{},observations:[{stress_mpa:900}],evaluations:[
    {iteration:0,status:'feasible',score:1,constraint_checks:[]}]});
assert(get('objective-results').textContent.includes('最小应力 / 目标推荐 / Pareto'));
assert.equal(calls.length,0);
''')
