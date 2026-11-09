(() => {
    const get = id => document.getElementById(id);
    const labels = {passed: '通过', warning: '提醒', failed: '失败', unknown: '待确认',
                    unavailable: '暂无记录', observed: '已记录', available: '已采集', partial: '部分可用'};
    let runId = null;
    let loading = false;
    let generation = 0;
    let backupBusy = false;

    function node(tag, value, className) {
        const result = document.createElement(tag);
        if (className) result.className = className;
        if (value != null) result.textContent = String(value);
        return result;
    }

    function duration(value) {
        return Number.isFinite(value) ? Math.round(value) + ' 秒' : '—';
    }

    async function preflight(existing) {
        const button = get(existing ? 'preflight-run' : 'preflight-new');
        const target = runId;
        if (existing && !target) return;
        button.disabled = true;
        get('preflight-summary').textContent = '正在检查文件、存储与任务配置…';
        get('preflight-checks').replaceChildren();
        try {
            const body = existing ? {run_id: target} : {};
            if (!existing) {
                const policy = window.readOptimizationPolicy?.();
                if (policy) body.optimization_policy = policy;
            }
            const report = await window.api('/api/preflight', {method: 'POST', body: JSON.stringify(body)});
            if (existing && target !== runId) return;
            get('preflight-summary').textContent = (existing ? '所选运行 · ' : '新任务 · ') + report.summary;
            const entries = (report.checks || []).map(check => {
                const row = node('div', null, 'recovery-event');
                row.append(node('strong', check.label + ' · ' + (labels[check.status] || check.status)),
                           node('div', check.message, 'recovery-details'));
                return row;
            });
            get('preflight-checks').replaceChildren(...entries);
        } catch (error) {
            get('preflight-summary').textContent = '体检未完成 · ' + error.message;
        } finally {
            button.disabled = existing && !runId;
        }
    }

    function diagnostics(data) {
        const holder = get('engineering-diagnostics');
        const entries = [];
        for (const check of data.geometry?.checks || []) {
            entries.push(node('div', check.label + ' · ' + (labels[check.status] || check.status) + ' · ' + check.message));
        }
        const result = data.result_diagnostics;
        if (result) {
            entries.push(node('strong', '实测记录 ' + data.diagnostics_iteration + ' 的结果诊断'));
            const quality = result.mesh_quality || {};
            entries.push(node('div', '网格统计 · ' + (labels[quality.status] || quality.status || '待确认') + ' · ' + (quality.message || '')));
            for (const [key, metric] of Object.entries(quality.metrics || {})) {
                entries.push(node('div', key + ' · 最小 ' + metric.minimum + '，最大 ' + metric.maximum + '，平均 ' + metric.mean));
            }
            for (const warning of result.warnings || []) entries.push(node('div', warning));
            for (const change of result.stress_changes || []) {
                const value = Number.isFinite(change.relative_change_percent) ? change.relative_change_percent.toFixed(2) + '%' : '待核对';
                entries.push(node('div', '相对' + (change.reference === 'previous' ? '上一实测' : '基准') + '应力变化 ' + value));
            }
            entries.push(node('div', '应力变化提醒线为25%，用作诊断提示。网格工程验收门槛待配置。'));
        }
        if (data.result_reclaimed) entries.push(node('div', '已核验并回收此前完成的求解结果。'));
        if (!entries.length) entries.push(node('div', '此运行暂无新增检查记录。新评价会保存几何与原生网格诊断。'));
        holder.replaceChildren(...entries);
    }

    async function readBackups() {
        const target = runId;
        if (!target || backupBusy) return;
        backupBusy = true;
        get('refresh-backups').disabled = true;
        try {
            const report = await window.api('/api/runs/' + encodeURIComponent(target) + '/backups');
            if (target !== runId) return;
            get('backup-status').textContent = report.diagnostic?.message || (report.warnings || []).join('\n') ||
                (report.backups?.length ? '已核验备份 ' + report.backups.length + ' 份。' : '尚无备份，新提交结果核验后会自动建立。');
            const rows = (report.backups || []).map(backup => {
                const row = node('div', null, 'row box');
                row.append(node('span', '已完成 ' + backup.completed_candidates + ' / ' + backup.target_total_candidates + ' · ' + backup.created_at));
                const button = node('button', '核验恢复', 'small-button');
                button.addEventListener('click', () => restore(target, backup.backup_id, button));
                row.append(button);
                return row;
            });
            get('checkpoint-backups').replaceChildren(...rows);
        } catch (error) {
            if (target === runId) get('backup-status').textContent = '读取备份失败 · ' + error.message;
        } finally {
            backupBusy = false;
            get('refresh-backups').disabled = !runId;
        }
    }

    async function restore(target, backupId, button) {
        if (backupBusy || target !== runId) return;
        backupBusy = true;
        button.disabled = true;
        try {
            const path = '/api/runs/' + encodeURIComponent(target) + '/backups/';
            const preview = await window.api(path + 'preview', {method: 'POST', body: JSON.stringify({backup_id: backupId})});
            if (target !== runId) return;
            get('backup-status').textContent = preview.reason_zh;
            if (!preview.can_restore) return;
            if (!window.confirm('恢复已核验断点？\n' + target + '\n已提交候选 ' + preview.completed_candidates + '\n' + preview.reason_zh)) return;
            const result = await window.api(path + 'restore', {method: 'POST', body: JSON.stringify({
                backup_id: backupId, expected_current_sha256: preview.current_checkpoint_sha256})});
            if (result.restored !== true) throw new Error('后台尚未确认断点恢复。');
            get('backup-status').textContent = '已恢复核验通过的断点，可重新预览续算任务。';
            window.clearPreview?.();
            await window.refresh();
        } catch (error) {
            get('backup-status').textContent = '断点核验或恢复失败 · ' + error.message;
        } finally {
            backupBusy = false;
            button.disabled = false;
        }
    }

    window.refreshRunProtection = async target => {
        if (target !== runId) {
            runId = target;
            generation += 1;
            loading = false;
            get('preflight-run').disabled = !target;
            get('refresh-backups').disabled = !target;
            get('checkpoint-backups').replaceChildren();
            get('backup-status').textContent = '';
            get('engineering-diagnostics').textContent = target ? '正在读取检查记录…' : '选择运行后读取检查记录。';
            if (target) readBackups();
        }
        if (!target) {
            get('solver-stage').textContent = '选择运行后查看求解阶段';
            get('solver-stage-time').textContent = '';
            get('solver-progress-time').textContent = '';
            get('solver-monitor-message').textContent = '';
            return;
        }
        if (loading) return;
        loading = true;
        const current = generation;
        try {
            const data = await window.api('/api/runs/' + encodeURIComponent(target) + '/protection');
            if (current !== generation || target !== runId) return;
            const monitor = data.monitor || {};
            const statuses = {running: '计算中', delayed: '耗时提醒', completed: '已完成', failed: '失败',
                              interrupted: '已中断', requesting: '生成候选中', verifying: '核验中', unavailable: '暂无监控记录'};
            get('solver-stage').textContent = (monitor.stage_label || '') + ' · ' + (statuses[monitor.status] || monitor.status || '待命');
            get('solver-stage-time').textContent = '阶段耗时 ' + duration(monitor.stage_elapsed_seconds) + ' · 总耗时 ' + duration(monitor.elapsed_seconds);
            get('solver-progress-time').textContent = monitor.last_progress_utc ? '最近进展 ' + monitor.last_progress_utc : '';
            get('solver-monitor-message').textContent = monitor.message || '';
            if (monitor.warning_limit_seconds) get('solver-monitor-message').textContent +=
                ' 当前阶段观察阈值 ' + duration(monitor.warning_limit_seconds) + '。';
            diagnostics(data);
            if (data.backup_status?.status === 'warning') get('backup-status').textContent = data.backup_status.message;
        } catch (error) {
            if (current === generation) get('solver-monitor-message').textContent = '读取运行保护记录失败 · ' + error.message;
        } finally {
            if (current === generation) loading = false;
        }
    };

    get('preflight-new').addEventListener('click', () => preflight(false));
    get('preflight-run').addEventListener('click', () => preflight(true));
    get('refresh-backups').addEventListener('click', readBackups);
})();
