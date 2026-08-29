// comic_studio 前端入口（无构建，Vue3 本地 vendor）
// 分区导航：data → computed → methods(列表/详情/设置/日志/分镜) → 挂载
const { createApp } = Vue;
// 画风拆层（2026-08-27 方案A）：v=视觉风格词（主图/关键帧等图像生成用），
// n=叙事/节奏词（只随完整 style 进视频提示词——"场景切换流畅""剪辑感"对 T2I 是噪声）
const STYLE_PRESETS = {
  // 通用
  '动漫':     { v: '日系动漫风格，干净线稿，赛璐璐上色，高饱和度，番剧质感', n: '' },
  '写实':     { v: '写实风格，电影质感，真实皮肤与材质细节，自然光照', n: '' },
  '国风仙侠': { v: '国风仙侠风格，飘逸衣袂，古典配色，仙气氛围', n: '' },
  '3D':       { v: '3D渲染风格，卡通造型，柔和全局光照，精致材质', n: '' },
  // 常规剧情向
  '剧情PV':   { v: '电影质感画面，叙事性构图，角色情绪饱满', n: '场景切换流畅，情绪递进' },
  '宣传PV':   { v: '强视觉冲击，高对比配色，精致品牌质感', n: '节奏明快' },
  '动画':     { v: '卡通动画风格，夸张造型，明快色彩，插画质感', n: '' },
  '情感PV':   { v: '柔和光影，人物情绪特写，浅景深', n: '氛围渲染，情绪递进' },
  '风景PV':   { v: '自然风光，广角构图，黄金时刻光线，旅行宣传片质感', n: '' },
  '时尚写真': { v: '时尚写真风格，人物造型精致，服饰搭配讲究，棚拍布光设计', n: '' },
  '短剧PV':   { v: '都市剧集质感，戏剧化光效', n: '剧情张力，反转节奏，紧凑剪辑感' },
  '文艺':     { v: '文艺电影风格，胶片颗粒质感，朦胧氛围，留白构图，自然色调', n: '' },
  '科幻':     { v: '科幻风格，赛博朋克视觉，霓虹光效，未来都市，冷色调高对比', n: '' },
  '古风':     { v: '古风风格，传统服饰，水墨意境，古典美学，绢本设色质感', n: '' },
  '未来科技': { v: '未来科技风格，机械感，全息光影特效，金属材质，蓝色光晕', n: '' },
  '悬疑':     { v: '悬疑风格，低调布光，阴影构图', n: '镜头语言克制，情绪张力紧绷' },
  '喜剧':     { v: '喜剧风格，夸张表情动作，明快高饱和色彩', n: '明快节奏' },
  '舞台剧':   { v: '舞台剧风格，舞台布光，剧场质感，动态构图', n: '戏剧化表演节奏' },
};
const presetStyle = k => { const p = STYLE_PRESETS[k]; return p ? [p.v, p.n].filter(Boolean).join('，') : ''; };
const presetStyleVis = k => (STYLE_PRESETS[k] && STYLE_PRESETS[k].v) || '';

