const { api } = require("../../utils/api");
const { ensurePrivacyAuthorized } = require("../../utils/privacy");
const OPTIMIZATION_LABELS = { action: "强化动作", camera: "强化运镜", identity: "人物一致", style: "风格强化", concise: "精简提示词", professional: "专业扩写" };
Page({
  data: {
    job: null, analysis: { timeline: [] }, facts: [], prompt: "", promptZh: "", promptEn: "", language: "zh",
    platform: "universal", platformOptions: [], platforms: {}, versionKey: "base", versionOptions: [{ key: "base", label: "原始版本" }],
    optimizations: [], optimizationStrategies: Object.keys(OPTIMIZATION_LABELS).map((key) => ({ key, label: OPTIMIZATION_LABELS[key] })),
    optimizing: false, optimizationCost: 1, computeCount: 0, errorMessage: "", project: null, projectSaving: false
  },
  pollTimer: null,
  optimizationTimer: null,
  async onLoad(options) {
    this.jobId = options.id;
    await this.loadJob();
  },
  onUnload() { if (this.pollTimer) clearTimeout(this.pollTimer); if (this.optimizationTimer) clearTimeout(this.optimizationTimer); },
  async loadJob() {
    try {
      const user = await getApp().ensureLogin(); const job = await api("/api/v1/jobs/" + this.jobId);
      if (job.status === "failed") return this.setData({ errorMessage: job.error_message || "分析失败，算力次数已退回" });
      if (job.status !== "succeeded") { this.setData({ errorMessage: "任务仍在分析中，请保持页面打开" }); this.pollTimer = setTimeout(() => this.loadJob(), 2000); return; }
      const analysis = job.result.analysis; const facts = [["主体 SUBJECT", "subject"], ["场景 SCENE", "scene"], ["构图 COMPOSITION", "composition"], ["镜头 CAMERA", "camera"], ["光线 LIGHTING", "lighting"], ["色彩 COLOR", "color"], ["风格 STYLE", "style"]].map(([label, key]) => ({ label, value: analysis[key] }));
      const prompts = job.result.prompts || {};
      const isVideoPrompt = job.mode === "video" || job.analysis_task === "image_expand_video";
      const promptZh = isVideoPrompt ? (prompts.video || prompts.chinese || prompts.universal || "") : (prompts.chinese || prompts.universal || "");
      const promptEn = prompts.english || prompts.universal || prompts.video || "";
      const platforms = prompts.platforms || { universal: { label: "通用", zh: promptZh, en: promptEn } };
      const platformOptions = Object.keys(platforms).map((key) => ({ key, label: platforms[key].label || key }));
      const platform = platformOptions[0]?.key || "universal";
      this.setData({ job, analysis, facts, isImageExpansion: job.analysis_task === "image_expand_video", promptZh, promptEn, platforms, platformOptions, platform, prompt: platforms[platform]?.zh || promptZh, computeCount: user.compute_count ?? user.credits ?? 0, optimizationCost: user.pricing?.optimization || 1 });
      const projects = await api("/api/v1/projects").catch(() => []);
      this.setData({ project: projects.find((item) => item.source_job_id === Number(this.jobId)) || null });
      await this.loadOptimizations();
    } catch (error) { this.setData({ errorMessage: error.message }); }
  },
  versionOptions(platform, optimizations = this.data.optimizations) {
    return [{ key: "base", label: "原始版本" }, ...optimizations.filter((item) => item.status === "succeeded" && item.platform === platform).map((item) => ({ key: "optimization-" + item.id, label: OPTIMIZATION_LABELS[item.strategy] || "优化版本" }))];
  },
  resolvePrompt(platform = this.data.platform, language = this.data.language, versionKey = this.data.versionKey, optimizations = this.data.optimizations) {
    if (versionKey !== "base") {
      const id = Number(versionKey.replace("optimization-", ""));
      const item = optimizations.find((entry) => entry.id === id);
      if (item?.result?.[language]) return item.result[language];
    }
    const selected = this.data.platforms[platform] || {};
    return selected[language] || selected.zh || (language === "en" ? this.data.promptEn : this.data.promptZh);
  },
  switchLanguage(event) { const language = event.currentTarget.dataset.language; this.setData({ language, prompt: this.resolvePrompt(this.data.platform, language) }); },
  switchPlatform(event) { const platform = event.currentTarget.dataset.platform; this.setData({ platform, versionKey: "base", versionOptions: this.versionOptions(platform), prompt: this.resolvePrompt(platform, this.data.language, "base") }); },
  switchVersion(event) { const versionKey = event.currentTarget.dataset.version; this.setData({ versionKey, prompt: this.resolvePrompt(this.data.platform, this.data.language, versionKey) }); },
  async loadOptimizations() {
    const optimizations = await api("/api/v1/jobs/" + this.jobId + "/optimizations");
    const processing = optimizations.find((item) => item.status === "processing");
    this.setData({ optimizations, versionOptions: this.versionOptions(this.data.platform, optimizations), optimizing: Boolean(processing) });
    if (processing) this.pollOptimization(processing.id);
  },
  async optimizePrompt(event) {
    if (this.data.optimizing) return;
    if (this.data.computeCount < this.data.optimizationCost) return wx.showToast({ title: "算力次数不足", icon: "none" });
    const strategy = event.currentTarget.dataset.strategy;
    const confirmed = await new Promise((resolve) => wx.showModal({ title: OPTIMIZATION_LABELS[strategy], content: "将针对当前目标模型优化提示词，消耗 " + this.data.optimizationCost + " 次算力。", confirmText: "开始优化", success: (result) => resolve(result.confirm), fail: () => resolve(false) }));
    if (!confirmed) return;
    this.setData({ optimizing: true });
    try {
      const task = await api("/api/v1/jobs/" + this.jobId + "/optimizations", { method: "POST", data: { strategy, platform: this.data.platform, idempotency_key: Date.now() + "-" + Math.random().toString(36).slice(2) + "-opt" } });
      this.setData({ computeCount: Math.max(0, this.data.computeCount - this.data.optimizationCost) });
      this.pollOptimization(task.id);
    } catch (error) {
      this.setData({ optimizing: false });
      wx.showToast({ title: error.message || "优化提交失败", icon: "none" });
    }
  },
  async pollOptimization(id) {
    if (this.optimizationTimer) clearTimeout(this.optimizationTimer);
    try {
      const task = await api("/api/v1/jobs/" + this.jobId + "/optimizations/" + id);
      if (task.status === "processing") { this.optimizationTimer = setTimeout(() => this.pollOptimization(id), 1600); return; }
      const user = await getApp().refreshMe().catch(() => ({ compute_count: this.data.computeCount, credits: this.data.computeCount }));
      if (task.status === "failed") {
        this.setData({ optimizing: false, computeCount: user.compute_count ?? user.credits ?? this.data.computeCount });
        wx.showToast({ title: task.error_message || "优化失败，算力已返还", icon: "none" });
        await this.loadOptimizations();
        return;
      }
      const optimizations = [task, ...this.data.optimizations.filter((item) => item.id !== task.id)];
      const matchesPlatform = task.platform === this.data.platform;
      const versionKey = matchesPlatform ? "optimization-" + task.id : "base";
      const prompt = matchesPlatform ? (task.result?.[this.data.language] || this.data.prompt) : this.resolvePrompt(this.data.platform, this.data.language, "base", optimizations);
      this.setData({ optimizations, optimizing: false, computeCount: user.compute_count ?? user.credits ?? this.data.computeCount, versionOptions: this.versionOptions(this.data.platform, optimizations), versionKey, prompt });
      wx.showToast({ title: "优化完成" });
    } catch (error) {
      this.optimizationTimer = setTimeout(() => this.pollOptimization(id), 3000);
    }
  },
  async copyPrompt() {
    if (!(await ensurePrivacyAuthorized())) return;
    wx.setClipboardData({ data: this.data.prompt });
  },
  async saveProject() {
    if (this.data.projectSaving) return;
    const title = await new Promise((resolve) => wx.showModal({ title: "保存创作项目", editable: true, content: this.data.project?.title || this.data.job.filename || "未命名项目", confirmText: "保存", success: (result) => resolve(result.confirm ? (result.content || this.data.job.filename || "未命名项目") : ""), fail: () => resolve("") }));
    if (!title) return;
    this.setData({ projectSaving: true });
    try {
      const project = await api("/api/v1/projects", { method: "POST", data: { job_id: Number(this.jobId), title, note: this.data.project?.note || "", platform: this.data.platform } });
      this.setData({ project });
      wx.showToast({ title: "项目已保存" });
    } catch (error) { wx.showToast({ title: error.message || "保存失败", icon: "none" }); }
    finally { this.setData({ projectSaving: false }); }
  }
  ,openFeedback() { wx.navigateTo({ url: "/pages/feedback/feedback?taskType=job&taskId=" + this.jobId }); }
});
