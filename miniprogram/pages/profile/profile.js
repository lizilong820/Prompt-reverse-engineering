const { api } = require("../../utils/api");
Page({
  data: { computeCount: 0, pricing: { image: 1, video: 1, depth: 1 }, jobs: [], depthJobs: [], diagnosticJobs: [], totalCount: 0 },
  onShow() { this.load(); },
  async load() {
    try {
      const user = await getApp().ensureLogin();
      this.setData({ computeCount: user.compute_count ?? user.credits ?? 0, pricing: user.pricing || this.data.pricing });
      const [jobsResult, depthResult, diagnosticResult] = await Promise.all([
        api("/api/v1/jobs").then((value) => ({ value })).catch((error) => ({ error })),
        api("/api/v1/depth/jobs").then((value) => ({ value })).catch((error) => ({ error })),
        api("/api/v1/replication-diagnostics").then((value) => ({ value })).catch((error) => ({ error }))
      ]);
      const jobs = Array.isArray(jobsResult.value) ? jobsResult.value : [];
      const depthJobs = Array.isArray(depthResult.value) ? depthResult.value : [];
      const diagnosticJobs = Array.isArray(diagnosticResult.value) ? diagnosticResult.value : [];
      this.setData({ totalCount: jobs.length + depthJobs.length + diagnosticJobs.length, jobs, depthJobs, diagnosticJobs });
      if (jobsResult.error && depthResult.error && diagnosticResult.error) throw jobsResult.error;
    } catch (error) { wx.showToast({ title: error.message || "我的页面加载失败", icon: "none" }); }
  },
  openHistory() { wx.navigateTo({ url: "/pages/history/history" }); },
  openLedger() { wx.navigateTo({ url: "/pages/ledger/ledger" }); }
});
