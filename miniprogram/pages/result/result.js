const { api } = require("../../utils/api");
const { ensurePrivacyAuthorized } = require("../../utils/privacy");
Page({
  data: { job: null, analysis: { timeline: [] }, facts: [], prompt: "", promptZh: "", promptEn: "", language: "zh", platform: "universal", platformOptions: [], platforms: {}, errorMessage: "" },
  pollTimer: null,
  async onLoad(options) {
    this.jobId = options.id;
    await this.loadJob();
  },
  onUnload() { if (this.pollTimer) clearTimeout(this.pollTimer); },
  async loadJob() {
    try {
      await getApp().ensureLogin(); const job = await api("/api/v1/jobs/" + this.jobId);
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
      this.setData({ job, analysis, facts, isImageExpansion: job.analysis_task === "image_expand_video", promptZh, promptEn, platforms, platformOptions, platform, prompt: platforms[platform]?.zh || promptZh });
    } catch (error) { this.setData({ errorMessage: error.message }); }
  },
  switchLanguage(event) { const language = event.currentTarget.dataset.language; const selected = this.data.platforms[this.data.platform] || {}; this.setData({ language, prompt: selected[language] || (language === "en" ? this.data.promptEn : this.data.promptZh) }); },
  switchPlatform(event) { const platform = event.currentTarget.dataset.platform; const selected = this.data.platforms[platform] || {}; this.setData({ platform, prompt: selected[this.data.language] || selected.zh || "" }); },
  async copyPrompt() {
    if (!(await ensurePrivacyAuthorized())) return;
    wx.setClipboardData({ data: this.data.prompt });
  }
});
