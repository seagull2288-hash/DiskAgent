'use strict';

(() => {
    const get = id => document.getElementById(id);
    const format = value => Number.isFinite(value) ? value.toFixed(6) : '待配置';
    function table(container, headings, rows) {
        const node = document.createElement('table');
        node.className = 'prediction-table';
        const header = node.createTHead().insertRow();
        for (const heading of headings) {
            const cell = document.createElement('th');
            cell.textContent = heading;
            header.append(cell);
        }
        const body = node.createTBody();
        for (const values of rows) {
            const row = body.insertRow();
            for (const value of values) row.insertCell().textContent = String(value);
        }
        const wrap = document.createElement('div');
        wrap.className = 'prediction-table-wrap';
        wrap.append(node);
        container.append(wrap);
    }

    async function refresh() {
        get('refresh-mesh-study').disabled = true;
        try {
            const response = await window.api('/api/mesh-study');
            const data = response.data;
            const container = get('mesh-study-results');
            container.replaceChildren();
            if (!data) {
                get('mesh-study-status').textContent = '尚无网格验证记录。';
                return;
            }
            const labels = {running: '正在计算', completed: '计算完成', failed: '部分案例计算失败'};
            get('mesh-study-status').textContent = `${data.study_id} · ${labels[data.status] || data.status} · 已完成 ${data.cases.length}/${data.request.design_evaluation_tasks} 个案例`;
            table(container, ['设计', '网格 mm', '节点', '单元', '应力 MPa', '质量 kg',
                'f1 Hz', 'f2 Hz', 'f3 Hz', 'f4 Hz', 'f5 Hz', 'f6 Hz'], data.cases.map(point => [
                    point.label, point.mesh_size_mm, point.node_count, point.element_count,
                    format(point.observation.stress_mpa), format(point.observation.mass_kg),
                    ...point.observation.modal_frequencies_hz.map(format)]));
            const note = document.createElement('p');
            note.className = 'hint';
            note.textContent = '数值验收相对容差 ' + format(data.relative_tolerance) +
                ' · 固定阶次预应力轴对称模态 · 源工程哈希核对 ' +
                (data.source_preserved === true ? '通过' : data.status === 'running' ? '计算结束后核对' : '见原始记录');
            container.append(note);
            if (data.comparisons?.length) {
                table(container, ['设计', '指标', '前两级变化 %', '后两级变化 %', '变化幅度'], data.comparisons.map(item => [
                    item.label, item.metric, ...item.relative_changes.map(value => format(value * 100)),
                    item.changes_decreasing ? '缩小' : '未持续缩小']));
            }
            if (data.error) {
                const error = document.createElement('p');
                error.textContent = data.error;
                container.append(error);
            }
            const details = document.createElement('details');
            const heading = document.createElement('summary');
            heading.textContent = '工况、实际网格与原生证据';
            const evidence = document.createElement('pre');
            evidence.className = 'code';
            evidence.textContent = JSON.stringify(data, null, 2);
            details.append(heading, evidence);
            container.append(details);
        } catch (error) {
            get('mesh-study-status').textContent = error.message;
        } finally {
            get('refresh-mesh-study').disabled = false;
        }
    }
    get('refresh-mesh-study').addEventListener('click', refresh);
})();
