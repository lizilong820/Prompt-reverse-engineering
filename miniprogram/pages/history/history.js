const { api } = require("../../utils/api");

Page({
  data: { records: [] },
  onShow() { this.load(); },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()); },
  async load() {
    try {
      await getApp().ensureLogin();
      const [jobs, depthJobs] = await Promise.all([
        api("/api/v1/jobs"),
        api("/api/v1/depth/jobs")
      ]);
      const analysisLabels = { succeeded: "已完成", failed: "已返还", processing: "分析中" };
      const depthLabels = { completed: "已完成", failed: "已返还", submitting: "提交中", queued: "等待中", downloading: "下载中", loading_model: "加载模型", processing: "处理中", encoding: "编码中" };
      const records = [
        ...jobs.map((job) => ({
          ...job,
          recordKey: "analysis-" + job.id,
          recordType: "analysis",
          title: job.mode === "video" ? "视频反推" : "图片反推",
          statusText: analysisLabels[job.status] || job.status,
          createdText: new Date(job.created_at).toLocaleString()
        })),
        ...depthJobs.map((job) => ({
          ...job,
          recordKey: "depth-" + job.id,
          recordType: "depth",
          title: "视频深度转换",
          statusText: depthLabels[job.status] || job.status,
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
    if (!record || record.recordType !== "analysis") return;
    wx.navigateTo({ url: "/pages/result/result?id=" + record.id });
  }
});
