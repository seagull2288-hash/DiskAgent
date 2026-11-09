"""Independent historical mass card and committed-snapshot precedence."""
import unittest

from agent_app.tests import test_recovery_ui as fixture


class MassSummaryMarkupTests(unittest.TestCase):
    def test_mass_card_follows_stress_card_in_summary_row(self):
        html = fixture.PAGE.read_text(encoding='utf-8')
        self.assertIn('class="cards summary-cards"', html)
        self.assertIn('历史最小质量', html)
        self.assertLess(html.index('id="best-stress"'), html.index('id="best-mass"'))
        self.assertLess(html.index('id="best-mass"'), html.index('id="improvement"'))


@unittest.skipUnless(fixture.NODE, 'Node is required for isolated browser logic tests.')
class MassSummaryDOMTests(unittest.TestCase):
    run_dom = fixture.RecoveryDOMTests.run_dom

    def test_card_uses_independent_mass_and_displays_its_candidate(self):
        self.run_dom(r'''
context.prepareOptimizationRun=()=>{};context.preparePredictionRun=()=>{};
context.detail={ok:true,data:{run_status:'complete',completed_candidates:2,
  best:{stress_mpa:600,mass_kg:35},minimum_mass:{iteration:1,mass_kg:27.612156}}};
evaluate('render(detail,"run","run_mass")');
assert.equal(get('best-stress').textContent,'600.00');
assert.equal(get('best-mass').textContent,'27.612');
assert(get('best-mass-source').textContent.includes('候选 1'));
''')

    def test_live_snapshot_updates_a_stale_batch_analysis(self):
        self.run_dom(r'''
context.prepareOptimizationRun=()=>{};context.preparePredictionRun=()=>{};
context.detail={ok:true,data:{state:{status:'running'},analysis:{minimum_mass:{iteration:1,mass_kg:32}},
  run_snapshot:{ok:true,data:{completed_candidates:3,minimum_mass:{iteration:3,mass_kg:29.5}}}}};
evaluate('render(detail,"workflow","wf_mass")');
assert.equal(get('best-mass').textContent,'29.500');
assert(get('best-mass-source').textContent.includes('候选 3'));
evaluate('render({ok:true,data:{analysis:{minimum_mass:{iteration:1,mass_kg:32}},run_snapshot:{ok:true,data:{minimum_mass:null}}}},"workflow","wf_without_mass")');
assert.equal(get('best-mass').textContent,'—');
''')

    def test_baseline_and_missing_mass_reset_previous_display(self):
        self.run_dom(r'''
context.prepareOptimizationRun=()=>{};context.preparePredictionRun=()=>{};
evaluate('render({ok:true,data:{minimum_mass:{iteration:0,mass_kg:36}}},"run","run_baseline")');
assert.equal(get('best-mass').textContent,'36.000');
assert(get('best-mass-source').textContent.includes('基准设计'));
evaluate('render({ok:true,data:{minimum_mass:null,best:{mass_kg:20}}},"run","run_old")');
assert.equal(get('best-mass').textContent,'—');
assert(get('best-mass-source').textContent.includes('尚无质量实测'));
''')


@unittest.skipUnless(fixture.NODE, 'Node is required for isolated browser logic tests.')
class MassChartDOMTests(unittest.TestCase):
    run_dom = fixture.RecoveryDOMTests.run_dom

    def test_mass_archive_and_stress_samples_render_independent_curves(self):
        self.run_dom(r'''
context.analysis={baseline:{iteration:0,stress_mpa:740},
  candidates:[{iteration:1,stress_mpa:600},{iteration:2,stress_mpa:650}],
  optimization:{observations:[{iteration:0,mass_kg:36},{iteration:1,mass_kg:34},{iteration:2,mass_kg:35}]}};
evaluate('plot(analysis)');
const stress=get('chart').children[0],mass=get('mass-chart').children[0];
assert.equal(stress.tag,'svg');assert.equal(mass.tag,'svg');
assert.equal(stress.children.filter(el=>el.tag==='circle').length,3);
assert.equal(mass.children.filter(el=>el.tag==='circle').length,3);
assert(mass['aria-label'].includes('kg'));
const best=mass.children.find(el=>el.tag==='polyline'&&el['stroke-dasharray']);
const values=best.points.split(' ').map(point=>point.split(',').map(Number));
assert.equal(values[1][1],values[2][1]);
assert(mass.children.find(el=>el.tag==='circle').textContent.includes('基准'));
assert.equal(calls.length,0);
''')

    def test_missing_mass_leaves_gaps_and_keeps_shared_iteration_axis(self):
        self.run_dom(r'''
context.analysis={baseline:{iteration:0,stress_mpa:740},
  candidates:[1,2,3,4,5].map(iteration=>({iteration,stress_mpa:700-iteration})),
  optimization:{observations:[{iteration:0,mass_kg:36},{iteration:1,mass_kg:null},
    {iteration:2,mass_kg:32},{iteration:3,mass_kg:0},{iteration:4,mass_kg:31},{iteration:5}]}};
evaluate('plot(analysis)');
const stress=get('chart').children[0],mass=get('mass-chart').children[0];
const dots=mass.children.filter(el=>el.tag==='circle');
assert.equal(dots.length,3);
const lines=mass.children.filter(el=>el.tag==='polyline'&&!el['stroke-dasharray']);
assert.equal(lines.length,3);
assert(lines.every(line=>line.points.split(' ').length===1));
const end=stress.children.filter(el=>el.tag==='circle').at(-1);
assert(Number(dots.at(-1).cx)<Number(end.cx));
assert.equal(Number(end.cx),480);
for(const svg of [stress,mass])assert(svg.children.some(el=>el.tag==='text'&&el.textContent==='5'));
''')

    def test_switching_to_a_run_without_mass_clears_the_previous_chart(self):
        self.run_dom(r'''
evaluate('plot({baseline:{iteration:0,stress_mpa:740,mass_kg:36},candidates:[]})');
assert.equal(get('mass-chart').children[0].tag,'svg');
evaluate('plot({baseline:{iteration:0,stress_mpa:740},candidates:[{iteration:1,stress_mpa:700}]})');
assert.equal(get('mass-chart').children[0].tag,'div');
assert(get('mass-chart').textContent.includes('尚无质量实测'));
assert.equal(get('chart').children[0].tag,'svg');
''')


if __name__ == '__main__':
    unittest.main()
