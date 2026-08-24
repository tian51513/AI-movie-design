// comic_studio 前端入口（无构建，Vue3 本地 vendor）
// 分区导航：data → computed → methods(列表/详情/设置/日志/分镜) → 挂载
const { createApp } = Vue;
const STYLE_PRESETS = {
  // 通用
  '动漫': '日系动漫风格，干净线稿，赛璐璐上色，高饱和度，番剧质感',
  '写实': '写实风格，电影质感，真实皮肤与材质细节，自然光照',
  '国风仙侠': '国风仙侠风格，飘逸衣袂，古典配色，仙气氛围',
  '3D': '3D渲染风格，卡通造型，柔和全局光照，精致材质',
  // 常规剧情向
  '剧情PV': '剧情PV风格，叙事构图，角色互动，场景切换流畅，情绪递进',
  '宣传PV': '宣传PV风格，强视觉冲击，品牌质感，节奏明快，高对比配色',
  '动画': '卡通动画风格，夸张造型，明快色彩，插画质感',
  '情感PV': '情感PV风格，柔和光影，人物情绪特写，氛围渲染，浅景深',
  '风景PV': '风景PV风格，自然风光，广角构图，黄金时刻光线，旅行宣传片质感',
  '时尚写真': '时尚写真风格，人物造型精致，服饰搭配讲究，棚拍布光设计',
  '短剧PV': '短剧PV风格，剧情张力，角色冲突，反转节奏，紧凑剪辑感',
  '文艺': '文艺电影风格，胶片颗粒质感，朦胧氛围，留白构图，自然色调',
  '科幻': '科幻风格，赛博朋克视觉，霓虹光效，未来都市，冷色调高对比',
  '古风': '古风风格，传统服饰，水墨意境，古典美学，绢本设色质感',
  '未来科技': '未来科技风格，机械感，全息光影特效，金属材质，蓝色光晕',
  '悬疑': '悬疑风格，低调布光，阴影构图，镜头语言克制，情绪张力紧绷',
  '喜剧': '喜剧风格，夸张肢体动作，幽默表情特写，明快节奏，高饱和色彩',
  '舞台剧': '舞台剧风格，舞台布光，戏剧化表演，动态构图，剧场质感',
};

/* ===== data ===== */
function data() {
  return {
    view: 'projects', projects: [], project: null, assets: [],
    views: {}, queue: {running:0, pending:0, failed:0, comfy_ok:false},
    newName: '', newRatio: '9:16', newFile: null, creating: false,
    newStyleKey: '', newStyleText: '',
    analyzeState: { status: '', error: null }, pollTimer: null,
    settingsForm: { local: {}, online: {}, routing: {}, comfy: {}, t2i_tm: '',
                    model_overrides: {}, model_templates: [] }, saving: false,
    moTemplate: '', modelChoices: [], moError: '',
    ollamaModels: [], showThink: false, loadingModels: false,
    activeKind: '全部', perRow: 2, lightbox: null,
    comfyStatus: null, llmTesting: '', llmTestResult: {local: null, online: null},
    llmTestManual: {local: false, online: false},
    logs: [], lastLogId: 0, logsTimer: null,
    taskLabels: { extract_assets: '资产分析', fix_appearance: '外貌固化',
      split_storyboards: '分镜拆解', gen_video_prompt: '视频提示词生成' },
    detailMode: 'assets', shots: [], splitRunning: false, expandedShot: null, editingShot: false,
    paramsOpen: false, merges: [],
  };
}

/* ===== computed ===== */
const computed = {
  allHaveViews() { return this.assets.length && this.assets.every(a => (this.views[a.id]||[]).length); },
  allShotsReady() { return this.shots.length > 0 && this.shots.every(s => s.status === 'ready'); },
  allShotsRendered() { return this.shots.length > 0 && this.shots.every(s => !!s.video_url); },
  shownAssets() {
    return this.activeKind === '全部' ? this.assets
                                      : this.assets.filter(a => a.kind === this.activeKind);
  },
  displayOllamaModels() {
    return this.showThink ? this.ollamaModels
      : this.ollamaModels.filter(m => !m.toLowerCase().includes("think"));
  },
  currentMO() {
    const mo = this.settingsForm.model_overrides;
    if (!mo[this.moTemplate]) mo[this.moTemplate] = {};
    return mo[this.moTemplate];
  },
};

