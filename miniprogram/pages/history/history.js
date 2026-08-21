const { api } = require("../../utils/api");

Page({
  data: { records: [] },
  onShow() { this.load(); },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()); },
  async load() {
    try {
      await getApp().ensureLogin();
      const [jobs, depthJobs, diagnosticJobs] = await Promise.all([
        api("/api/v1/jobs"),
        api("/api/v1/depth/jobs"),
        api("/api/v1/replication-diagnostics")
      ]);
      const analysisLabels = { succeeded: "已完成", failed: "已返还", processing: "分析中" };
      const depthLabels = { completed: "已完成", expired: "已过期", failed: "已返还", submitting: "提交中", queued: "等待中", downloading: "下载中", loading_model: "加载模型", processing: "处理中", encoding: "编码中", finalizing: "保存结果中" };
      const depthPresetLabels = { quick_preview: "快速预览", standard_depth: "标准深度", motion_character: "人物动作" };
      const records = [
        ...jobs.map((job) => ({
          ...job,
          recordKey: "analysis-" + job.id,
          recordType: "analysis",
          title: job.analysis_task === "image_expand_video" ? "画面拓展" : job.mode === "video" ? "视频反推" : "图片反推",
          statusText: analysisLabels[job.status] || job.status,
          createdText: new Date(job.created_at).toLocaleString()
        })),
        ...depthJobs.map((job) => ({
          ...job,
          recordKey: "depth-" + job.id,
          recordType: "depth",
          title: "视频深度转换 · " + (depthPresetLabels[job.preset] || "标准深度"),
          statusText: depthLabels[job.status] || job.status,
          createdText: new Date(job.created_at).toLocaleString()
        })),
        ...diagnosticJobs.map((job) => ({
          ...job,
          recordKey: "diagnostic-" + job.id,
          recordType: "diagnostic",
          title: "视频复刻诊断",
          filename: [job.original_filename, job.generated_filename].filter(Boolean).join(" / ") || "等待上传",
          statusText: analysisLabels[job.status] || (job.status === "awaiting_upload" ? "等待上传" : job.status),
          createdText: new Date(job.created_at).toLocaleString()
        }))
      ].sort((left, right) => new Date(right.created_at) - new Date(left.created_at));
      this.setData({ records });
    } catch (error) {
      wx.showToast({ title: error.message, icon: "none" });
    }
  },
  openRecord(event) {
    const record = this.data.records.find((item) => String(item.id) === String(event.currentTarget.dataset.id) && item.recordType === event.currentTarget.dataset.type);
    if (!record) return;
    if (record.recordType === "analysis" && record.status === "succeeded") {
      wx.navigateTo({ url: "/pages/result/result?id=" + record.id });
      return;
    }
    if (record.recordType === "depth" && record.status === "completed") {
      wx.setStorageSync("openDepthJobId", record.id);
      wx.switchTab({ url: "/pages/tools/tools" });
      return;
    }
    if (record.recordType === "diagnostic" && record.status === "succeeded") {
      wx.navigateTo({ url: "/pages/diagnostic-result/diagnostic-result?id=" + record.id });
    }
  }
});
