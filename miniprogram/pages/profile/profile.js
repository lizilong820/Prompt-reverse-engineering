const { api } = require("../../utils/api");
Page({
  data: { computeCount: 0, pricing: { image: 1, video: 1, depth: 1 }, jobs: [], depthJobs: [], totalCount: 0 },
  onShow() { this.load(); },
  async load() { try { const user = await getApp().ensureLogin(); const [jobs, depthJobs] = await Promise.all([api("/api/v1/jobs"), api("/api/v1/depth/jobs")]); this.setData({ computeCount: user.compute_count ?? user.credits, pricing: user.pricing, totalCount: jobs.length + depthJobs.length, jobs, depthJobs }); } catch (error) { wx.showToast({ title: error.message, icon: "none" }); } },
  openHistory() { wx.navigateTo({ url: "/pages/history/history" }); },
  openLedger() { wx.navigateTo({ url: "/pages/ledger/ledger" }); }
});
