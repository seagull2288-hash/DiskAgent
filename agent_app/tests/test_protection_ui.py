"""Diagnostic UI uses fixed routes and preserves evidence on cancelled restoration."""
import unittest

from agent_app.tests import test_recovery_ui as fixture


SETUP = r'''
context.window=context;
vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'protection.js'),'utf8'),context);
'''


@unittest.skipUnless(fixture.NODE, 'Node is required for isolated browser logic tests.')
class ProtectionDOMTests(unittest.TestCase):
    run_dom = fixture.RecoveryDOMTests.run_dom

    def test_failed_preflight_shows_every_check_without_submitting_optimization(self):
        self.run_dom(SETUP + r'''
route('POST','/api/preflight',{ok:false,summary:'发现1项失败',checks:[
  {label:'磁盘',status:'failed',message:'<script>literal evidence</script>'}]});
await get('preflight-new').listeners.click();
assert.equal(calls.length,1);
assert.equal(calls[0].path,'/api/preflight');
assert.equal(calls[0].body,'{}');
assert(get('preflight-summary').textContent.includes('发现1项失败'));
assert(get('preflight-checks').textContent.includes('<script>literal evidence</script>'));
assert(!createdTags.includes('script'));
''')

    def test_run_monitor_and_native_metric_names_are_shown(self):
        self.run_dom(SETUP + r'''
route('GET','/api/runs/run_fixture/backups',{ok:true,backups:[]});
route('GET','/api/runs/run_fixture/protection',{ok:true,monitor:{status:'delayed',stage_label:'模态求解',
  stage_elapsed_seconds:200,elapsed_seconds:300,last_progress_utc:'fixture-time',warning_limit_seconds:180},
  diagnostics_iteration:4,result_diagnostics:{mesh_quality:{status:'available',metrics:{
    element_quality:{minimum:.3,maximum:.9,mean:.7}}},stress_changes:[],warnings:[]}});
await context.refreshRunProtection('run_fixture');
assert(get('solver-stage').textContent.includes('模态求解'));
assert(get('solver-stage').textContent.includes('耗时提醒'));
assert(get('solver-stage-time').textContent.includes('200 秒'));
assert(get('engineering-diagnostics').textContent.includes('平均 0.7'));
assert(!get('engineering-diagnostics').textContent.includes('undefined'));
''')

    def test_cancelled_backup_preview_sends_no_restoration(self):
        self.run_dom(SETUP + r'''
route('GET','/api/runs/run_fixture/backups',{ok:true,backups:[{backup_id:'checkpoint_000004.json',
  completed_candidates:4,target_total_candidates:10,created_at:'fixture'}]});
route('GET','/api/runs/run_fixture/protection',{ok:true,monitor:{status:'completed'}});
route('POST','/api/runs/run_fixture/backups/preview',{ok:true,can_restore:true,completed_candidates:4,
  current_checkpoint_sha256:'fixture-digest',reason_zh:'核验通过'});
context.confirm=()=>false;
await context.refreshRunProtection('run_fixture');
await get('checkpoint-backups').children[0].children[1].listeners.click();
assert(calls.some(call=>call.path.endsWith('/preview')));
assert(!calls.some(call=>call.path.endsWith('/restore')));
''')

    def test_late_monitor_response_cannot_replace_the_newly_selected_run(self):
        self.run_dom(SETUP + r'''
let release;
context.api=async path=>{
  if(path.endsWith('/backups'))return {ok:true,backups:[]};
  if(path.includes('run_old'))return await new Promise(resolve=>{release=resolve;});
  return {ok:true,monitor:{status:'running',stage_label:'新运行'}};
};
const old=context.refreshRunProtection('run_old');
await context.refreshRunProtection('run_new');
release({ok:true,monitor:{status:'completed',stage_label:'旧运行'}});
await old;
assert(get('solver-stage').textContent.includes('新运行'));
assert(!get('solver-stage').textContent.includes('旧运行'));
''')
