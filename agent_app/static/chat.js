'use strict';

(() => {
    const get = id => document.getElementById(id);
    const storageKey = 'diskagent.chat.' + location.origin;
    let session = null;
    let posting = false;
    let polling = false;
    let available = false;
    let pendingRequest = null;
    let eventSignature = '';
    let cardSignature = '';
    let budgetEdited = false;
    let previewId = null;
    let previewInvalidated = false;

    function taskContext() {
        if (get('enable-objectives').checked && typeof window.readOptimizationPolicy !== 'function') {
            throw new Error('当前服务尚未加载目标配置接口，请打开新版工作台。');
        }
        return {run_id: get('context').value || null,
            optimization_policy: window.readOptimizationPolicy?.() ?? null,
            auto_recover: get('auto-recover').checked};
    }

    function contextSignature(value) {
        return JSON.stringify({run_id: value.run_id ?? null, optimization_policy: value.optimization_policy ?? null,
            auto_recover: value.auto_recover ?? false});
    }

    function renderContext() {
        try {
            const context = taskContext();
            const policy = context.optimization_policy;
            const names = {stress_mpa: '应力', mass_kg: '质量', frequency_hz: '频率'};
            const objectives = policy?.objectives.map(item => names[item.metric] +
                (item.direction === 'minimize' ? '最小化' : '最大化')).join('、');
            get('chat-context').textContent = '当前运行 ' + (context.run_id || '新任务 / 按文字指定') +
                ' · ' + (objectives || '新任务采用默认应力目标，续算沿用运行配置') +
                ' · 配置约束 ' + (policy?.constraints.length ?? 0) + ' 项 · 自动恢复' +
                (context.auto_recover ? '开启' : '关闭');
            return context;
        } catch (error) {
            get('chat-context').textContent = '任务设置待完善 · ' + error.message;
            return null;
        }
    }

    function restoreContext(value) {
        const context = value.pending_preview?.selection_context || value.context;
        window.bindAssistantRun?.(context.run_id || null);
        window.loadOptimizationPolicy?.(context.optimization_policy ?? null);
        get('auto-recover').checked = context.auto_recover ?? value.pending_preview?.arguments.auto_recover ?? false;
        previewId = null;
        previewInvalidated = false;
        budgetEdited = false;
    }

    async function refreshSessions() {
        try {
            const response = await window.api('/api/chat/sessions');
            const selector = get('chat-session-select');
            selector.replaceChildren();
            const placeholder = document.createElement('option');
            placeholder.value = '';
            placeholder.textContent = '选择历史对话';
            selector.append(placeholder);
            for (const saved of response.sessions) {
                const option = document.createElement('option');
                option.value = saved.session_id;
                option.textContent = saved.title + ' · ' + saved.updated_at;
                selector.append(option);
            }
            selector.value = session?.session_id || '';
        } catch (error) {
            get('chat-session-select').title = error.message;
        }
    }

    function renderCards(cards) {
        const signature = JSON.stringify(cards);
        if (signature === cardSignature) return;
        cardSignature = signature;
        const container = get('chat-result-cards');
        container.replaceChildren();
        const metrics = [['stress_mpa', '最大等效应力', 'MPa'], ['mass_kg', '整盘质量', 'kg'],
            ['displacement_mm', '最大位移', 'mm'], ['frequency_hz', '指定阶次频率', 'Hz']];
        const format = value => Number.isFinite(value) ? value.toFixed(6) : '未采集';
        const statuses = {feasible: '满足当前配置', infeasible: '超限', missing: '评价数据不足',
            not_configured: '数值约束未配置', passed: '通过', failed: '超限'};
        for (const card of cards) {
            const section = document.createElement('section');
            section.className = 'chat-result';
            const heading = document.createElement('h3');
            heading.textContent = card.label + (card.iteration != null ? ' · 记录 ' + card.iteration : '');
            section.append(heading);
            const values = document.createElement('div');
            values.className = 'chat-result-metrics';
            for (const [key, label, unit] of metrics) {
                const item = document.createElement('p');
                item.textContent = label + ' ' + format(card.observation[key]) + ' ' + unit;
                values.append(item);
            }
            section.append(values);
            const evaluation = card.evaluation;
            const status = document.createElement('p');
            status.textContent = (statuses[card.constraint_status || evaluation.status] || '待评价') +
                ' · 原生结果' + (card.native_verified ? '已核验' : '待复核') + ' · 工程资格认证未评价';
            section.append(status);
            const source = document.createElement('p');
            source.className = 'detail-id';
            source.textContent = [card.job_id, card.run_id, card.policy_id].filter(Boolean).join(' · ');
            section.append(source);
            const details = document.createElement('details');
            const label = document.createElement('summary');
            label.textContent = '尺寸、约束裕量与原生证据';
            const content = document.createElement('pre');
            content.className = 'code';
            content.textContent = JSON.stringify({parameters: card.observation.parameters,
                constraint_checks: evaluation.constraint_checks, native_report_file: card.native_report_file,
                checkpoint_file: card.checkpoint_file, project_file: card.project_file}, null, 2);
            details.append(label, content);
            section.append(details);
            const frequencies = card.observation.modal_frequencies_hz;
            if (Array.isArray(frequencies)) {
                const modes = document.createElement('p');
                modes.textContent = frequencies.map((value, index) => '第' + (index + 1) + '阶 ' + format(value) + ' Hz').join(' · ');
                section.append(modes);
            }
            container.append(section);
        }
    }

    function notice(text) {
        get('chat-status').textContent = text;
    }

    function controls() {
        const working = posting || ['working', 'working_elsewhere'].includes(session?.status);
        const context = renderContext();
        const preview = session?.pending_preview;
        const changed = !!preview && (previewInvalidated || !context ||
            contextSignature(context) !== contextSignature(preview.selection_context || session.context));
        get('chat-send').disabled = !available || working;
        get('chat-new').disabled = !available || working;
        get('assistant-new-task').disabled = !available || working;
        get('chat-session-select').disabled = working;
        get('chat-recover').disabled = !session || working;
        get('chat-execute').disabled = working || !!window.diskEngineeringBusy || !preview || changed || !!preview.job_id;
        get('chat-execute').classList.toggle('hidden', !preview || !!preview.job_id);
        get('chat-preview-context').textContent = changed ? '任务设置已改变，请发送任务重新生成预览。' :
            '';
    }

    function render(value) {
        if (previewId !== value.pending_preview?.preview_id) {
            previewId = value.pending_preview?.preview_id || null;
            previewInvalidated = false;
        }
        session = value;
        if (!budgetEdited) get('chat-token-budget').value = session.token_budget ?? 12000;
        controls();
        const labels = {working: '正在处理', working_elsewhere: '另一工作台正在处理此对话', ready: '可继续对话',
            failed: '本条处理失败', interrupted: '上次会话处理已中断，可恢复任务查询'};
        const cost = session.costs;
        notice((labels[session.status] || session.status) + ' · 对话模型请求 ' + cost.model_requests +
            ' 次 · 已记录 ' + (cost.total_tokens ?? '未知') + ' tokens · ' +
            ({complete: '用量完整', partial: '用量部分已知', unknown: '用量未知'}[cost.token_accounting_status] || ''));
        get('chat-error').textContent = session.error || '';
        get('chat-session-id').textContent = session.session_id;
        renderCards(session.result_cards || []);
        const latest = session.messages[session.messages.length - 1];
        const turnEvents = latest?.request_id ? session.events.filter(event => event.request_id === latest.request_id) : [];
        const taskConfirmation = turnEvents.some(event => ['preview', 'job_submitted'].includes(event.type));
        const answer = latest?.role === 'assistant' && !taskConfirmation ? latest.text : '';
        get('chat-answer').textContent = answer;
        get('chat-answer').classList.toggle('hidden', !answer);
        const nextEvents = JSON.stringify(session.events);
        if (nextEvents !== eventSignature) {
            const events = get('chat-tool-events');
            events.replaceChildren();
            const names = {connected: 'MCP已连接', model_started: '正在请求对话模型',
                model_result: '对话模型已返回', tool_started: '正在调用工具', tool_result: '工具已返回',
                preview: '已保存任务预览', job_submitted: '工程任务已提交', error: '处理出错', routed: '已使用规则解析'};
            for (const event of session.events.slice(-30)) {
                if (!names[event.type]) continue;
                const item = document.createElement('details');
                const label = document.createElement('summary');
                label.textContent = event.time + ' · ' + names[event.type] + (event.name ? ' · ' + event.name : '');
                const details = document.createElement('pre');
                details.className = 'code';
                details.textContent = JSON.stringify(event, null, 2);
                item.append(label, details);
                events.append(item);
            }
            eventSignature = nextEvents;
        }
        const preview = session.pending_preview;
        get('chat-task-summary').textContent = preview && !preview.job_id ?
            preview.tool === 'disk_evaluate_design' ? '待执行 · 指定尺寸评价 1 个设计，基准评价 0 个' :
                '待执行 · 候选 ' + (preview.budget.additional_candidates ?? '见原工作流') +
                ' 个，基准评价 ' + (preview.budget.baseline_evaluations ?? '见原工作流') + ' 个' : '';
        const jobs = get('chat-jobs');
        jobs.replaceChildren();
        for (const job of session.jobs) {
            const item = document.createElement('div');
            item.className = 'chat-job';
            item.textContent = '工程任务 ' + job.job_id + ' · ' +
                ({queued: '已排队', running: '正在计算', completed: job.ok ? '计算完成' : '计算失败',
                  detached: '任务来自之前的服务会话，请查询原记录', unknown: '状态待核验'}[job.status] || job.status);
            jobs.append(item);
        }
    }

    async function createSession() {
        const response = await window.api('/api/chat/sessions', {method: 'POST', body: '{}'});
        localStorage.setItem(storageKey, response.session.session_id);
        pendingRequest = null;
        render(response.session);
        await refreshSessions();
    }

    async function send(action = 'message') {
        if (posting || session?.status === 'working') return;
        const message = get('request').value.trim();
        if (action === 'message' && !message) {
            notice('请输入需要读取、查询或预览的工程任务。');
            return;
        }
        try {
            const context = taskContext();
            if (action === 'execute' && (get('chat-execute').disabled || previewInvalidated)) {
                throw new Error('请先生成与当前任务设置一致的预览。');
            }
            posting = true;
            controls();
            if (!session) await createSession();
            const body = action === 'execute' ? {action, preview_id: session.pending_preview.preview_id} : {
                action, message};
            Object.assign(body, {context_run_id: context.run_id, optimization_policy: context.optimization_policy,
                auto_recover: context.auto_recover});
            const tokenBudget = Number(get('chat-token-budget').value);
            if (!Number.isInteger(tokenBudget) || tokenBudget < 1000 || tokenBudget > 100000) {
                throw new Error('请输入1000至100000之间的整数token预算。');
            }
            body.token_budget = tokenBudget;
            const signature = JSON.stringify(body);
            if (!pendingRequest || pendingRequest.signature !== signature) {
                pendingRequest = {signature, request_id: 'msg_' + crypto.randomUUID().replaceAll('-', '')};
            }
            body.request_id = pendingRequest.request_id;
            const response = await window.api('/api/chat/sessions/' + session.session_id + '/messages', {
                method: 'POST', body: JSON.stringify(body)});
            pendingRequest = null;
            budgetEdited = false;
            if (action === 'message') get('request').value = '';
            render(response.session);
        } catch (error) {
            get('chat-error').textContent = error.message;
        } finally {
            posting = false;
            controls();
        }
    }

    async function refresh() {
        if (!session || polling || posting) return;
        polling = true;
        const sessionId = session.session_id;
        try {
            const response = await window.api('/api/chat/sessions/' + sessionId);
            if (session?.session_id === sessionId) render(response.session);
        } catch (error) {
            get('chat-error').textContent = error.message;
        } finally {
            polling = false;
        }
    }

    get('chat-send').addEventListener('click', () => send());
    get('chat-session-select').addEventListener('change', async event => {
        const selected = event.target.value;
        if (!selected || posting) return;
        try {
            const response = await window.api('/api/chat/sessions/' + selected);
            localStorage.setItem(storageKey, selected);
            pendingRequest = null;
            restoreContext(response.session);
            render(response.session);
            window.refresh?.();
        } catch (error) {
            get('chat-error').textContent = error.message;
        }
    });
    get('chat-token-budget').addEventListener('input', () => { budgetEdited = true; });
    get('chat-execute').addEventListener('click', () => send('execute'));
    get('chat-recover').addEventListener('click', async () => {
        if (!session || posting) return;
        try {
            posting = true;
            controls();
            const response = await window.api('/api/chat/sessions/' + session.session_id + '/recover', {
                method: 'POST', body: '{}'});
            render(response.session);
        } catch (error) {
            get('chat-error').textContent = error.message;
        } finally {
            posting = false;
            controls();
        }
    });
    get('chat-new').addEventListener('click', async () => {
        try { await createSession(); } catch (error) { get('chat-error').textContent = error.message; }
    });
    get('assistant-new-task').addEventListener('click', async () => {
        try {
            window.bindAssistantRun?.(null);
            get('request').value = '';
            await createSession();
            get('request').focus();
        } catch (error) { get('chat-error').textContent = error.message; }
    });
    get('request').addEventListener('keydown', event => {
        if (event.key === 'Enter' && event.ctrlKey) { event.preventDefault(); send(); }
    });
    window.diskAssistant = {
        invalidate() {
            if (session?.pending_preview && !session.pending_preview.job_id) previewInvalidated = true;
            controls();
        },
        refreshControls: controls,
        async previewWorkflow(workflowId) {
            get('request').value = '预览恢复工作流 ' + workflowId;
            await send();
        }
    };
    (async () => {
        try {
            const config = await window.api('/api/config');
            available = config.conversation?.available === true && config.conversation?.unified_tasks === 'v1';
            if (!available) {
                notice('请重启工作台服务以启用统一工程助手。');
                controls();
                return;
            }
            notice('明确任务使用规则生成预览；分析与复杂操作调用对话模型。');
            const requested = location.hash?.startsWith('#chat=') ? decodeURIComponent(location.hash.slice(6)) : null;
            const saved = requested || localStorage.getItem(storageKey);
            if (saved) {
                try {
                    const savedSession = (await window.api('/api/chat/sessions/' + saved)).session;
                    restoreContext(savedSession);
                    render(savedSession);
                    localStorage.setItem(storageKey, saved);
                }
                catch (error) { notice('可新建对话，已有工程记录可继续查询。'); }
            }
            await refreshSessions();
            controls();
            setInterval(refresh, 2000);
        } catch (error) {
            get('chat-error').textContent = error.message;
        }
    })();
})();
