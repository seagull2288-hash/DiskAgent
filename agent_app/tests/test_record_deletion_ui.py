"""Context menu confirmation and state updates in an isolated DOM."""
import unittest

from agent_app.tests import test_recovery_ui as fixture


@unittest.skipUnless(fixture.NODE, 'Node is required for isolated browser logic tests.')
class RecordDeletionDOMTests(unittest.TestCase):
    run_dom = fixture.RecoveryDOMTests.run_dom

    def test_run_menu_renames_with_existing_name_and_keeps_selected_identity(self):
        self.run_dom(r'''
evaluate('state.kind="run";state.selected="run_target"');
const event={clientX:100,clientY:100,preventDefault(){},stopPropagation(){}};
context.event=event;
evaluate('showRecordMenu(event,"workflow",{workflow_id:"wf_case"})');
assert(get('rename-record').classList.contains('hidden'));
evaluate('showRecordMenu(event,"run",{run_id:"run_target",display_name:"原实验名称"})');
assert(!get('rename-record').classList.contains('hidden'));
assert.equal(evaluate('state.recordMenuTarget.display_name'),'原实验名称');
context.prompt=(_question,value)=>{assert.equal(value,'原实验名称');return '对比实验';};
let refreshes=0;
context.refresh=async()=>{refreshes++;};
route('POST','/api/runs/run_target/rename',{ok:true,run_id:'run_target',display_name:'对比实验'});
await evaluate('renameRecord()');
assert.equal(calls.length,1);
assert.equal(calls[0].path,'/api/runs/run_target/rename');
assert.deepEqual(JSON.parse(calls[0].body),{display_name:'对比实验'});
assert.equal(refreshes,1);
assert.equal(evaluate('state.selected'),'run_target');
assert.equal(evaluate('state.recordRenaming'),false);
''')

    def test_cancelled_run_rename_sends_no_request(self):
        self.run_dom(r'''
context.prompt=()=>null;
evaluate('state.kind="run";state.selected="run_test";state.recordMenuTarget={kind:"run",id:"run_test",label:"运行",display_name:"原名称"}');
await evaluate('renameRecord()');
assert.equal(calls.length,0);
assert.equal(evaluate('state.selected'),'run_test');
assert(!evaluate('state.recordRenaming'));
''')

    def test_rename_updates_same_run_context_and_renders_name_as_text(self):
        self.run_dom(r'''
const select=get('context');
select.replaceChildren=(...children)=>{select.children=children.flatMap(child=>child.tag==='fragment'?child.children:[child]);};
select.value='run_test';
evaluate('updateRunContexts([{run_id:"run_test",display_name:"旧名称"}])');
const before=select.children.find(option=>option.value==='run_test');
assert(before.textContent.includes('旧名称'));
evaluate('updateRunContexts([{run_id:"run_test",display_name:"新名称"}])');
assert(select.children.find(option=>option.value==='run_test').textContent.includes('新名称'));
assert.equal(select.value,'run_test');
const button=evaluate('listItem({run_id:"run_test",display_name:"<img src=x onerror=alert(1)>",status:"complete"},"run")');
assert.equal(button.children[0].textContent,'<img src=x onerror=alert(1)>');
assert.equal(button.children[0].children.length,0);
''')

    def test_right_click_opens_record_menu_without_changing_selection(self):
        self.run_dom(r'''
evaluate('state.kind="workflow";state.selected="wf_selected"');
const button=evaluate('listItem({run_id:"run_target",status:"complete",completed_candidates:3},"run")');
let prevented=false;
button.listeners.contextmenu({clientX:900,clientY:700,preventDefault:()=>{prevented=true;},stopPropagation:()=>{}});
assert(prevented);
assert(!get('record-context-menu').classList.contains('hidden'));
assert.equal(get('delete-record').textContent,'删除此运行');
assert.equal(get('record-menu-title').textContent,'run_target');
assert.equal(evaluate('state.selected'),'wf_selected');
assert.equal(calls.length,0);
evaluate('hideRecordMenu()');
assert(get('record-context-menu').classList.contains('hidden'));
assert.equal(evaluate('state.recordMenuTarget'),null);
''')

    def test_cancel_sends_no_delete_request(self):
        self.run_dom(r'''
context.confirm=()=>false;
evaluate('state.recordMenuTarget={kind:"workflow",id:"wf_test",label:"工作流"}');
await evaluate('deleteRecord()');
assert.equal(calls.length,0);
assert.equal(evaluate('state.recordDeleting'),false);
''')

    def test_confirmed_run_delete_clears_selected_context_and_preview(self):
        self.run_dom(r'''
context.confirm=()=>true;
context.refresh=async()=>{};
get('context').value='run_test';
evaluate('state.kind="run";state.selected="run_test";state.recordMenuTarget={kind:"run",id:"run_test",label:"运行"}');
let invalidated=0;context.diskAssistant={invalidate:()=>invalidated++};
route('POST','/api/runs/run_test/delete',{ok:true,deleted:true});
await evaluate('deleteRecord()');
assert.equal(calls.length,1);
assert.equal(calls[0].method,'POST');
assert.equal(calls[0].path,'/api/runs/run_test/delete');
assert.equal(calls[0].body,'{}');
assert.equal(get('context').value,'');
assert.equal(evaluate('state.selected'),null);
assert.equal(invalidated,1);
assert.equal(evaluate('state.recordDeleting'),false);
''')

    def test_rejected_deletion_preserves_current_selection(self):
        self.run_dom(r'''
context.confirm=()=>true;
evaluate('state.kind="run";state.selected="run_test";state.recordMenuTarget={kind:"run",id:"run_test",label:"运行"}');
route('POST','/api/runs/run_test/delete',{ok:false,error:{code:'RECORD_REFERENCED',message:'请先删除相关工作流'}},409);
await evaluate('deleteRecord()');
assert.equal(evaluate('state.selected'),'run_test');
assert.equal(evaluate('state.kind'),'run');
assert.equal(evaluate('state.recordDeleting'),false);
assert(get('message').textContent.includes('请先删除相关工作流'));
''')

    def test_busy_state_disables_deletion(self):
        self.run_dom(r'''
context.confirm=()=>{throw new Error('Busy task cannot open confirmation.');};
evaluate('state.busy=true;showRecordMenu({clientX:100,clientY:100,preventDefault(){},stopPropagation(){}},"workflow",{workflow_id:"wf_busy"})');
assert(get('delete-record').disabled);
await evaluate('deleteRecord()');
assert.equal(calls.length,0);
''')


if __name__ == '__main__':
    unittest.main()
