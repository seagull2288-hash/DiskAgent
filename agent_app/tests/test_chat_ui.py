"""Browser conversation submits MCP previews with immutable execution references."""
import subprocess
import unittest

from agent_app.tests import test_objective_ui as fixture


SCRIPT = fixture.PAGE.parent / 'chat.js'
SETUP = r'''
context.location={origin:'http://127.0.0.1:9999'};
context.crypto={randomUUID:()=> '12345678-1234-1234-1234-123456789abc'};
evaluate('state.token="fixture-csrf"');
const session={session_id:'chat_fixture',status:'ready',messages:[],events:[],context:{},pending_preview:null,jobs:[],
 costs:{model_requests:0,total_tokens:0,token_accounting_status:'complete'}};
route('GET','/api/config',{conversation:{available:true,unified_tasks:'v1'}});
route('GET','/api/chat/sessions',{ok:true,sessions:[]});
evaluate('refresh=async()=>{}');
'''


@unittest.skipUnless(fixture.NODE, 'Node is required for isolated browser tests.')
class ChatDOMTests(unittest.TestCase):
    def run_dom(self, body):
        source = fixture.HARNESS.replace('vm.createContext(context);', fixture.DOM_SUPPORT + '\nvm.createContext(context);')
        source += SETUP
        source += '\n(async()=>{try{\n' + body + '\n}catch(error){console.error(error);process.exitCode=1;}})();'
        result = subprocess.run([fixture.NODE, '-e', source, str(fixture.PAGE), str(SCRIPT)],
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_startup_reads_configuration_and_does_not_create_or_execute_tasks(self):
        self.run_dom(r'''
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
await new Promise(resolve=>setImmediate(resolve));
assert(calls.every(call=>call.method==='GET'));
assert(get('chat-status').textContent.includes('明确任务使用规则'));
assert.equal(get('chat-send').disabled,false);
assert.equal(get('chat-execute').disabled,true);
''')

    def test_send_carries_selected_run_and_stable_request_and_uses_chat_endpoint(self):
        self.run_dom(r'''
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
await new Promise(resolve=>setImmediate(resolve));
route('POST','/api/chat/sessions',{ok:true,session});
route('POST','/api/chat/sessions/chat_fixture/messages',{ok:true,session:{...session,status:'working'}});
get('request').value='读取当前运行的推荐方案';
get('context').value='run_selected';
await get('chat-send').listeners.click();
const sent=calls.find(call=>call.path.endsWith('/messages'));
const body=JSON.parse(sent.body);
assert.equal(body.context_run_id,'run_selected');
assert.equal(body.message,'读取当前运行的推荐方案');
assert(body.request_id.startsWith('msg_'));
assert.equal(body.action,'message');
assert.equal(body.auto_recover,true);
assert.equal(body.optimization_policy,null);
assert.equal(sent.headers['X-CSRF-Token'],'fixture-csrf');
assert(!calls.some(call=>call.path==='/api/workflows'));
assert.equal(get('chat-send').disabled,true);
''')

    def test_execution_submits_only_saved_preview_reference_and_renders_untrusted_text_safely(self):
        self.run_dom(r'''
const malicious='<img src=x onerror=alert(1)>';
const ready={...session,messages:[{role:'assistant',text:malicious}],pending_preview:{
 preview_id:'preview_frozen',tool:'disk_evaluate_design',arguments:{parameters:{X1:20}},
 budget:{design_evaluation_tasks:1}}};
draftStore.set('diskagent.chat.http://127.0.0.1:9999','chat_fixture');
route('GET','/api/chat/sessions/chat_fixture',{ok:true,session:ready});
route('POST','/api/chat/sessions/chat_fixture/messages',{ok:true,session:{...ready,status:'working'}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
await new Promise(resolve=>setImmediate(resolve));
assert(get('chat-answer').textContent.includes(malicious));
assert(!createdTags.includes('img'));
assert.equal(get('chat-execute').disabled,false);
get('request').value='另一个没有提交的编辑';
await get('chat-execute').listeners.click();
const body=JSON.parse(calls.find(call=>call.path.endsWith('/messages')).body);
assert.equal(body.action,'execute');
assert.equal(body.preview_id,'preview_frozen');
assert(!('parameters' in body));
assert(!('message' in body));
''')

    def test_task_echo_and_confirmation_are_hidden_with_compact_execution_controls(self):
        self.run_dom(r'''
const ready={...session,context:{auto_recover:true},messages:[
 {role:'user',text:'新建优化1个候选',request_id:'msg_rule'},
 {role:'assistant',text:'已按规则生成任务预览',request_id:'msg_rule'}],
 events:[{type:'preview',request_id:'msg_rule'}],pending_preview:{preview_id:'preview_task',
 tool:'disk_submit_optimization',arguments:{task:{total_candidate_budget:1}},
 selection_context:{auto_recover:true},budget:{additional_candidates:1,baseline_evaluations:1}}};
draftStore.set('diskagent.chat.http://127.0.0.1:9999','chat_fixture');
route('GET','/api/chat/sessions/chat_fixture',{ok:true,session:ready});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
await new Promise(resolve=>setImmediate(resolve));
assert(!html.includes('id="chat-messages"'));
assert(!html.includes('id="chat-preview-parameters"'));
assert.equal(get('chat-answer').textContent,'');
assert(get('chat-answer').classList.contains('hidden'));
assert(get('chat-task-summary').textContent.includes('候选 1 个，基准评价 1 个'));
assert.equal(get('chat-execute').disabled,false);
assert(!get('chat-execute').classList.contains('hidden'));
assert(calls.every(call=>call.method==='GET'));
''')

    def test_changed_settings_invalidate_preview_across_polling_and_block_execution(self):
        self.run_dom(r'''
const ready={...session,context:{auto_recover:true},pending_preview:{preview_id:'preview_frozen',
 tool:'disk_submit_optimization',arguments:{task:{total_candidate_budget:4}},
 selection_context:{auto_recover:true},budget:{additional_candidates:4,baseline_evaluations:1}}};
draftStore.set('diskagent.chat.http://127.0.0.1:9999','chat_fixture');
route('GET','/api/chat/sessions/chat_fixture',{ok:true,session:ready});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
await new Promise(resolve=>setImmediate(resolve));
assert.equal(get('chat-execute').disabled,false);
get('auto-recover').checked=false;
get('auto-recover').listeners.change();
assert.equal(get('chat-execute').disabled,true);
assert(get('chat-preview-context').textContent.includes('设置已改变'));
await get('chat-execute').listeners.click();
assert(!calls.some(call=>call.method==='POST'));
get('auto-recover').checked=true;
get('auto-recover').listeners.change();
assert.equal(get('chat-execute').disabled,true);
''')

    def test_switching_saved_conversation_restores_run_and_clears_explicit_null_binding(self):
        self.run_dom(r'''
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
await new Promise(resolve=>setImmediate(resolve));
route('GET','/api/chat/sessions/chat_old',{ok:true,session:{...session,session_id:'chat_old',
 context:{run_id:'run_original',auto_recover:false,optimization_policy:null}}});
await get('chat-session-select').listeners.change({target:{value:'chat_old'}});
assert.equal(get('context').value,'run_original');
assert.equal(get('auto-recover').checked,false);
assert(get('chat-context').textContent.includes('run_original'));
route('GET','/api/chat/sessions/chat_new',{ok:true,session:{...session,session_id:'chat_new',
 context:{run_id:null,auto_recover:true,optimization_policy:null}}});
await get('chat-session-select').listeners.change({target:{value:'chat_new'}});
assert.equal(get('context').value,'');
assert.equal(get('auto-recover').checked,true);
assert(calls.every(call=>call.method==='GET'));
''')

    def test_result_card_uses_measured_values_and_shows_unknown_reference_constraint(self):
        self.run_dom(r'''
const ready={...session,result_cards:[{label:'指定尺寸评价',job_id:'job_evidence',policy_id:'policy_frozen',
 observation:{stress_mpa:739.12,mass_kg:36.13,parameters:{X1:21.013}},native_verified:true,
 native_report_file:'registered/native.json',constraint_status:'missing',evaluation:{status:'missing',
 constraint_checks:[{metric:'mass_kg',status:'missing',margin:null}]}}]};
draftStore.set('diskagent.chat.http://127.0.0.1:9999','chat_fixture');
route('GET','/api/chat/sessions/chat_fixture',{ok:true,session:ready});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
await new Promise(resolve=>setImmediate(resolve));
const card=get('chat-result-cards').textContent;
for(const text of ['739.120000 MPa','36.130000 kg','评价数据不足','policy_frozen','registered/native.json']){
 assert(card.includes(text),text);
}
assert(calls.every(call=>call.method==='GET'));
''')