/* ===== methods ===== */
const methods = {
  // ===== 项目列表 =====
  async refresh() { this.projects = await (await fetch('/api/projects')).json(); },
  async createProject() {
    this.creating = true;
    const fd = new FormData();
    fd.append('name', this.newName); fd.append('aspect_ratio', this.newRatio);
    fd.append('style', this.newStyleKey === '自定义' ? this.newStyleText : STYLE_PRESETS[this.newStyleKey] || '');
    fd.append('novel', this.newFile);
    const resp = await fetch('/api/projects', { method: 'POST', body: fd });
    if (!resp.ok) { alert(await resp.text()); }
    this.creating = false; this.newName = ''; this.newFile = null;
    await this.refresh();
  },

  // ===== 详情：导航与资产 =====
  back() { clearInterval(this.pollTimer); this.pollTimer = null;
    this.stopLogsPolling(); this.project = null; this.view = 'projects';
    this.splitRunning = false; },
  async open(p) {
    clearInterval(this.pollTimer); this.pollTimer = null;  // 重入防护：清掉旧轮询
    this.logs = []; this.lastLogId = 0;
    this.detailMode = 'assets'; this.shots = []; this.splitRunning = false; this.expandedShot = null;
    this.merges = [];
    this.view = 'detail'; this.project = p;
    await this.loadDetail(); this.startLogsPolling();
  },
  async loadDetail() {
    this.project = await (await fetch(`/api/projects/${this.project.id}`)).json();
    this.assets = await (await fetch(`/api/projects/${this.project.id}/assets`)).json();
    this.views = {}; (await Promise.all(this.assets.map(async a => [a.id, await (await fetch(`/api/assets/${a.id}/views`)).json()]))).forEach(([k,v]) => this.views[k]=v);
    const s = await fetch(`/api/projects/${this.project.id}/analyze/status`);
    if (s.ok) this.analyzeState = await s.json();
    if (this.project.stage === 'rendered' || this.project.stage === 'merged') await this.loadMerges();
  },

  // ===== autopilot 一键出片 =====
  async setAutopilot(p, on) {
    const r = await fetch(`/api/projects/${p.id}`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({autopilot: on})});
    if (!r.ok) { alert(await r.text()); return; }
    Object.assign(p, await r.json());
    if (on) await this.refresh();  // 让列表立即显示「自动运行中」角标
  },
  autopilotActionLabel() {
    const a = this.project.autopilot_action;
    if (!a) return '自动运行中';
    return { analyze: '分析资产', gen_refs: '生成参考图', gate1: '过门1', split: '拆分分镜',
      gen_prompts: '生成提示词', gate2: '提示词检查', render: '批量渲染', gate3: '过门3',
      merge: '合成成片', wait: '等待任务', done: '已完成' }[a.action] || a.action;
  },
  async startMerge() {
    const r = await fetch(`/api/projects/${this.project.id}/merge`, {method: 'POST'});
    if (!r.ok) alert(await r.text());
  },
  async loadMerges() {
    try { this.merges = await (await fetch(`/api/projects/${this.project.id}/merges`)).json(); }
    catch (e) { /* 目录未建时静默 */ }
  },
  async editStyle() {
    const v = prompt('画风描述（留空则不指定，生成时由模型自由发挥）：',
      this.project.style || '');
    if (v === null) return;
    const r = await fetch(`/api/projects/${this.project.id}`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({style: v})});
    if (r.ok) { await this.loadDetail(); } else { alert(await r.text()); }
  },

  // ===== 详情：生成与队列 =====
  async startAnalyze() {
    const r = await fetch(`/api/projects/${this.project.id}/analyze`, { method: 'POST' });
    if (r.status === 202) {
      this.analyzeState = { status: 'running', error: null };
      this.pollTimer = setInterval(async () => {
        await this.loadDetail();
        if (this.analyzeState.status !== 'running') {
          clearInterval(this.pollTimer);
        }
      }, 2000);
    } else { alert(await r.text()); }
  },
  async genAllRefs() {
    const r = await fetch(`/api/projects/${this.project.id}/generate-refs`, {method:'POST'});
    alert(r.ok ? `已入队 ${(await r.json()).enqueued} 个资产` : await r.text());
  },
  assetBusy(a) {
    return (this.queue.jobs || []).some(
      j => j.type === 'gen_ref' && j.asset_id === a.id &&
           (j.status === 'pending' || j.status === 'running'));
  },
  async regenAsset(a) {
    const r = await fetch(`/api/assets/${a.id}/gen`, {method:'POST'});
    if (!r.ok) alert(await r.text());
  },
  async passGate1() {
    const r = await fetch(`/api/projects/${this.project.id}/gate1`, {method:'POST'});
    if (r.ok) { await this.loadDetail(); } else { alert(await r.text()); }
  },
  failedTooltip() { return (this.queue.jobs||[]).filter(j=>j.status==='failed').map(j=>`#${j.id}: ${j.error||''}`).join('\n') || '无失败任务'; },

  // ===== 设置 =====
  async openSettings() {
    clearInterval(this.pollTimer); this.pollTimer = null;
    this.stopLogsPolling(); this.project = null;
    this.view = 'settings'; this.checkComfy();
    this.llmTestResult = {local: null, online: null};
    this.llmTestManual = {local: false, online: false};
    const s = await (await fetch('/api/settings')).json();
    this.settingsForm = {
      local: { ...s.llm_providers.local }, online: { ...s.llm_providers.online },
      routing: { ...s.llm_routing },
      comfy: { ...s.comfy },
      t2i_tm: s.template_map?.t2i || '',
      model_overrides: JSON.parse(JSON.stringify(s.model_overrides || {})),
      model_templates: s.model_templates || [],
    };
    this.moTemplate = this.settingsForm.model_templates.includes('h3_ref2va')
      ? 'h3_ref2va' : (this.settingsForm.model_templates[0] || '');
    await this.loadModelChoices();
    this.llmTest('local', false); this.llmTest('online', false);  // 表单就绪后再自动检测
  },
  // ===== 模型切换 =====
  async loadModelChoices() {
    this.modelChoices = []; this.moError = '';
    if (!this.moTemplate) return;
    try {
      const r = await fetch(`/api/settings/models/choices?template=${encodeURIComponent(this.moTemplate)}`);
      if (!r.ok) { this.moError = '枚举失败：' + (await r.json()).detail; return; }
      const slots = await r.json();
      this.modelChoices = slots;
      const mo = this.currentMO;
      for (const slot of slots)
        if (!(slot.label in mo)) mo[slot.label] = '';  // 缺省「模板默认」
    } catch (e) { this.moError = '枚举失败：' + e; }
  },
  llmLamp(provider) {
    const r = this.llmTestResult[provider];
    if (this.llmTesting === provider) return {color: '#8b949e', text: '检测中…'};
    if (r === null) return {color: '#8b949e', text: ''};
    if (r.unconfigured) return {color: '#8b949e', text: '未配置'};
    return r.ok ? {color: '#4ade80', text: '在线'}
                : {color: '#f87171', text: '离线'};
  },
  async llmTest(provider, manual = true) {
    this.llmTesting = provider;
    this.llmTestManual[provider] = !!manual;
    const p = this.settingsForm[provider] || {};
    if (!(p.base_url || '').trim() || !(p.model || '').trim()) {
      this.llmTestResult[provider] = {ok: false, unconfigured: true, detail: 'base_url 或模型名为空'};
      this.llmTesting = ''; return;
    }
    try {
      const r = await fetch('/api/settings/llm-test', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({provider, base_url: p.base_url || '',
                              api_key: p.api_key || '', model: p.model || ''})});
      this.llmTestResult[provider] = await r.json();
    } catch (e) { this.llmTestResult[provider] = {ok: false, detail: String(e)}; }
    this.llmTesting = '';
  },
  async checkComfy() {
    this.comfyStatus = null;
    try { this.comfyStatus = (await (await fetch('/api/comfy/status')).json()).ok; }
    catch (e) { this.comfyStatus = false; }
  },
  async fetchOllamaModels() {
    this.loadingModels = true;
    try {
      const resp = await fetch('/api/settings/ollama-models?base_url=' +
        encodeURIComponent(this.settingsForm.local.base_url || ''));
      if (!resp.ok) {
        alert('获取失败：' + (await resp.json()).detail);
        this.loadingModels = false; return;
      }
      this.ollamaModels = (await resp.json()).models;
    } catch (e) { alert('获取失败：' + e); }
    this.loadingModels = false;
  },
  async saveSettings() {
    this.saving = true;      const payload = {
      llm_providers: {
        local: { base_url: this.settingsForm.local.base_url || '',
                 api_key: this.settingsForm.local.api_key || 'ollama',
                 model: this.settingsForm.local.model || '' },
        online: { base_url: this.settingsForm.online.base_url || '',
                  api_key: this.settingsForm.online.api_key || '',
                  model: this.settingsForm.online.model || '' },
      },
      llm_routing: { ...this.settingsForm.routing },
      comfy: { base_url: this.settingsForm.comfy.base_url || '' },
      template_map: { t2i: this.settingsForm.t2i_tm || null },
      model_overrides: this.settingsForm.model_overrides,
    };
    const resp = await fetch('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload) });
    this.saving = false;
    if (resp.ok) { alert('已保存'); } else { alert('保存失败：' + (await resp.text())); }
  },

  // ===== 日志 =====
  startLogsPolling() {
    this.stopLogsPolling();
    let wasBusy = false;
    const tick = async () => {
      const r = await fetch(`/api/projects/${this.project.id}/logs?after=${this.lastLogId}`);
      if (!r.ok || !this.project) return;
      const body = await r.json();
      if (body.logs.length) {
        this.logs.unshift(...body.logs); this.lastLogId = body.last_id;  // 时间降序：最新在顶
      }
      try {
        this.queue = await (await fetch(`/api/projects/${this.project.id}/queue`)).json();
        const done = this.queue.jobs.filter(j => j.status === 'done').length;
        const busy = this.queue.running > 0 || this.queue.pending > 0;
        if ((wasBusy && !busy) || done > this._doneSeen) { await this.loadDetail(); }
        this._doneSeen = done; wasBusy = busy;
      } catch (e) { /* 队列瞬时失败不影响日志流 */ }
      // 分镜模式轮询
      if (this.detailMode === 'shots' && this.project) {
        try {
          if (this.splitRunning) {
            const st = await (await fetch(`/api/projects/${this.project.id}/split-storyboards/status`)).json();
            if (st.status === 'done' || st.status === 'failed') {
              this.splitRunning = false;
              if (st.status === 'done') { await this.loadShots(); }
              else { alert('分镜拆解失败：' + (st.error||'')); }
            }
          } else {
            if (!this.editingShot) await this.loadShots();  // 编辑中跳过刷新，blur 后恢复
          }
          if (this.project.stage === 'rendered' || this.project.stage === 'merged') {
            await this.loadMerges();  // 合成完成 → 成片列表出现
          }
        } catch (e) { /* shots 轮询失败不影响日志流 */ }
      }
      // autopilot：轻量刷新详情（角标动作/阶段），重载仍由队列 busy→idle 触发
      if (this.project && this.project.autopilot && this.view === 'detail') {
        try { this.project = await (await fetch(`/api/projects/${this.project.id}`)).json(); }
        catch (e) { /* 瞬时失败忽略 */ }
      }
    };
    this._doneSeen = 0; tick(); this.logsTimer = setInterval(tick, 1000);
  },
  stopLogsPolling() { clearInterval(this.logsTimer); this.logsTimer = null; },
  logColor(level) { return { info: '#9ca3af', warn: '#facc15', error: '#f87171' }[level] || '#eee'; },

  // ===== 灯箱 =====

  // ===== 分镜 =====
  assetName(id) { const a = this.assets.find(x => x.id === id); return a ? a.name : id; },
  ledgerLabel(cat) { return { must_appear: '必须出现', must_keep: '必须保持', may_change: '允许变化', must_avoid: '禁止' }[cat] || cat; },
  async switchDetailMode(mode) {
    this.detailMode = mode;
    if (mode === 'shots' && this.project) await this.loadShots();
  },
  async loadShots() {
    this.shots = await (await fetch(`/api/projects/${this.project.id}/shots`)).json();
  },
  async startSplit() {
    if (this.shots.length && !confirm('已存在分镜，重新拆解将覆盖（提示词会丢失）。继续？')) return;
    const r = await fetch(`/api/projects/${this.project.id}/split-storyboards`, {method:'POST'});
    if (!r.ok) { alert(await r.text()); return; }
    this.splitRunning = true;
  },
  async regenPrompt(s, force=false) {
    const r = await fetch(`/api/shots/${s.id}/regen-prompt`,
      {method:'POST', headers:{'Content-Type':'application/json'},
       body: JSON.stringify({force})});
    if (!r.ok) alert(await r.text());
  },
  async genAllPrompts() {
    const r = await fetch(`/api/projects/${this.project.id}/generate-prompts`, {method:'POST'});
    alert(r.ok ? `已入队 ${(await r.json()).enqueued} 镜` : await r.text());
  },
  async saveShot(s, fields) {
    const r = await fetch(`/api/shots/${s.id}`, {method:'PATCH',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(fields)});
    if (r.ok) { Object.assign(s, await r.json()); await this.loadShots(); } else alert(await r.text());
  },
  async passGate2() {
    const r = await fetch(`/api/projects/${this.project.id}/gate2`, {method:'POST'});
    if (r.ok) await this.loadDetail(); else alert(await r.text());
  },
  async toggleShotPrompt(s) {
    if (this.expandedShot === s.id) { this.expandedShot = null; return; }
    this.expandedShot = s.id;
  },
  shotStatusColor(s) {
    if (s.status === 'ready') return '#4ade80';
    if (s.status === 'stale') return '#fb923c';
    return '#f87171';
  },
  shotStatusLabel(s) {
    return { pending: '待生成', ready: '就绪', stale: '资产已更新' }[s.status] || s.status;
  },
  async renderShot(s) {
    const r = await fetch(`/api/shots/${s.id}/render`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({force: !!s.video_url})});
    if (!r.ok) alert(await r.text());
  },
  async renderAll() {
    const r = await fetch(`/api/projects/${this.project.id}/render`, {method: 'POST'});
    if (r.ok) { const b = await r.json();
      alert(`入队 ${b.enqueued} 镜` + (b.skipped_no_prompt ? `（${b.skipped_no_prompt} 镜缺提示词已跳过）` : '')); }
    else alert(await r.text());
  },
  async passGate3() {
    const r = await fetch(`/api/projects/${this.project.id}/gate3`, {method: 'POST'});
    if (r.ok) await this.loadDetail(); else alert(await r.text());
  },
  async patchVideoParam(key, value) {
    const r = await fetch(`/api/projects/${this.project.id}`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({[key]: value})});
    if (!r.ok) { alert(await r.text()); await this.loadDetail(); }
  },
  promptBadge(s) {
    const j = s.prompt_job;
    if (!j) return '';
    if (j.status === 'running') return '提示词生成中…';
    if (j.status === 'done') return '';
    if (j.status === 'failed') return '提示词生成失败';
    return '排队中';
  },
  videoParamsLabel() {
    const p = this.project;
    if (!p) return '';
    return `${p.video_megapixels}MP · ${p.video_multiple}倍 · ${p.video_speed} · 默认${p.default_shot_duration}s · ${p.prompt_mode||'D'}模式 · LoRA${p.lora_realism}`;
  },
  async selectVersion(s, file) {
    const r = await fetch(`/api/shots/${s.id}/version`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file})});
    if (r.ok) await this.loadShots(); else alert(await r.text());
  },
  renderBadge(s) {
    const j = s.render_job;
    if (!j) return '';
    if (j.status === 'running') return `渲染中 · ${j.elapsed_s}s`;
    if (j.status === 'done') return `完成 · ${j.elapsed_s}s`;
    if (j.status === 'failed') return '渲染失败';
    return '排队中';
  },
  async editAssetDetail(a) {
    const v = prompt('外貌/服装描述（同步库与 meta；引用分镜会标 stale）：', a.detail || '');
    if (v === null || !v.trim()) return;
    const r = await fetch(`/api/assets/${a.id}`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({detail: v.trim()})});
    if (r.ok) { alert('已更新。引用该资产的分镜已标 stale——请重生参考图与提示词'); await this.loadDetail(); }
    else alert(await r.text());
  },

  stageName(s) { return { created: '已创建', analyzed: '已分析', assets_ready: '资产就绪',
    storyboard_ready: '分镜就绪', rendering: '渲染中', rendered: '已渲染', merged: '已合成' }[s] || s; },
  kindName(k) { return { character: '角色', scene: '场景', prop: '道具' }[k]; },
};

createApp({ data, computed, methods, async mounted() {
    await this.refresh();
    setInterval(async () => {  // 项目列表轮询：autopilot 角标/成片状态实时化
      if (this.view === 'projects' && !this.creating) {
        try { await this.refresh(); } catch (e) { /* 忽略瞬时失败 */ }
      }
    }, 4000);
  } })
  .mount('#app');
