const { api } = require("../../utils/api");
Page({
  data: { credits: 0, pricing: { image: 1, video: 3 }, ledger: [] },
  onShow() { this.load(); },
  async load() { try { const user = await getApp().ensureLogin(); const ledger = await api("/api/v1/credits/ledger"); const reasons = { welcome_bonus: "新用户赠送", rewarded_ad: "激励广告奖励", analysis_job: "分析任务", job_refund: "分析失败退款", purchase: "购买积分" }; this.setData({ credits: user.credits, pricing: user.pricing, ledger: ledger.map((item) => ({ ...item, reasonText: reasons[item.reason] || item.reason, createdText: new Date(item.created_at).toLocaleString() })) }); } catch (error) { wx.showToast({ title: error.message, icon: "none" }); } }
});
