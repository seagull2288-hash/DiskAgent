"""Recovery browser logic in an isolated DOM with recorded fake HTTP calls."""
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import unittest


PAGE = Path(__file__).resolve().parents[1] / 'static' / 'index.html'
NODE = shutil.which('node')


class InputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = {}

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == 'input' and 'id' in attributes:
            self.inputs[attributes['id']] = attributes


class RecoveryMarkupTests(unittest.TestCase):
    def test_recovery_panel_is_in_sidebar_below_existing_runs(self):
        html = PAGE.read_text(encoding='utf-8')
        sidebar = html.split('<aside>', 1)[1].split('</aside>', 1)[0]
        self.assertEqual(html.count('id="recovery-panel"'), 1)
        self.assertIn('id="recovery-panel"', sidebar)
        self.assertLess(sidebar.index('id="runs"'), sidebar.index('id="recovery-panel"'))

    def test_auto_recovery_checkbox_is_checked_by_default(self):
        parser = InputParser()
        parser.feed(PAGE.read_text(encoding='utf-8'))
        self.assertEqual(parser.inputs['auto-recover']['type'], 'checkbox')
        self.assertIn('checked', parser.inputs['auto-recover'])


HARNESS = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const code=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(code);
assert(!html.includes('innerHTML'));
const nodes=new Map(),createdTags=[],calls=[],routes=new Map();
class Element{
  constructor(tag='div'){
    this.tag=tag;this.children=[];this._text='';this.className='';this.value='';
    this.checked=false;this.disabled=false;this.style={};this.listeners={};this.dataset={};
    this.classList={
      contains:name=>this.className.split(/\s+/).includes(name),
      toggle:(name,force)=>{
        const names=new Set(this.className.split(/\s+/).filter(Boolean));
        const enabled=force===undefined?!names.has(name):force;
        if(enabled)names.add(name);else names.delete(name);
        this.className=[...names].join(' ');return enabled;
      }
    };
  }
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent||'').join(' ');}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=[...children];this._text='';}
  addEventListener(name,callback){this.listeners[name]=callback;}
  focus(){this.focused=true;}
  querySelectorAll(){return [];}
  setAttribute(name,value){this[name]=String(value);}
}
function get(id){
  if(!nodes.has(id)){
    const element=new Element();
    const tag=html.match(new RegExp('<[^>]*\\bid="'+id+'"[^>]*>'))?.[0]||'';
    element.className=tag.match(/class="([^"]*)"/)?.[1]||'';
    element.checked=/\bchecked\b/.test(tag);
    nodes.set(id,element);
  }
  return nodes.get(id);
}
function route(method,path,value,status=200){routes.set(method+' '+path,{value,status});}
const context={
  document:{getElementById:get,createElement:tag=>{createdTags.push(tag);return new Element(tag);},
    createElementNS:(_ns,tag)=>{createdTags.push(tag);return new Element(tag);},
    createDocumentFragment:()=>new Element('fragment'),querySelectorAll:()=>[],addEventListener:()=>{}},
  fetch:async(path,options={})=>{
    const method=options.method||'GET';
    calls.push({path,method,body:options.body,headers:options.headers});
    const response=routes.get(method+' '+path);
    if(!response)throw new Error('Unexpected fake HTTP '+method+' '+path);
    return {ok:response.status>=200&&response.status<300,status:response.status,
      headers:{get:()=> 'application/json'},json:async()=>response.value,text:async()=>JSON.stringify(response.value)};
  },
  console,setInterval:()=>1,queueMicrotask,
};
vm.createContext(context);
const bootstrap=code.lastIndexOf('(async()=>{try{const config=await api(');
assert(bootstrap>0);
vm.runInContext(code.slice(0,bootstrap),context);
const evaluate=source=>vm.runInContext(source,context);
const snapshot={status:'waiting',can_recover:true,reason_zh:'供应商HTTP500，等待有限次数重试。',
  workflow_id:'wf_case',run_id:'run_case',completed_candidates:44,target_total_candidates:100,
  remaining_candidates:56,pending_iteration:45,pending_phase:'propose',attempt:1,max_attempts:3,
  total_attempts:2,max_total_attempts:8,http_attempts:4,max_http_attempts:12,
  next_retry_at:new Date(Date.now()+20000).toISOString(),last_error:{code:'HTTPError',message:'HTTP500'},
  events:[{at:'2026-11-07T10:00:00+08:00',status:'waiting',message:'等待下一次恢复'}]};