/* ===== data ===== */
function data() {
  return {
    view: 'projects', projects: [], project: null, assets: [],
    views: {}, queue: {running:0, pending:0, failed:0, comfy_ok:false},
    newName: '', newRatio: '9:16', newFile: null, creating: false,
    newMode: 'upload', themes: [], newThemeId: '', newProtagonist: '', newWordCount: '',
    newExtraPrompt: '', themePreview: '', themePreviewing: false,
    createOpen: false, projPage: 1, projPageSize: 12, comicFiles: [], describing: false,
    projectsView: (() => { try { return localStorage.getItem('cs.projectsView') || 'grid'; } catch (e) { return 'grid'; } })(),
    newSegDur: 5, newTotalDur: 0,
    settingsTab: 'llm', wfImportFile: null, wfImporting: false, activeShotSeq: 1,
    themesManage: [], themeImportFile: null, themeImporting: false,
    editAssetOpen: false, editAssetId: null, editAssetName: '', editAssetDraft: '', editAssetKind: 'character',
    newStyleKey: '', newStyleText: '',
    analyzeState: { status: '', error: null }, pollTimer: null,
    settingsForm: { local: {}, online: {}, routing: {}, comfy: {}, t2i_tm: '',
                    model_overrides: {}, model_templates: [] }, saving: false,
    moTemplate: '', modelChoices: [], moError: '',
    ollamaModels: [], showThink: false, loadingModels: false,
    activeKind: '全部', perRow: 2, lightbox: null,
    comfyStatus: null, llmTesting: '', llmTestResult: {local: null, online: null},
    localProviderType: '',  // ollama / lmstudio / custom（从 base_url 反推）
    freeingComfy: false,
    llmTestManual: {local: false, local2: false, online: false},
    logs: [], lastLogId: 0, logsTimer: null,
    taskLabels: { extract_assets: '资产分析', fix_appearance: '外貌固化',
      split_storyboards: '分镜拆解', gen_video_prompt: '视频提示词生成',
      optimize_prompt: '提示词优化（✨按钮）', gen_story: '主题生成项目正文' },
    detailMode: 'assets', shots: [], splitRunning: false, expandedShot: null, editingShot: false,
    splitTargetCount: null, shotSel: [], editingProject: false,
    chapters: [], splitChFrom: null, splitChTo: null, splitAllChapters: true,
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
  projTotalPages() { return Math.max(1, Math.ceil(this.projects.length / this.projPageSize)); },
  directorBusy() {  // 快车道状态由队列轮询驱动（刷新页面可恢复）
    return ((this.queue && this.queue.jobs) || []).some(
      j => j.type === 'gen_director' && (j.status === 'pending' || j.status === 'running'));
  },
  hasActiveJobs() {
    return this.directorBusy || (this.queue && (this.queue.pending > 0 || this.queue.running > 0));
  },
  pagedProjects() {
    const pg = Math.min(this.projPage, this.projTotalPages);  // 删除项目后页码越界自动收敛
    const start = (pg - 1) * this.projPageSize;
    return this.projects.slice(start, start + this.projPageSize);
  },
};

/* ===== methods ===== */
const methods = {
  // ===== 项目列表 =====
  async refresh() { this.projects = await (await fetch('/api/projects')).json(); },
  openCreate() {
    this.createOpen = true;
    if (this.newMode === 'theme') this.loadThemes();
  },
  setProjectsView(v) {
    this.projectsView = v;
    try { localStorage.setItem('cs.projectsView', v); } catch (e) { /* 无痕模式等 */ }
  },
  async createProject() {
    this.creating = true;
    const fd = new FormData();
    fd.append('name', this.newName); fd.append('aspect_ratio', this.newRatio);
    fd.append('style', this.newStyleKey === '自定义' ? this.newStyleText : presetStyle(this.newStyleKey));
    fd.append('style_vis', this.newStyleKey === '自定义' ? this.newStyleText : presetStyleVis(this.newStyleKey));
    fd.append('novel', this.newFile);
    fd.append('default_shot_duration', this.newSegDur || 5);
    fd.append('target_duration', this.newTotalDur || 0);
    const resp = await fetch('/api/projects', { method: 'POST', body: fd });
    if (!resp.ok) { alert(await resp.text()); }
    this.creating = false; this.newName = ''; this.newFile = null;
    this.createOpen = false;
    await this.refresh();
  },
  async loadThemes() {
    if (this.themes.length) return;
    try { this.themes = await (await fetch('/api/themes')).json(); }
    catch (e) { alert('主题列表加载失败：' + e); }
  },
  templatesOfType(type) {
    return (this.settingsForm.model_templates || []).filter(t => t.type === type);
  },
  async loadThemesManage() {
    try { this.themesManage = await (await fetch('/api/themes')).json(); }
    catch (e) { /* 忽略 */ }
  },
  scrollToShot(seq) {
    this.activeShotSeq = seq;
    const strip = document.getElementById('shotStrip');
    if (!strip) return;
    const card = strip.children[seq - 1];
    if (card) card.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'start' });
  },
  onStripScroll() {
    // 检测最靠近左侧边缘的卡片 → 高亮对应导航按钮
    const strip = document.getElementById('shotStrip');
    if (!strip || !this.shots.length) return;
    const stripLeft = strip.scrollLeft;
    const cardW = strip.children[0] ? strip.children[0].offsetWidth + 12 : 400;
    const idx = Math.round(stripLeft / cardW);
    if (idx >= 0 && idx < this.shots.length) {
      this.activeShotSeq = this.shots[idx].seq;
    }
  },
  kfUrl(s, phase) {
    // 从 shots API 返回的 kf_start_url / kf_end_url 取（routes_shots 需附上）
    return s[`kf_${phase}_url`];
  },
  async uploadKf(s, phase, file) {
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    const r = await fetch(`/api/shots/${s.id}/keyframe?phase=${phase}`,
      { method: 'POST', body: fd });
    if (!r.ok) { alert('上传失败'); return; }
    await this.loadShots();  // 刷新缩略图
  },
  async generateTts() {
    const r = await fetch(`/api/projects/${this.project.id}/tts`, {method: 'POST'});
    if (r.ok) {
      const b = await r.json();
      alert(b.shots_with_dialogue
        ? `配音就绪：${b.shots_with_dialogue} 镜`
        : '0 镜有台词——拆分镜按原文照录对白，小说原文没有引号对白时无内容可配音');
    }
    else alert(await r.text());
  },
  async autoBind() {
    const r = await fetch(`/api/projects/${this.project.id}/auto-bind`, {method: 'POST'});
    if (r.ok) { const b = await r.json(); alert(`补绑 ${b.bound} 处角色`); await this.loadShots(); }
    else alert(await r.text());
  },
  async regenKf(s, phase = 'all') {
    const label = phase === 'start' ? '首帧' : phase === 'end' ? '尾帧' : '双帧';
    if (!confirm(`重新生成分镜 ${s.seq} 的${label}？`)) return;
    const r = await fetch(`/api/shots/${s.id}/regen-keyframes?phase=${phase}`,
                          { method: 'POST' });
    if (!r.ok) { alert(await r.text()); return; }
    await this.loadShots();  // 刷新缩略图
  },
  testWorkflow(tid) {
    if (!tid) return;
    // 下载 JSON → 新标签打开 ComfyUI
    const a = document.createElement('a');
    a.href = `/api/workflows/download?tid=${encodeURIComponent(tid)}`;
    a.download = tid + '.json';
    a.click();
    const comfy = (this.settingsForm.comfy?.base_url) || 'http://127.0.0.1:8188';
    window.open(comfy, '_blank');
  },
  async importWorkflow() {
    if (!this.wfImportFile) return;
    this.wfImporting = true;
    try {
      const fd = new FormData();
      fd.append('file', this.wfImportFile);
      const r = await fetch('/api/workflows/import', { method: 'POST', body: fd });
      const b = await r.json();
      if (!r.ok) alert('导入失败：' + (b.detail || ''));
      else { alert(`已导入 ${b.id}（类型 ${b.type}）`); this.wfImportFile = null;
             // 重载模板列表
             const s2 = await (await fetch('/api/settings')).json();
             this.settingsForm.model_templates = s2.model_templates || []; }
    } catch (e) { alert('导入失败：' + e); }
    this.wfImporting = false;
  },
  async importThemes() {
    if (!this.themeImportFile) return;
    this.themeImporting = true;
    try {
      const fd = new FormData();
      fd.append('file', this.themeImportFile);
      const r = await fetch('/api/themes/import', { method: 'POST', body: fd });
      const b = await r.json();
      if (!r.ok) alert('导入失败：' + (b.detail || ''));
      else { alert(`已导入 ${b.imported} 个主题`); this.themeImportFile = null;
             this.themes = []; await this.loadThemesManage(); }
    } catch (e) { alert('导入失败：' + e); }
    this.themeImporting = false;
  },
  async deleteTheme(t) {
    if (!confirm(`删除主题「${t.name}」？`)) return;
    const r = await fetch(`/api/themes/${t.id}`, { method: 'DELETE' });
    if (!r.ok) alert(await r.text());
    else { this.themes = []; await this.loadThemesManage(); }
  },
  async previewFromTheme() {
    this.themePreviewing = true;
    try {
      const resp = await fetch('/api/projects/from-theme/preview', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          theme_id: this.newThemeId, aspect_ratio: this.newRatio,
          protagonist: this.newProtagonist,
          word_count: this.newWordCount || undefined,
          extra_prompt: this.newExtraPrompt || undefined }) });
      if (!resp.ok) { alert(await resp.text()); }
      else { this.themePreview = (await resp.json()).text; }
    } catch (e) { alert('生成失败：' + e); }
    this.themePreviewing = false;
  },
  async createFromTheme() {
    this.creating = true;
    try {
      const resp = await fetch('/api/projects/from-theme', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          theme_id: this.newThemeId, aspect_ratio: this.newRatio,
          name: this.newName || undefined, protagonist: this.newProtagonist,
          word_count: this.newWordCount || undefined,
          extra_prompt: this.newExtraPrompt || undefined,
          text: this.themePreview || undefined,  // 两步流：确认/编辑后的正文直建，不再调 LLM
          default_shot_duration: this.newSegDur || 5,
          target_duration: this.newTotalDur || 0 }) });
      if (!resp.ok) { alert(await resp.text()); }
      else { this.newName = ''; this.newProtagonist = ''; this.newThemeId = '';
             this.newWordCount = ''; this.newExtraPrompt = ''; this.themePreview = '';
             this.createOpen = false; }
    } catch (e) { alert('生成失败：' + e); }
    this.creating = false;
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
    // 项目参数输入中跳过 project 整体替换——否则轮询把正在键入的值顶回旧值，
    // 手动输入"存不上"（点加减按钮立即触发 change 所以没事；2026-08-27 真机）
    if (!this.editingProject) {
      this.project = await (await fetch(`/api/projects/${this.project.id}`)).json();
    }
    // P7-E 章节结构（有章节才显示章节选择；默认全选范围）
    try {
      this.chapters = await (await fetch(`/api/projects/${this.project.id}/chapters`)).json();
      if (this.chapters && !this.splitChFrom) {
        this.splitChFrom = this.chapters[0]?.idx ?? null;
        this.splitChTo = this.chapters[this.chapters.length - 1]?.idx ?? null;
      }
    } catch (e) { this.chapters = []; }
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
  async deleteProject(p) {
    if (!confirm(`确认删除项目「${p.name}」？\n分镜、任务、渲染产物与成片将全部清除（不可恢复）。`)) return;
    if (!confirm(`再次确认：删除「${p.name}」不可恢复，确定？`)) return;
    const r = await fetch(`/api/projects/${p.id}`, {method: 'DELETE'});
    if (!r.ok) { alert(await r.text()); return; }
    if (this.project && this.project.id === p.id) this.back();
    await this.refresh();
  },
  autopilotActionLabel() {
    const a = this.project && this.project.autopilot_action;
    return this.actionLabel(a);
  },
  actionLabel(a) {
    if (!a) return '自动运行中';
    if (a.action === 'wait') return a.detail || '等待任务';  // wait 显示具体原因（渲染中等）
    return { analyze: '分析资产', gen_refs: '生成参考图', gate1: '过门1', split: '拆分分镜',
      gen_prompts: '生成提示词', gate2: '提示词检查', render: '批量渲染', gate3: '过门3',
      merge: '合成成片', done: '已完成' }[a.action] || a.action;
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
    const vis = prompt('图像画风子集（主图/关键帧用，视觉词；留空=同上）：',
      this.project.style_vis || '');
    if (vis === null) return;
    const body = { style: v };
    if (vis.trim()) body.style_vis = vis;
    const r = await fetch(`/api/projects/${this.project.id}`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)});
    if (r.ok) { await this.loadDetail(); } else { alert(await r.text()); }
  },
  async editEra() {
    const v = prompt('时代背景（如 中国唐代；留空=清除。参考图与视频提示词会自动附加时代限制）：',
      this.project.era || '');
    if (v === null) return;
    const r = await fetch(`/api/projects/${this.project.id}`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({era: v})});
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
  async uploadMain(a, file) {
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    const r = await fetch(`/api/assets/${a.id}/main-image`, { method: 'POST', body: fd });
    if (!r.ok) { alert('上传失败：' + (await r.json()).detail); return; }
    alert('主图已上传。可点「三视图」从新主图重新派生');
    await this.loadDetail();
  },
  async regenAsset(a, stage = 'all') {
    const r = await fetch(`/api/assets/${a.id}/gen`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({stage})});
    if (!r.ok) alert(await r.text());
  },
  async passGate1() {
    const r = await fetch(`/api/projects/${this.project.id}/gate1`, {method:'POST'});
    if (r.ok) { await this.loadDetail(); } else { alert(await r.text()); }
  },
  failedTooltip() { return (this.queue.jobs||[]).filter(j=>j.status==='failed').map(j=>`#${j.id}: ${j.error||''}`).join('\n') || '无失败任务'; },
  async clearQueue() {
    if (!confirm(`确认取消本项目的 ${this.queue.pending} 个排队 + ${this.queue.running} 个在跑任务？\n（在跑的渲染会被掐断；已完成的不受影响）`)) return;
    const r = await fetch(`/api/projects/${this.project.id}/queue`, {method: 'DELETE'});
    if (r.ok) alert(`已取消 ${(await r.json()).cancelled} 个任务`);
    else alert(await r.text());
  },

  // ===== 设置 =====
  async openSettings() {
    clearInterval(this.pollTimer); this.pollTimer = null;
    this.stopLogsPolling(); this.project = null;
    this.view = 'settings'; this.checkComfy();
    this.llmTestResult = {local: null, online: null};
    this.llmTestManual = {local: false, online: false};
    const s = await (await fetch('/api/settings')).json();
    // 从 base_url 反推服务商类型（选下拉用——必须在 s 赋值之后！）
    const _bu = (s.llm_providers.local?.base_url || '').toLowerCase();
    this.localProviderType = _bu.includes('11434') ? 'ollama'
      : _bu.includes('1234') ? 'lmstudio'
      : _bu ? 'custom' : '';
    this.settingsForm = {
      local: { ...s.llm_providers.local,
               extra_body_json: s.llm_providers.local?.extra_body ? JSON.stringify(s.llm_providers.local.extra_body) : '' },
      online: { ...s.llm_providers.online,
                extra_body_json: s.llm_providers.online?.extra_body ? JSON.stringify(s.llm_providers.online.extra_body) : '' },
      routing: { ...s.llm_routing },
      comfy: { ...s.comfy },
      t2i_tm: s.template_map?.t2i || '',
      cvTm: s.template_map?.character_views || '',
      kfTm: s.template_map?.keyframe || 'xf_zimage_ti2i',
      r2vaTm: s.template_map?.ref2va || 'h3_ref2va',
      fl2vTm: s.template_map?.fl2v || 'h3_fl2v',
      t2vTm: s.template_map?.t2v || 'h3_t2v',
      dirTm: s.template_map?.director || 'h3_director',
      model_overrides: JSON.parse(JSON.stringify(s.model_overrides || {})),
      model_templates: s.model_templates || [],
    };
    const ids = (this.settingsForm.model_templates || []).map(t => t.id);
    this.moTemplate = ids.includes('h3_ref2va') ? 'h3_ref2va' : (ids[0] || '');
    await this.loadModelChoices();
    this.loadThemesManage();
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
      // 未覆盖的槽位预填模板当前值——用户能看到默认用的是什么再决定是否调整
      for (const slot of slots)
        if (!mo[slot.label]) mo[slot.label] = slot.current || '';
    } catch (e) { this.moError = '枚举失败：' + e; }
  },
  async resetMO() {
    if (!confirm(`恢复「${this.moTemplate}」全部槽位为工作流内置默认？`)) return;
    this.settingsForm.model_overrides[this.moTemplate] = {};
    const r = await fetch('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({model_overrides: {[this.moTemplate]: {}}})});
    if (!r.ok) { alert('恢复失败：' + (await r.text())); return; }
    await this.loadModelChoices();  // 重新预填内置默认
  },
  llmLamp(provider) {
    const r = this.llmTestResult[provider];
    if (this.llmTesting === provider) return {color: '#8b949e', text: '检测中…'};
    if (r == null) return {color: '#8b949e', text: ''};  // null/undefined 都安全
    if (r.unconfigured) return {color: '#8b949e', text: '未配置'};
    return r.ok ? {color: '#4ade80', text: '在线'}
                : {color: '#f87171', text: '离线'};
  },
  // extra_body 输入框（JSON 字符串）→ 对象；空返回 null，非法 JSON 返回 undefined
  _parseExtra(p) {
    const raw = (p.extra_body_json || '').trim();
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (e) { return undefined; }
  },
  async llmTest(provider, manual = true) {
    this.llmTesting = provider;
    this.llmTestManual[provider] = !!manual;
    const p = this.settingsForm[provider] || {};
    if (!(p.base_url || '').trim() || !(p.model || '').trim()) {
      this.llmTestResult[provider] = {ok: false, unconfigured: true, detail: 'base_url 或模型名为空'};
      this.llmTesting = ''; return;
    }
    const eb = this._parseExtra(p);
    if (eb === undefined) {
      this.llmTestResult[provider] = {ok: false, detail: 'extra_body 不是合法 JSON'};
      this.llmTesting = ''; return;
    }
    try {
      const r = await fetch('/api/settings/llm-test', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({provider, base_url: p.base_url || '',
                              api_key: p.api_key || '', model: p.model || '',
                              extra_body: eb})});
      this.llmTestResult[provider] = await r.json();
    } catch (e) { this.llmTestResult[provider] = {ok: false, detail: String(e)}; }
    this.llmTesting = '';
  },
  async checkComfy() {
    this.comfyStatus = null;
    // 传表单值：手输地址立即可测，不依赖先保存（2026-08-29 用户需求）
    const bu = encodeURIComponent((this.settingsForm?.comfy?.base_url) || '');
    try { this.comfyStatus = (await (await fetch(`/api/comfy/status?base_url=${bu}`)).json()).ok; }
    catch (e) { this.comfyStatus = false; }
  },
  async freeComfy() {
    this.freeingComfy = true;
    try {
      const r = await fetch('/api/comfy/free', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({unload_models: true})});
      alert(r.ok ? '已请求 ComfyUI 卸载模型并清理显存/内存' : '清理失败：' + (await r.json()).detail);
    } catch (e) { alert('清理失败：' + e); }
    this.freeingComfy = false;
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
    const ebLocal = this._parseExtra(this.settingsForm.local);
    const ebOnline = this._parseExtra(this.settingsForm.online);
    if (ebLocal === undefined || ebOnline === undefined) {
      alert('extra_body 不是合法 JSON（各 provider 检查）'); return;
    }
    this.saving = true;      const payload = {
      llm_providers: {
        local: { base_url: this.settingsForm.local.base_url || '',
                 api_key: this.settingsForm.local.api_key || 'ollama',
                 model: this.settingsForm.local.model || '',
                 extra_body: ebLocal },
        online: { base_url: this.settingsForm.online.base_url || '',
                  api_key: this.settingsForm.online.api_key || '',
                  model: this.settingsForm.online.model || '',
                  extra_body: ebOnline },
      },
      llm_routing: { ...this.settingsForm.routing },
      comfy: { base_url: this.settingsForm.comfy.base_url || '',
               director_clear_vram: !!this.settingsForm.comfy.director_clear_vram,
               director_export_source: !!this.settingsForm.comfy.director_export_source,
               director_batch_relay: this.settingsForm.comfy.director_batch_relay !== false,
               director_mix: this.settingsForm.comfy.director_mix !== false },
      template_map: { t2i: this.settingsForm.t2i_tm || null,
                      character_views: this.settingsForm.cvTm || null,
                      keyframe: this.settingsForm.kfTm || null,
                      ref2va: this.settingsForm.r2vaTm || null,
                      fl2v: this.settingsForm.fl2vTm || null,
                      t2v: this.settingsForm.t2vTm || null,
                      director: this.settingsForm.dirTm || null },
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
    this._tickBusy = true;  // 首拍按忙等（1s），随后自适应
    const tick = async () => {
      // 日志拉取整体包 try/catch——此前裸 fetch 一旦网络抖动/服务重启，
      // loop() 直接抛异常死掉，日志轮询永久停止（2026-08-28 真机：看不到日志了）
      try {
        const r = await fetch(`/api/projects/${this.project.id}/logs?after=${this.lastLogId}`);
        if (r.ok && this.project) {
          const body = await r.json();
          if (body.logs.length) {
            if (this.lastLogId === 0) {
              this.logs = body.logs;  // 首拉：后端已按时间降序（最新在顶）
            } else {
              this.logs.unshift(...body.logs.slice().reverse());  // 增量升序 → 倒序后 unshift 保持最新在顶
            }
            this.lastLogId = body.last_id;
          }
        }
      } catch (e) { /* 瞬时失败不杀轮询 */ }
      try {
        this.queue = await (await fetch(`/api/projects/${this.project.id}/queue`)).json();
        const dj = (this.queue.jobs || []).find(j => j.type === 'gen_director');
        if (dj) {
          // 只在「页面打开期间发生的新失败」弹窗——首次观测到的旧失败（如刷新前
          // 的 job 721）不弹，避免打开页面就被历史错误轰炸
          const prev = this._dirStatus;
          this._dirStatus = dj.status;
          if (dj.status === 'failed' && prev && prev !== 'failed') {
            alert('🚄 整段快车道失败：' + (dj.error || '详见日志'));
          }
        }
        const done = this.queue.jobs.filter(j => j.status === 'done').length;
        const busy = this.queue.running > 0 || this.queue.pending > 0;
        this._tickBusy = busy || (this.project && this.project.autopilot);
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
      if (this.project && this.project.autopilot && this.view === 'detail'
          && !this.editingProject) {
        try { this.project = await (await fetch(`/api/projects/${this.project.id}`)).json(); }
        catch (e) { /* 瞬时失败忽略 */ }
      }
    };
    this._doneSeen = 0;
    const loop = async () => {          // 自适应轮询：忙 1s / 闲 4s（减少空转请求）
      try { await tick(); } catch (e) { /* tick 内部已兜底，双保险 */ }
      this.logsTimer = setTimeout(loop, this._tickBusy ? 1000 : 4000);
    };
    loop();
  },
  stopLogsPolling() { clearTimeout(this.logsTimer); this.logsTimer = null; },
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
    // 分镜数可选：填了按全文目标数分配各块配额，不填自动拆分；
    // 章节范围：勾"全部章"不传（=全文），否则按 from–to 切片（P7-E）
    const payload = {};
    if (this.splitTargetCount) payload.target_count = this.splitTargetCount;
    if (this.chapters.length && !this.splitAllChapters && this.splitChFrom && this.splitChTo) {
      payload.chapter_from = Math.min(this.splitChFrom, this.splitChTo);
      payload.chapter_to = Math.max(this.splitChFrom, this.splitChTo);
    }
    const body = Object.keys(payload).length ? JSON.stringify(payload) : null;
    const r = await fetch(`/api/projects/${this.project.id}/split-storyboards`,
      {method:'POST', headers: body ? {'Content-Type': 'application/json'} : undefined, body});
    if (!r.ok) { alert(await r.text()); return; }
    this.splitRunning = true;
  },
  // 本地服务商切换：自动填预设地址（可手改）；不清模型（防误保存空值——
  // 真机 2026-08-29：清了模型用户直接保存 → 库里 base_url/model 全空）
  switchLocalProvider(type) {
    this.localProviderType = type;
    if (type === 'ollama') {
      this.settingsForm.local.base_url = 'http://127.0.0.1:11434';
      this.settingsForm.local.api_key = 'ollama';
    } else if (type === 'lmstudio') {
      this.settingsForm.local.base_url = 'http://127.0.0.1:1234';
      this.settingsForm.local.api_key = 'lmstudio';
    }
    // custom 不动地址（用户自己填）；模型保留——切后点「获取模型」重选即可
  },
  async stopJobs() {
    if (!confirm('停止本项目全部任务？\n待跑的直接取消；在跑的向 ComfyUI 发中断（约几秒内停止）。')) return;
    const r = await fetch(`/api/projects/${this.project.id}/stop-jobs`, {method: 'POST'});
    if (!r.ok) { alert(await r.text()); return; }
    const b = await r.json();
    alert(`已取消待跑 ${b.cancelled} 项、停止在跑 ${b.stopping} 项`);
    this._dirStatus = '';
  },
  async renderDirector() {
    if (!confirm('整段快车道：全部生效镜一次提交导演台（段间 latent 连贯），产出整片直达合成。\nv1 限制：不混配音字幕（要配音版请走逐镜「批量渲染」）。继续？')) return;
    const r = await fetch(`/api/projects/${this.project.id}/render-director`, {method: 'POST'});
    if (!r.ok) { alert(await r.text()); return; }
    alert('已入队——整段渲染耗时较长，详情页日志可跟踪');
  },
  async createFromComic() {
    if (!this.comicFiles.length) return;
    this.creating = true;
    const fd = new FormData();
    fd.append('name', this.newName || `漫画${this.comicFiles.length}页`);
    fd.append('aspect_ratio', this.newRatio);
    // 自然排序（numeric:true）：1.png < 2.png < 10.png（字符串排序会 1<10<2 乱序）
    this.comicFiles.sort((a, b) => a.name.localeCompare(b.name, undefined, {numeric: true}))
      .forEach(f => fd.append('images', f, f.name));
    const resp = await fetch('/api/projects/from-comic', { method: 'POST', body: fd });
    this.creating = false;
    if (!resp.ok) { alert(await resp.text()); return; }
    this.newName = ''; this.comicFiles = []; this.createOpen = false;
    await this.refresh();
  },
  async describeShots() {
    if (!confirm('VLM 读图生成全部缺失提示词？（本地视觉模型逐镜调用，页多较慢）')) return;
    const r = await fetch(`/api/projects/${this.project.id}/describe-shots`, {method: 'POST'});
    if (!r.ok) { alert(await r.text()); return; }
    this.describing = true;
    setTimeout(() => { this.describing = false; this.loadShots(); }, 3000);  // 简易轮询：稍后刷新
  },
  async describeOneShot(s) {
    // 逐镜 VLM 读图（漫画项目）：同步等待结果（10-30s），有明确反馈
    this.describing = s.id;  // 记录正在读图的镜 id（按钮显示状态）
    try {
      const r = await fetch(`/api/projects/${this.project.id}/describe-shots?shot_id=${s.id}`, {method: 'POST'});
      if (!r.ok) { alert('读图失败：' + (await r.text())); return; }
      const b = await r.json();
      await this.loadShots();
      if (b.generated > 0) {
        // 短暂高亮提示成功
        s._justRead = true;
        setTimeout(() => { s._justRead = false; }, 2000);
      } else {
        alert('模型没有生成提示词（看日志排查）');
      }
    } catch (e) { alert('读图失败：' + e); }
    this.describing = null;
  },
  async batchShots(action) {
    const ids = this.shotSel;
    if (!ids.length) return;
    if (action === 'delete' && !confirm(`确定删除选中的 ${ids.length} 个分镜？不可恢复。`)) return;
    const r = await fetch(`/api/projects/${this.project.id}/shots/batch`,
      {method:'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({action, ids})});
    if (!r.ok) { alert(await r.text()); return; }
    this.shotSel = [];
    await this.loadDetail();
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
    const label = this.shotStatusLabel(s);
    if (label === '渲染中' || label === '排队渲染') return '#f59e0b';
    if (label === '生成提示词') return '#60a5fa';
    if (s.status === 'ready') return '#4ade80';
    if (s.status === 'stale') return '#fb923c';
    return '#f87171';
  },
  shotStatusLabel(s) {
    // 渲染进行中动态覆盖（真机 2026-08-26：渲染时仍显示'就绪'令人困惑）
    if (s.render_job && s.render_job.status === 'running') return '渲染中';
    if (s.render_job && s.render_job.status === 'pending') return '排队渲染';
    if (s.prompt_job && s.prompt_job.status === 'running') return '生成提示词';
    return { pending: '待生成', ready: '就绪', stale: '资产已更新', '生成首尾帧': '生成首尾帧', '渲染中': '渲染中' }[s.status] || s.status;
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
  currentRenderMode() {
    if (!this.shots.length) return 'ref2va';
    const counts = {};
    for (const s of this.shots) counts[s.workflow_type] = (counts[s.workflow_type] || 0) + 1;
    return Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0];
  },
  async patchRenderMode(mode) {
    const r = await fetch(`/api/projects/${this.project.id}`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({render_mode: mode})});
    if (r.ok) { await this.loadDetail(); await this.loadShots(); }
    else alert(await r.text());
  },
  async patchVideoParam(key, value) {
    const r = await fetch(`/api/projects/${this.project.id}`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({[key]: value})});
    if (r.ok) { Object.assign(this.project, await r.json()); }  // 回写 UI——否则输入框被旧值顶回，形同没保存
    else { alert(await r.text()); await this.loadDetail(); }
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
    return `${p.video_megapixels}MP · ${p.video_multiple}倍 · ${p.video_speed} · 段${p.default_shot_duration}s${p.target_duration ? ` · 总${p.target_duration}s` : ''} · ${p.prompt_mode||'D'}模式 · LoRA${p.lora_realism}`;
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
  editAssetDetail(a) {
    this.editAssetId = a.id;
    this.editAssetName = a.name;
    this.editAssetKind = a.kind || 'character';
    this.editAssetDraft = a.detail || '';
    this.editAssetOpen = true;
  },
  async saveAssetDetail() {
    const v = (this.editAssetDraft || '').trim();
    if (!v) { alert('描述不能为空'); return; }
    const r = await fetch(`/api/assets/${this.editAssetId}`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({detail: v})});
    if (r.ok) {
      this.editAssetOpen = false;
      alert('已更新。引用该资产的分镜已标 stale——请重生参考图与提示词');
      await this.loadDetail();
    } else alert(await r.text());
  },

  stageName(s) { return { created: '已创建', analyzed: '已分析', assets_ready: '资产就绪',
    storyboard_ready: '分镜就绪', rendering: '渲染中', rendered: '已渲染', merged: '已合成' }[s] || s; },
  kindName(k) { return { character: '角色', scene: '场景', prop: '道具' }[k]; },
  fmtDate(s) { return s ? String(s).slice(5, 16) : ''; },  // "2026-08-27 20:15" → "08-27 20:15"
  dialogueOf(s) { return (s.ledger && s.ledger.dialogue) || []; },
};

