"""Run-list selection and periodic dropdown refresh must retain explicit binding."""
import unittest

from agent_app.tests import test_recovery_ui as fixture


@unittest.skipUnless(fixture.NODE, 'Node is required for isolated browser logic tests.')
class RunContextDOMTests(unittest.TestCase):
    run_dom = fixture.RecoveryDOMTests.run_dom

    def test_run_list_selection_is_sent_to_preview_and_invalidates_old_preview(self):
        self.run_dom(r'''
evaluate('refresh=async()=>{}');
let invalidated=0;context.diskAssistant={invalidate:()=>invalidated++};
get('request').value='追加3个候选';
await evaluate('select("run","run_selected")');
assert.equal(get('context').value,'run_selected');
assert(get('context-binding').textContent.includes('run_selected'));
assert.equal(invalidated,1);
assert.equal(get('request').value,'追加3个候选');
assert.equal(calls.length,0);
''')

    def test_identical_run_poll_preserves_option_nodes_and_changed_list_keeps_binding(self):
        self.run_dom(r'''
const select=get('context');
select.replaceChildren=function(...children){
  this.children=children.flatMap(child=>child.tag==='fragment'?child.children:[child]);
};
context.runs=[{run_id:'run_first'},{run_id:'run_second'}];
evaluate('updateRunContexts(runs)');
assert.equal(select.children.length,3);
const first=select.children[1];select.value='run_first';
evaluate('updateRunContexts(runs)');
assert.strictEqual(select.children[1],first);
assert.equal(select.value,'run_first');
assert(get('context-binding').textContent.includes('run_first'));
context.runs.push({run_id:'run_third'});
evaluate('updateRunContexts(runs)');
assert.equal(select.value,'run_first');
context.runs=[{run_id:'run_third'}];
evaluate('updateRunContexts(runs)');
assert.equal(select.value,'run_first');
assert(select.children.some(option=>option.value==='run_first'));
select.value='';evaluate('updateRunContexts(runs)');
assert.equal(select.value,'');
''')


if __name__ == '__main__':
    unittest.main()
