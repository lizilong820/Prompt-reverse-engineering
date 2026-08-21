const { api, uploadDiagnosticVideo, onUploadProgress } = require("../../utils/api");
const { ensurePrivacyAuthorized } = require("../../utils/privacy");

Page({
  data: {
    originalPath: "", originalName: "", generatedPath: "", generatedName: "",
    computeCount: 0, cost: 1, submitting: false, progress: 0, stageText: ""
  },
  async onShow() {
    try {
      const user = await getApp().ensureLogin();
      this.setData({ computeCount: user.compute_count ?? user.credits ?? 0, cost: user.pricing?.diagnostic || 1 });
    } catch (error) { wx.showToast({ title: error.message, icon: "none" }); }
  },
  async chooseVideo(event) {
    if (this.data.submitting || !(await ensurePrivacyAuthorized())) return;
    const role = event.currentTarget.dataset.role;
    wx.chooseMedia({
      count: 1, mediaType: ["video"], sourceType: ["album", "camera"], maxDuration: 90,
      success: ({ tempFiles }) => {
        const file = tempFiles[0];
        if (!file) return;
        if (file.size > 180 * 1024 * 1024) return wx.showToast({ title: "视频不能超过 180MB", icon: "none" });
        const name = file.tempFilePath.split("/").pop();
        this.setData(role === "original" ? { originalPath: file.tempFilePath, originalName: name } : { generatedPath: file.tempFilePath, generatedName: name });
      }
    });
  },
  noop() {},
  async submit() {
    if (this.data.submitting) return;
    if (!this.data.originalPath || !this.data.generatedPath) return wx.showToast({ title: "请选择两段视频", icon: "none" });
    if (this.data.computeCount < this.data.cost) return wx.showToast({ title: "算力次数不足", icon: "none" });
    this.setData({ submitting: true, progress: 0, stageText: "正在创建诊断任务" });
    let diagnostic;
    try {
      diagnostic = await api("/api/v1/replication-diagnostics", { method: "POST", data: { idempotency_key: `${Date.now()}-${Math.random().toString(36).slice(2)}-diagnostic` } });
      this.setData({ computeCount: Math.max(0, this.data.computeCount - this.data.cost), stageText: "正在上传原视频" });
      onUploadProgress((progress) => this.setData({ progress: Math.round(progress * 0.5) }));
      await uploadDiagnosticVideo(diagnostic.id, "original", this.data.originalPath);
      this.setData({ stageText: "正在上传生成视频", progress: 50 });
      onUploadProgress((progress) => this.setData({ progress: 50 + Math.round(progress * 0.5) }));
      await uploadDiagnosticVideo(diagnostic.id, "generated", this.data.generatedPath);
      onUploadProgress(null);
      wx.redirectTo({ url: "/pages/diagnostic-result/diagnostic-result?id=" + diagnostic.id });
    } catch (error) {
      onUploadProgress(null);
      if (diagnostic?.id) await api(`/api/v1/replication-diagnostics/${diagnostic.id}/cancel`, { method: "POST" }).catch(() => null);
      const user = await getApp().refreshMe().catch(() => null);
      this.setData({ computeCount: user?.compute_count ?? user?.credits ?? this.data.computeCount, submitting: false, stageText: "" });
      wx.showModal({ title: "提交失败", content: error.message || "诊断任务提交失败，已扣算力会自动返还", showCancel: false });
    }
  }
});
