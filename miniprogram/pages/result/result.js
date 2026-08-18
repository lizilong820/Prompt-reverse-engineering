const { api } = require("../../utils/api");
const { ensurePrivacyAuthorized } = require("../../utils/privacy");
Page({
  data: { job: null, analysis: { timeline: [] }, facts: [], prompt: "", promptZh: "", promptEn: "", language: "zh", errorMessage: "" },
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
      this.setData({ job, analysis, facts, promptZh: prompts.chinese || prompts.universal || "", promptEn: prompts.english || prompts.universal || "", prompt: prompts.chinese || prompts.universal || "" });
    } catch (error) { this.setData({ errorMessage: error.message }); }
  },
  switchLanguage(event) { const language = event.currentTarget.dataset.language; this.setData({ language, prompt: language === "en" ? this.data.promptEn : this.data.promptZh }); },
  async copyPrompt() {
    if (!(await ensurePrivacyAuthorized())) return;
    wx.setClipboardData({ data: this.data.prompt });
  }
});
