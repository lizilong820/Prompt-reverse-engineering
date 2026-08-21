const { api } = require("../../utils/api");

const reasonLabels = {
  welcome_bonus: "新用户赠送",
  rewarded_ad: "激励广告奖励",
  analysis_job: "反推任务",
  job_refund: "反推失败返还",
  tool_refund: "工具失败返还",
  depth_job: "深度转换",
  prompt_optimization: "提示词优化",
  optimization_refund: "优化失败返还",
  admin_refund: "管理员返还"
};

Page({
  data: { ledger: [] },
  onShow() { this.load(); },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()); },
  async load() {
    try {
      await getApp().ensureLogin();
      const ledger = await api("/api/v1/credits/ledger");
      this.setData({
        ledger: ledger.map((item) => ({
          ...item,
          reasonText: reasonLabels[item.reason] || (item.reason?.startsWith("admin_adjustment:") ? "管理员调整" : item.reason),
          createdText: new Date(item.created_at).toLocaleString()
        }))
      });
    } catch (error) {
      wx.showToast({ title: error.message, icon: "none" });
    }
  }
});
