const { api, uploadJob, onUploadProgress } = require("../../utils/api");
const { AD_UNIT_ID } = require("../../config");
const { ensurePrivacyAuthorized } = require("../../utils/privacy");

Page({
  data: { mode: "image", credits: 0, pricing: { image: 1, video: 3 }, currentCost: 1, adReward: 1, filePath: "", fileName: "", fileSize: "", submitting: false, progress: 0, adLoading: false },
  rewardAd: null,
  async onLoad() {
    if (AD_UNIT_ID && wx.createRewardedVideoAd) {
      this.rewardAd = wx.createRewardedVideoAd({ adUnitId: AD_UNIT_ID });
      this.rewardAd.onError((error) => { this.setData({ adLoading: false }); wx.showToast({ title: "广告暂不可用", icon: "none" }); console.error(error); });
    }
  },
  async onShow() { try { const user = await getApp().ensureLogin(); this.syncUser(user); } catch (error) { wx.showToast({ title: error.message, icon: "none" }); } },
  syncUser(user) { this.setData({ credits: user.credits, pricing: user.pricing || this.data.pricing, currentCost: (user.pricing || this.data.pricing)[this.data.mode], adReward: user.ad?.reward || 1 }); },
  switchMode(event) { const mode = event.currentTarget.dataset.mode; this.setData({ mode, currentCost: this.data.pricing[mode], filePath: "", fileName: "", fileSize: "" }); },
  async chooseMedia() {
    if (!(await ensurePrivacyAuthorized())) return;
    wx.chooseMedia({ count: 1, mediaType: [this.data.mode], sourceType: ["album", "camera"], maxDuration: 90, success: ({ tempFiles }) => { const file = tempFiles[0]; const size = file.size || 0; const max = this.data.mode === "image" ? 12 : 180; if (size > max * 1024 * 1024) return wx.showToast({ title: "文件超过 " + max + "MB", icon: "none" }); this.setData({ filePath: file.tempFilePath, fileName: file.tempFilePath.split("/").pop(), fileSize: (size / 1024 / 1024).toFixed(2) + " MB" }); } });
  },
  async watchRewardAd() {
    if (!this.rewardAd) return wx.showToast({ title: "激励广告尚未配置", icon: "none" });
    this.setData({ adLoading: true });
    try {
      const prepared = await api("/api/v1/rewards/ad/prepare", { method: "POST" });
      const ended = await new Promise(async (resolve, reject) => { const handler = (event) => { this.rewardAd.offClose(handler); resolve(event?.isEnded); }; this.rewardAd.onClose(handler); try { await this.rewardAd.show(); } catch (_) { try { await this.rewardAd.load(); await this.rewardAd.show(); } catch (error) { this.rewardAd.offClose(handler); reject(error); } } });
      if (!ended) return wx.showToast({ title: "完整观看后才能获得积分", icon: "none" });
      const result = await api("/api/v1/rewards/ad/complete", { method: "POST", data: { claim_token: prepared.claim_token } });
      this.setData({ credits: result.credits }); wx.showToast({ title: "+" + result.rewarded + " 积分" });
    } catch (error) { wx.showToast({ title: error.message || "领取失败", icon: "none" }); } finally { this.setData({ adLoading: false }); }
  },
  async submit() {
    if (this.data.credits < this.data.currentCost) return wx.showToast({ title: "积分不足", icon: "none" });
    const idempotencyKey = Date.now() + "-" + Math.random().toString(36).slice(2) + "-" + this.data.mode;
    this.setData({ submitting: true, progress: 0 }); onUploadProgress((progress) => this.setData({ progress }));
    try { const job = await uploadJob(this.data.filePath, this.data.mode, idempotencyKey); await getApp().refreshMe(); wx.navigateTo({ url: "/pages/result/result?id=" + job.id }); } catch (error) { await getApp().refreshMe().then((user) => this.syncUser(user)).catch(() => {}); wx.showModal({ title: "分析失败", content: error.message || "积分已自动退回", showCancel: false }); } finally { this.setData({ submitting: false }); }
  }
});