function workflowRoutes(recovery=snapshot,active=null){
  route('GET','/api/config',{ok:true,csrf_token:'fixture-csrf',active_job_id:active});
  route('GET','/api/workflows',{ok:true,active_job_id:active,workflows:[{workflow_id:'wf_case',status:'failed',
    request_text:'优化100个候选',progress:{completed_candidates:44,target_total_candidates:100}}]});
  route('GET','/api/runs',{ok:true,runs:[]});
  route('GET','/api/workflows/wf_case',{ok:true,status:'workflow_read',data:{workflow_id:'wf_case',run_id:'run_case',
    state:{workflow_id:'wf_case',status:'failed',task:{operation:'optimize'},request_text:'优化100个候选',
      start_completed_candidates:0,target_total_candidates:100},
    run_snapshot:{ok:true,data:{completed_candidates:44,target_total_candidates:100,run_status:'failed'}},recovery}});
}
'''


@unittest.skipUnless(NODE, 'Node is required for isolated browser logic tests.')
class RecoveryDOMTests(unittest.TestCase):
    def run_dom(self, test_body):
        source = HARNESS + '\n(async()=>{try{\n' + test_body + '\n}catch(error){console.error(error);process.exitCode=1;}})();'
        result = subprocess.run([NODE, '-e', source, str(PAGE)], capture_output=True, text=True,
                                encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_recovered_error_is_labeled_as_history_and_current_error_remains_visible(self):
        self.run_dom(r'''
context.snapshot=JSON.parse(JSON.stringify(snapshot));
context.snapshot.status='completed';context.snapshot.last_recovered_error=context.snapshot.last_error;context.snapshot.last_error=null;
evaluate('renderRecovery(snapshot,"workflow","wf_case")');
assert(get('recovery-error').textContent.startsWith('已恢复的历史记录'));
assert.equal(get('recovery-error').className,'recovery-details');
context.snapshot.status='recovered';context.snapshot.last_error=context.snapshot.last_recovered_error;delete context.snapshot.last_recovered_error;
evaluate('renderRecovery(snapshot,"workflow","wf_case")');
assert(get('recovery-error').textContent.startsWith('已恢复的历史记录'));
context.snapshot.status='needs_review';
evaluate('renderRecovery(snapshot,"workflow","wf_case")');
assert(!get('recovery-error').textContent.includes('已恢复的历史记录'));
assert.equal(get('recovery-error').className,'recovery-error');
''')

    def test_running_checkpoint_clears_stale_analysis_error(self):
        self.run_dom(r'''
context.prepareOptimizationRun=()=>{};context.preparePredictionRun=()=>{};
context.detail={ok:true,data:{state:{status:'running',error:null},
  analysis:{run_status:'failed',last_error:{type:'BOUNDS',message:'Previous analysis error.'}},
  run_snapshot:{ok:true,data:{run_status:'running',completed_candidates:3,last_error:null}}}};
evaluate('render(detail,"workflow","wf_current")');
assert.equal(get('error-detail').textContent,'');
context.detail.data.run_snapshot.data.run_status='failed';
context.detail.data.run_snapshot.data.last_error={type:'CurrentFailure',message:'Current checkpoint error.'};
evaluate('render(detail,"workflow","wf_current")');
assert(get('error-detail').textContent.includes('Current checkpoint error.'));
assert(!get('error-detail').textContent.includes('Previous analysis error.'));
''')

    def test_hidden_recovery_keeps_counts_countdown_and_untrusted_text_safe(self):
        self.run_dom(r'''
context.snapshot=JSON.parse(JSON.stringify(snapshot));
const injection='<img src=x onerror=window.pwned=1>';
context.snapshot.reason_zh=injection;context.snapshot.events[0].message=injection;
evaluate('state.kind="workflow";state.selected="wf_case";state.job="job_active";state.jobWorkflowId="wf_case";state.busy=true;text("error-detail","HTTPError · HTTP500");renderRecovery(snapshot,"workflow","wf_case")');
assert(get('recovery-panel').classList.contains('hidden'));
assert.equal(get('recovery-status').textContent,'等待自动重试');
assert.equal(get('status').textContent,'等待自动重试');
assert(get('error-detail').textContent.startsWith('上次错误'));
assert.equal(get('recovery-attempt').textContent,'1 / 3');
assert.equal(get('recovery-total').textContent,'2 / 8');
assert.equal(get('recovery-http').textContent,'4 / 12');
assert(get('recovery-progress').textContent.includes('44 / 100'));
assert(get('recovery-progress').textContent.includes('56'));
assert(get('recovery-next').textContent.includes('秒后'));
assert.equal(get('recovery-reason').textContent,injection);
assert(get('recovery-events').textContent.includes(injection));
assert(!createdTags.includes('img'));assert.equal(calls.length,0);
assert(!get('stop-recovery').disabled);
const now=Date.parse('2026-11-07T00:00:00Z');context.fixedNow=now;
assert(evaluate('recoveryCountdown("2026-11-07T00:00:10.100Z",fixedNow)').includes('11 秒后'));
assert(evaluate('recoveryCountdown("2026-11-06T00:00:00Z",fixedNow)').includes('等待后台执行'));
assert(evaluate('recoveryCountdown(null,fixedNow)').includes('等待后台安排'));
context.snapshot.status='retrying';evaluate('renderRecovery(snapshot,"workflow","wf_case")');
assert.equal(get('status').textContent,'自动恢复中');
context.snapshot.status='retry_failed';evaluate('badge("failed");renderRecovery(snapshot,"workflow","wf_case")');
assert.equal(get('recovery-status').textContent,'本次恢复尝试未成功');
assert.equal(get('status').textContent,'失败');
context.snapshot.status='needs_review';evaluate('badge("failed");renderRecovery(snapshot,"workflow","wf_case")');
assert.equal(get('recovery-status').textContent,'需要人工检查');
assert(get('stop-recovery').classList.contains('hidden'));
evaluate('renderRecovery(snapshot,"run","run_case")');
assert(get('recovery-panel').classList.contains('hidden'));
evaluate('renderRecovery(undefined,"workflow","wf_case")');
assert(get('recovery-panel').classList.contains('hidden'));
context.snapshot.status='retrying';
context.document.getElementById=id=>['status','error-detail'].includes(id)?null:get(id);
evaluate('renderRecovery(snapshot,"workflow","wf_case")');
''')

    def test_page_initialization_and_waiting_polling_make_only_get_requests(self):
        self.run_dom(r'''
