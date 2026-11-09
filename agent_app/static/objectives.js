'use strict';

(() => {
    const element = id => document.getElementById(id);
    const metrics = [
        {key: 'stress_mpa', label: '最大等效应力', unit: 'MPa', direction: 'minimize'},
        {key: 'mass_kg', label: '整盘质量', unit: 'kg', direction: 'minimize'},
        {key: 'frequency_hz', label: '指定模态频率', unit: 'Hz', direction: 'maximize'}
    ];
    const panel = element('objective-fields');

    for (const metric of metrics) {
        const card = document.createElement('fieldset');
        card.className = 'objective-fieldset';
        const options = metric.key === 'frequency_hz' ? '<option value="outside_band">避让频带</option>' : '';
        card.innerHTML = `
            <legend>${metric.label} · ${metric.unit}</legend>
            <label>优化方向
                <select id="role-${metric.key}">
                    <option value="off">关闭</option><option value="monitor">仅观测</option>
                    <option value="minimize">最小化</option><option value="maximize">最大化</option>
                </select>
            </label>
            <label>目标权重
                <input id="weight-${metric.key}" type="range" min="0.1" max="1" step="0.1" value="0.5">
                <output id="weight-label-${metric.key}">0.5</output>
            </label>
            <label>约束
                <select id="constraint-${metric.key}">
                    <option value="">未配置</option><option value="<=">不超过上限</option>
                    <option value=">=">不低于下限</option>${options}
                </select>
            </label>
            <label>阈值参照
                <select id="reference-${metric.key}">
                    <option value="absolute">绝对值 · ${metric.unit}</option>
                    <option value="baseline_ratio">相对基准 · %</option>
                </select>
            </label>
            <label>限值 / 频带下限
                <input id="limit-${metric.key}" type="number" min="0" step="any" placeholder="待填写">
            </label>
            <label>频带上限
                <input id="upper-${metric.key}" type="number" min="0" step="any" placeholder="频率避让时填写" ${metric.key === 'frequency_hz' ? '' : 'disabled'}>
            </label>`;
        panel.append(card);
    }
    element('role-stress_mpa').value = 'minimize';
    element('role-mass_kg').value = 'monitor';

    window.optimizationDraft = () => {
        const fields = element('objective-policy').querySelectorAll('input, select');
        return Object.fromEntries(Array.from(fields, field => [field.id, field.type === 'checkbox' ? field.checked : field.value]));
    };
    let restoredPolicy = null;
    let restoredDraft = null;

    window.loadOptimizationPolicy = policy => {
        element('enable-objectives').checked = policy !== null;
        if (policy === null) {
            restoredPolicy = null;
            restoredDraft = null;
            return;
        }
        for (const metric of metrics) {
            const objective = policy?.objectives.find(item => item.metric === metric.key);
            const constraint = policy?.constraints.find(item => item.metric === metric.key);
            const observed = metric.key === 'stress_mpa' || (metric.key === 'mass_kg' ? policy?.collect_mass : policy?.modal);
            element('role-' + metric.key).value = objective?.direction || (observed ? 'monitor' : 'off');
            if (objective) element('weight-' + metric.key).value = objective.weight;
            element('weight-label-' + metric.key).textContent = element('weight-' + metric.key).value;
            element('constraint-' + metric.key).value = constraint?.operator || '';
            element('reference-' + metric.key).value = constraint?.reference || 'absolute';
            const scale = constraint?.reference === 'baseline_ratio' ? 100 : 1;
            element('limit-' + metric.key).value = constraint ? constraint.value * scale : '';
            element('upper-' + metric.key).value = constraint?.upper != null ? constraint.upper * scale : '';
        }
        element('constraint-source').value = policy?.constraints[0]?.source || '';
        if (policy?.modal) {
            element('modal-analysis-name').value = policy.modal.analysis_name;
            element('modal-order').value = policy.modal.mode_index;
        }
        // Preserve policies with multiple rules or distinct evidence sources
        // while their restored controls remain untouched.
        restoredPolicy = policy === null ? null : JSON.parse(JSON.stringify(policy));
        restoredDraft = JSON.stringify(window.optimizationDraft());
    };

    function positive(id, label) {
        const raw = element(id).value.trim();
        const value = Number(raw);
        if (!raw || !Number.isFinite(value) || value <= 0) {
            throw new Error(label + '需要填写正数');
        }
        return value;
    }

    window.readOptimizationPolicy = () => {
        if (!element('enable-objectives').checked) return null;
        if (restoredPolicy && restoredDraft === JSON.stringify(window.optimizationDraft())) {
            return JSON.parse(JSON.stringify(restoredPolicy));
        }
        if (window.diskEngineeringCapabilities?.mass !== 'axisymmetric_revolved_section_v1') {
            throw new Error('当前服务尚未加载质量与模态接口，请使用新版工作台地址。');
        }
        const objectives = [];
        const constraints = [];
        let collectMass = element('role-mass_kg').value !== 'off';
        let collectModal = element('role-frequency_hz').value !== 'off';
        const source = element('constraint-source').value.trim() || '探索性设置，工程限值来源待确认';
        for (const metric of metrics) {
            const role = element('role-' + metric.key).value;
            if (['minimize', 'maximize'].includes(role)) {
                objectives.push({metric: metric.key, direction: role,
                                 weight: positive('weight-' + metric.key, metric.label + '权重')});
            }
            const operator = element('constraint-' + metric.key).value;
            if (!operator) continue;
            const reference = element('reference-' + metric.key).value;
            const divisor = reference === 'baseline_ratio' ? 100 : 1;
            const value = positive('limit-' + metric.key, metric.label + '限值') / divisor;
            const upper = operator === 'outside_band' ? positive('upper-' + metric.key, '频带上限') / divisor : null;
            if (upper !== null && upper <= value) throw new Error('频带上限应大于下限');
            constraints.push({metric: metric.key, operator, reference, value, upper, source});
            collectMass ||= metric.key === 'mass_kg';
            collectModal ||= metric.key === 'frequency_hz';
        }
        if (!objectives.length) throw new Error('至少选择一个优化目标');
        let modal = null;
        if (collectModal) {
            const name = element('modal-analysis-name').value.trim();
            const order = positive('modal-order', '模态阶次');
            if (!name || !Number.isInteger(order) || order > 6) {
                throw new Error('请填写已有模态分析名称及 1～6 的整数阶次');
            }
            if (window.diskEngineeringCapabilities.modal_profile !== name) {
                throw new Error('当前服务尚未加载这个已核验的预应力模态工程。');
            }
            modal = {analysis_name: name, mode_index: order, family: 'axisymmetric_fixed_order', prestress_required: true};
        }
        return {schema_version: 'disk.optimization-policy.v1', objectives, constraints,
                collect_mass: collectMass, modal};
    };

    element('objective-policy').addEventListener('input', () => {
        for (const metric of metrics) {
            element('weight-label-' + metric.key).textContent = element('weight-' + metric.key).value;
        }
        window.clearPreview?.();
    });

    element('objective-template').addEventListener('change', event => {
        element('enable-objectives').checked = true;
        const template = event.target.value;
        element('role-stress_mpa').value = template === 'mass' ? 'monitor' : 'minimize';
        element('role-mass_kg').value = template === 'stress' ? 'monitor' : 'minimize';
        window.clearPreview?.();
    });

    element('save-objective-draft').addEventListener('click', () => {
        try {
            localStorage.setItem('diskagent.objective-draft.v1', JSON.stringify(window.optimizationDraft()));
            element('objective-draft-status').textContent = '配置草稿已保存在当前浏览器';
        } catch (error) {
            element('objective-draft-status').textContent = error.message;
        }
    });

    element('load-objective-draft').addEventListener('click', () => {
        try {
            const raw = localStorage.getItem('diskagent.objective-draft.v1');
            if (!raw) throw new Error('当前浏览器尚无配置草稿');
            const values = JSON.parse(raw);
            for (const [id, value] of Object.entries(values)) {
                const field = element(id);
                if (!field || !element('objective-policy').contains(field)) continue;
                if (field.type === 'checkbox') field.checked = value === true;
                else field.value = value;
            }
            element('objective-policy').dispatchEvent(new Event('input'));
            element('objective-draft-status').textContent = '已载入配置草稿，提交前可继续调整';
        } catch (error) {
            element('objective-draft-status').textContent = error.message;
        }
    });

    const statuses = {feasible: '满足当前配置', infeasible: '超限', missing: '数据缺失'};
    const checkStatuses = {passed: '通过', failed: '超限', missing: '数据缺失'};
    const format = value => Number.isFinite(value) ? value.toFixed(5) : '未采集';

    function constraintText(check) {
        const unit = check.margin_unit || metrics.find(metric => metric.key === check.metric)?.unit || '';
        const rule = check.operator === 'outside_band' ?
            `避让 (${format(check.limit)}, ${format(check.upper_limit)}) ${unit}` :
            `${check.operator} ${format(check.limit)} ${unit}`;
        return `${check.metric} ${rule}，实际 ${format(check.actual)} ${unit}，裕量 ${format(check.margin)} ${unit}，${checkStatuses[check.status] || check.status}，依据 ${check.source}`;
    }

    function showSelections(container, selections) {
        container.replaceChildren();
        if (!selections) {
            container.textContent = '读取已提交记录后展示三类方案及评价配置。';
            return;
        }
        const policy = document.createElement('p');
        policy.className = 'selection-policy';
        const source = selections.policy_source === 'saved_optimization_policy' ?
            '运行保存的目标与约束' : '历史默认应力目标';
        policy.textContent = `配置版本 ${selections.policy_id} · ${source} · 已启用数值约束 ${selections.constraint_count} 项`;
        container.append(policy);
        const grid = document.createElement('div');
        grid.className = 'selection-grid';
        for (const [title, design] of [['最小应力方案', selections.minimum_stress],
                                        ['当前配置推荐方案', selections.recommended]]) {
            const card = document.createElement('section');
            card.className = 'selection-card';
            const heading = document.createElement('h3');
            heading.textContent = title;
            card.append(heading);
            const description = document.createElement('p');
            if (!design) {
                description.textContent = title === '当前配置推荐方案' ?
                    '暂无可行推荐，超限或数据缺失记录可在“目标与可行方案”中查看。' : '尚无已提交计算记录。';
                card.append(description);
                grid.append(card);
                continue;
            }
            const evaluation = design.evaluation;
            description.textContent = `记录 ${design.iteration} · ${statuses[evaluation.status]}`;
            card.append(description);
            for (const metric of metrics) {
                const value = document.createElement('p');
                value.textContent = `${metric.label} ${format(design.observation[metric.key])} ${metric.unit}`;
                card.append(value);
            }
            const score = document.createElement('p');
            score.textContent = `归一化评分 ${format(evaluation.score)}`;
            card.append(score);
            if (evaluation.missing_metrics.length) {
                const missing = document.createElement('p');
                missing.textContent = '缺少评价数据 ' + evaluation.missing_metrics.join('、');
                card.append(missing);
            }
            if (evaluation.constraint_checks.length) {
                const checks = document.createElement('details');
                const label = document.createElement('summary');
                label.textContent = '约束判定、裕量与依据';
                checks.append(label);
                for (const check of evaluation.constraint_checks) {
                    const rule = document.createElement('p');
                    rule.textContent = constraintText(check);
                    checks.append(rule);
                }
                card.append(checks);
            } else {
                const note = document.createElement('p');
                note.textContent = '当前配置未启用数值约束';
                card.append(note);
            }
            if (title === '当前配置推荐方案') {
                const dimensions = document.createElement('details');
                const label = document.createElement('summary');
                label.textContent = '查看推荐方案尺寸';
                const values = document.createElement('p');
                values.textContent = Object.entries(design.observation.parameters || {}).map(
                    ([name, value]) => `${name}=${format(value)} mm`).join('，');
                dimensions.append(label, values);
                card.append(dimensions);
            }
            grid.append(card);
        }
        const pareto = document.createElement('section');
        pareto.className = 'selection-card';
        const heading = document.createElement('h3');
        heading.textContent = 'Pareto 方案集';
        const description = document.createElement('p');
        description.textContent = selections.pareto.length ?
            `当前配置下 ${selections.pareto.length} 个可行非支配方案，可按取舍选择。` : '暂无可行 Pareto 方案。';
        pareto.append(heading, description);
        const list = document.createElement('div');
        list.className = 'selection-pareto';
        for (const design of selections.pareto) {
            const item = document.createElement('details');
            const label = document.createElement('summary');
            label.textContent = `记录 ${design.iteration} · ${statuses[design.evaluation.status]} · 评分 ${format(design.evaluation.score)}`;
            item.append(label);
            for (const metric of metrics) {
                const value = document.createElement('p');
                value.textContent = `${metric.label} ${format(design.observation[metric.key])} ${metric.unit}`;
                item.append(value);
            }
            for (const check of design.evaluation.constraint_checks) {
                const rule = document.createElement('p');
                rule.textContent = constraintText(check);
                item.append(rule);
            }
            list.append(item);
        }
        pareto.append(list);
        grid.append(pareto);
        container.append(grid);
        const note = document.createElement('p');
        note.className = 'hint';
        note.textContent = '推荐按当前配置的硬约束与加权归一化目标确定。裕量正值表示尚有余量，负值表示超限，零表示边界。';
        container.append(note);
    }

    window.renderDesignSelection = selections => showSelections(element('design-selection'), selections);

    function showArchive(container, data) {
        container.replaceChildren();
        if (!data) {
            container.textContent = '选择带配置目标的运行，或用上方面板重评已有结果。';
            return;
        }
        const summary = document.createElement('p');
        summary.textContent = `${data.run_id || ''} · 配置 ${data.policy_id} · 可行记录 ${data.feasible_count} · 推荐记录 ${data.best_iteration ?? '暂无'} · Pareto记录 ${data.pareto_iterations.join('、') || '暂无'}`;
        container.append(summary);
        const table = document.createElement('table');
        table.className = 'prediction-table';
        const header = table.createTHead().insertRow();
        for (const label of ['记录', '应力 MPa', '质量 kg', '频率 Hz', '状态', '评分', '选择', '六阶频率 Hz']) {
            const th = document.createElement('th');
            th.textContent = label;
            header.append(th);
        }
        const body = table.createTBody();
        const observations = data.observations || [];
        const minimum = observations.reduce((best, point, i) => Number.isFinite(point.stress_mpa) &&
            (best === null || point.stress_mpa < observations[best].stress_mpa) ? i : best, null);
        for (const evaluation of data.evaluations) {
            const point = observations[evaluation.iteration] || {};
            const row = body.insertRow();
            const roles = [];
            if (evaluation.iteration === minimum) roles.push('最小应力');
            if (evaluation.iteration === data.best_iteration) roles.push('目标推荐');
            if (data.pareto_iterations.includes(evaluation.iteration)) roles.push('Pareto');
            const selected = roles.join(' / ') || '—';
            for (const value of [evaluation.iteration, format(point.stress_mpa), format(point.mass_kg),
                                 format(point.frequency_hz), statuses[evaluation.status], format(evaluation.score), selected]) {
                row.insertCell().textContent = String(value);
            }
            row.title = evaluation.constraint_checks.map(constraintText).join('\n');
            const modes = row.insertCell();
            if (Array.isArray(point.modal_frequencies_hz)) {
                const details = document.createElement('details');
                const label = document.createElement('summary');
                label.textContent = '已采集六阶';
                const values = document.createElement('pre');
                values.textContent = point.modal_frequencies_hz.map((value, i) => `f${i + 1} ${format(value)} Hz`).join('\n');
                details.append(label, values);
                modes.append(details);
            } else {
                modes.textContent = '未采集';
            }
        }
        const wrap = document.createElement('div');
        wrap.className = 'prediction-table-wrap';
        wrap.append(table);
        container.append(wrap);
        const config = document.createElement('pre');
        config.className = 'code';
        config.textContent = JSON.stringify(data.policy, null, 2);
        const details = document.createElement('details');
        const label = document.createElement('summary');
        label.textContent = '查看评价使用的完整配置';
        details.append(label, config);
        container.append(details);
        const checks = document.createElement('details');
        const checksLabel = document.createElement('summary');
        checksLabel.textContent = '查看逐项约束判定与依据';
        const checksContent = document.createElement('pre');
        checksContent.className = 'code';
        checksContent.textContent = data.evaluations.map(evaluation => {
            const rules = evaluation.constraint_checks.map(constraintText);
            return `记录 ${evaluation.iteration} / ${statuses[evaluation.status]}\n${rules.join('\n') || '约束未配置'}`;
        }).join('\n\n');
        checks.append(checksLabel, checksContent);
        container.append(checks);
        if (data.design_selection) {
            const selections = document.createElement('div');
            showSelections(selections, data.design_selection);
            container.append(selections);
        }
    }

    let previousRunId = null;
    window.renderObjectiveArchive = data => {
        if (previousRunId !== window.objectiveRunId) {
            element('objective-local-preview').replaceChildren();
            previousRunId = window.objectiveRunId;
        }
        showArchive(element('objective-results'), data);
    };
    element('reevaluate-objectives').addEventListener('click', async () => {
        const button = element('reevaluate-objectives');
        try {
            button.disabled = true;
            const policy = window.readOptimizationPolicy();
            if (!policy) throw new Error('请先启用目标与约束配置');
            const runId = window.objectiveRunId || element('context').value;
            if (!runId) throw new Error('请先选择一个已有运行');
            const response = await window.api('/api/runs/' + encodeURIComponent(runId) + '/objectives', {
                method: 'POST', body: JSON.stringify({optimization_policy: policy})
            });
            if (runId !== (window.objectiveRunId || element('context').value)) return;
            showArchive(element('objective-local-preview'), response.data);
            element('objective-draft-status').textContent = '已按当前设置重评已有记录';
        } catch (error) {
            element('objective-draft-status').textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });
})();