/* ===== 可复用组件 ===== */
// 提示词优化输入框：textarea 右上角 ✨ → 弹窗（预填当前值）→ LLM 优化 → 确认覆盖
const PromptBox = {
  props: {
    modelValue: { type: String, default: '' },
    rows: { type: Number, default: 2 },
    kind: { type: String, default: 'generic' },  // shot_desc / video_prompt / appearance / generic
  },
  emits: ['update:modelValue', 'focus', 'blur'],
  data: () => ({ open: false, draft: '', busy: false, err: '' }),
  template: `
  <div style="position:relative">
    <textarea :value="modelValue" :rows="rows"
      @input="$emit('update:modelValue', $event.target.value)"
      @focus="$emit('focus')" @blur="$emit('blur')"
      style="width:100%;background:#0d1117;color:#eee;border:1px solid #2c3540;border-radius:6px;padding:6px;resize:vertical;font-size:13px"></textarea>
    <button v-if="modelValue" @mousedown.prevent.stop="openDialog" title="AI 优化这段文本"
            style="position:absolute;top:3px;right:3px;background:#7c3aed;font-size:11px;padding:1px 6px;border-radius:4px">✨</button>
    <div v-if="open" style="position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:120;display:flex;align-items:center;justify-content:center" @click.self="open=false">
      <div class="card" style="width:min(720px,92vw)">
        <h3>✨ 优化文本<span class="muted" style="font-size:12px">（确认后覆盖原文本框）</span></h3>
        <textarea v-model="draft" rows="9" style="width:100%;background:#0d1117;color:#eee;border:1px solid #2c3540;border-radius:6px;padding:6px;resize:vertical;font-size:13px"></textarea>
        <p v-if="err" style="color:#f87171;font-size:13px">{{ err }}</p>
        <p style="display:flex;gap:8px;align-items:center">
          <button @click="optimize" :disabled="busy || !draft.trim()" style="background:#7c3aed">
            {{ busy ? '优化中…' : '✨ 优化' }}</button>
          <button @click="apply" :disabled="busy || !draft.trim()" style="background:#16a34a">确认覆盖</button>
          <button @click="open=false" :disabled="busy">取消</button>
          <span class="muted" style="font-size:12px">可先手改再优化；优化走「提示词优化」路由的 LLM</span>
        </p>
      </div>
    </div>
  </div>`,
  methods: {
    openDialog() { this.draft = this.modelValue || ''; this.err = ''; this.open = true; },
    async optimize() {
      this.busy = true; this.err = '';
      try {
        const r = await fetch('/api/llm/optimize', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: this.draft, kind: this.kind }) });
        if (!r.ok) { this.err = '优化失败：' + ((await r.json()).detail || r.status); }
        else {
          const t = (await r.json()).text || '';
          // 空结果不清空原文本（真机 2026-08-27：LLM 端点配错返回空 text，弹窗文本被清空）
          if (!t.trim()) { this.err = '优化返回空结果，已保留原文本'; }
          else { this.draft = t; }
        }
      } catch (e) { this.err = '优化失败：' + e; }
      this.busy = false;
    },
    apply() { this.$emit('update:modelValue', this.draft); this.open = false; },
  },
};

createApp({ components: { PromptBox }, data, computed, methods, async mounted() {
    await this.refresh();
    setInterval(async () => {  // 项目列表轮询：autopilot 角标/成片状态实时化
      if (this.view === 'projects' && !this.creating) {
        try { await this.refresh(); } catch (e) { /* 忽略瞬时失败 */ }
      }
    }, 4000);
  } })
  .mount('#app');