workflowRoutes(snapshot,'job_active');
route('GET','/api/jobs/job_active',{ok:true,job:{job_id:'job_active',status:'running',workflow_id:'wf_case',result:null}});
await vm.runInContext(code.slice(bootstrap),context);
await evaluate('refresh()');await evaluate('refresh()');
assert(get('auto-recover').checked);
assert(calls.length>0);assert(calls.every(call=>call.method==='GET'));
assert.equal(evaluate('state.selected'),'wf_case');
assert.equal(get('recovery-status').textContent,'等待自动重试');
assert(get('resume-preview').disabled);
assert(!get('stop-recovery').classList.contains('hidden'));
assert.equal(get('connection').textContent,'● 本地已连接');
''')

    def test_unknown_job_after_server_restart_retains_workflow_and_does_not_resume(self):
        self.run_dom(r'''
workflowRoutes(snapshot,null);
route('GET','/api/jobs/job_old',{ok:false,error:{code:'JOB_NOT_FOUND',message:'Unknown job.'}},404);
evaluate('state.kind="workflow";state.selected="wf_case";state.job="job_old";state.jobWorkflowId="wf_case";state.busy=true');
await evaluate('refresh()');await evaluate('refresh()');
assert.equal(evaluate('state.job'),null);
assert.equal(evaluate('state.selected'),'wf_case');
assert.equal(evaluate('state.kind'),'workflow');
assert(!get('resume-preview').disabled);
assert.equal(get('recovery-status').textContent,'等待自动重试');
assert.equal(get('status').textContent,'失败');
assert(get('recovery-reason').textContent.includes('等待记录已保留'));
assert(get('recovery-reason').textContent.includes('恢复预览'));
assert(get('recovery-next').textContent.includes('等待显式恢复授权'));
assert.equal(get('connection').textContent,'● 本地已连接');
assert.equal(calls.filter(call=>call.path==='/api/jobs/job_old').length,1);
assert(calls.every(call=>call.method==='GET'));
assert(get('message').textContent.includes('恢复预览'));
''')

    def test_workflow_resume_opens_the_unified_assistant_preview(self):
        self.run_dom(r'''
evaluate('refresh=async()=>{};state.token="fixture-csrf"');
const previews=[];
context.diskAssistant={previewWorkflow:async id=>previews.push(id)};
evaluate('state.kind="workflow";state.selected="wf_case"');
await get('resume-preview').listeners.click();
assert.deepEqual(previews,['wf_case']);
assert.equal(calls.length,0);
''')

    def test_stop_recovery_requires_waiting_job_ownership_and_sends_one_csrf_post(self):
        self.run_dom(r'''
context.snapshot=snapshot;
evaluate('refresh=async()=>{};state.token="fixture-csrf";state.kind="workflow";state.selected="wf_case";state.job="job_active";state.jobWorkflowId="wf_other";renderRecovery(snapshot,"workflow","wf_case")');
await evaluate('stopRecovery()');assert.equal(calls.length,0);
evaluate('state.jobWorkflowId="wf_case";renderRecovery(snapshot,"workflow","wf_case")');
route('POST','/api/jobs/job_active/stop-recovery',{ok:true,stop_requested:true});
await evaluate('stopRecovery()');await evaluate('stopRecovery()');
assert.equal(calls.length,1);
assert.equal(calls[0].path,'/api/jobs/job_active/stop-recovery');
assert.deepEqual(JSON.parse(calls[0].body),{});
assert.equal(calls[0].headers['X-CSRF-Token'],'fixture-csrf');
assert(get('stop-recovery').disabled);
assert.equal(get('stop-recovery').textContent,'已请求停止自动重试');
''')


if __name__ == '__main__':
    unittest.main()
