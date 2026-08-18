const { api } = require("../../utils/api");
Page({
  data: { computeCount: 0, pricing: { image: 1, video: 1, depth: 1 }, ledger: [], jobs: [], depthJobs: [], totalCount: 0 },
  onShow() { this.load(); },
  async load() { try { const user = await getApp().ensureLogin(); const [ledger, jobs, depthJobs] = await Promise.all([api("/api/v1/credits/ledger"), api("/api/v1/jobs"), api("/api/v1/depth/jobs")]); const reasons = { welcome_bonus: "新用户赠送", rewarded_ad: "激励广告奖励", analysis_job: "反推任务", job_refund: "失败返还算力", tool_refund: "工具失败返还", depth_job: "深度转换" }; this.setData({ computeCount: user.compute_count ?? user.credits, pricing: user.pricing, totalCount: jobs.length + depthJobs.length, jobs: jobs.slice(0, 10).map((job) => ({ ...job, createdText: new Date(job.created_at).toLocaleString() })), depthJobs: depthJobs.slice(0, 10), ledger: ledger.map((item) => ({ ...item, reasonText: reasons[item.reason] || item.reason, createdText: new Date(item.created_at).toLocaleString() })) }); } catch (error) { wx.showToast({ title: error.message, icon: "none" }); } }
});
