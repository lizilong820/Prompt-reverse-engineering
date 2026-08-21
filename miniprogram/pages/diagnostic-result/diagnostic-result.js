const { api } = require("../../utils/api");
const { ensurePrivacyAuthorized } = require("../../utils/privacy");

Page({
  data: { job: null, result: null, scoreItems: [], language: "zh", prompt: "", errorMessage: "" },
  timer: null,
  onLoad(options) { this.id = options.id; this.load(); },
  onUnload() { if (this.timer) clearTimeout(this.timer); },
  async load() {
    try {
      await getApp().ensureLogin();
      const job = await api("/api/v1/replication-diagnostics/" + this.id);
      if (job.status === "failed") return this.setData({ job, errorMessage: job.error_message || "诊断失败，算力已返还" });
      if (job.status !== "succeeded") { this.setData({ job }); this.timer = setTimeout(() => this.load(), 1800); return; }
      const result = job.result;
      const labels = { subject: "主体相似度", action: "动作匹配度", camera: "运镜匹配度", composition: "构图匹配度", style: "风格匹配度" };
      const scoreItems = Object.keys(labels).map((key) => ({ key, label: labels[key], value: result.scores[key] }));
      this.setData({ job, result, scoreItems, prompt: result.corrected_prompt.zh });
    } catch (error) { this.setData({ errorMessage: error.message || "诊断结果读取失败" }); }
  },
  switchLanguage(event) {
    const language = event.currentTarget.dataset.language;
    this.setData({ language, prompt: this.data.result.corrected_prompt[language] });
  },
  async copyPrompt() {
    if (!(await ensurePrivacyAuthorized())) return;
    wx.setClipboardData({ data: this.data.prompt });
  }
  ,openFeedback() { wx.navigateTo({ url: "/pages/feedback/feedback?taskType=replication_diagnostic&taskId=" + this.id }); }
});
