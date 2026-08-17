const { api } = require("../../utils/api");
Page({
  data: { jobs: [] },
  onShow() { this.load(); },
  onPullDownRefresh() { this.load().finally(wx.stopPullDownRefresh); },
  async load() { try { await getApp().ensureLogin(); const jobs = await api("/api/v1/jobs"); const labels = { succeeded: "已完成", failed: "已退款", processing: "分析中" }; this.setData({ jobs: jobs.map((job) => ({ ...job, statusText: labels[job.status], createdText: new Date(job.created_at).toLocaleString() })) }); } catch (error) { wx.showToast({ title: error.message, icon: "none" }); } },
  openJob(event) { wx.navigateTo({ url: "/pages/result/result?id=" + event.currentTarget.dataset.id }); }
});
