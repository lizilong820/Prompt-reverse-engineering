const { api } = require("../../utils/api");
Page({
  data: { computeCount: 0, pricing: { image: 1, video: 1, depth: 1 }, jobs: [], depthJobs: [], diagnosticJobs: [], totalCount: 0, referral: { code: "", reward: 3, invited_count: 0, bound: false }, referralBindings: [] },
  onShow() { this.load(); },
  async load() {
    try {
      const user = await getApp().ensureLogin();
      this.setData({ computeCount: user.compute_count ?? user.credits ?? 0, pricing: user.pricing || this.data.pricing, referral: user.referral || this.data.referral });
      const [jobsResult, depthResult, diagnosticResult, referralResult] = await Promise.all([
        api("/api/v1/jobs").then((value) => ({ value })).catch((error) => ({ error })),
        api("/api/v1/depth/jobs").then((value) => ({ value })).catch((error) => ({ error })),
        api("/api/v1/replication-diagnostics").then((value) => ({ value })).catch((error) => ({ error })),
        api("/api/v1/referrals").then((value) => ({ value })).catch((error) => ({ error }))
      ]);
      const jobs = Array.isArray(jobsResult.value) ? jobsResult.value : [];
      const depthJobs = Array.isArray(depthResult.value) ? depthResult.value : [];
      const diagnosticJobs = Array.isArray(diagnosticResult.value) ? diagnosticResult.value : [];
      const referral = referralResult.value || this.data.referral;
      this.setData({ totalCount: jobs.length + depthJobs.length + diagnosticJobs.length, jobs, depthJobs, diagnosticJobs, referral, referralBindings: referral.bindings || [] });
      if (jobsResult.error && depthResult.error && diagnosticResult.error) throw jobsResult.error;
    } catch (error) { wx.showToast({ title: error.message || "我的页面加载失败", icon: "none" }); }
  },
  openHistory() { wx.navigateTo({ url: "/pages/history/history" }); },
  openLedger() { wx.navigateTo({ url: "/pages/ledger/ledger" }); }
  ,openProjects() { wx.navigateTo({ url: "/pages/projects/projects" }); },
  copyReferralCode() {
    if (!this.data.referral.code) return;
    wx.setClipboardData({ data: this.data.referral.code, success: () => wx.showToast({ title: "邀请码已复制", icon: "none" }) });
  },
  bindReferral() {
    if (this.data.referral.bound) return wx.showToast({ title: "你已绑定过邀请码", icon: "none" });
    wx.showModal({ title: "填写好友邀请码", editable: true, placeholderText: "请输入 8 位邀请码", confirmText: "提交", success: async (res) => {
      if (!res.confirm || !res.content?.trim()) return;
      try {
        const result = await api("/api/v1/referrals/bind", { method: "POST", data: { code: res.content.trim() } });
        wx.showToast({ title: result.message || "绑定成功", icon: "none" });
        await this.load();
      } catch (error) { wx.showToast({ title: error.message || "邀请码绑定失败", icon: "none" }); }
    } });
  }
});
