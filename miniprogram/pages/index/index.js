const { api, uploadJob, onUploadProgress } = require("../../utils/api");
const { AD_UNIT_ID } = require("../../config");
const { ensurePrivacyAuthorized } = require("../../utils/privacy");

Page({
  data: { mode: "image", sourceMode: "upload", analysisDepth: "detailed", computeCount: 0, adReward: 1, adRemaining: 20, filePath: "", fileName: "", fileSize: "", videoLink: "", submitting: false, progress: 0, adLoading: false },
  rewardAd: null,
  active: true,
  onLoad() {
    this.active = true;
    if (AD_UNIT_ID && wx.createRewardedVideoAd) {
      this.rewardAd = wx.createRewardedVideoAd({ adUnitId: AD_UNIT_ID });
      this.rewardAd.onError((error) => { if (this.active) this.setData({ adLoading: false }); wx.showToast({ title: "广告暂不可用", icon: "none" }); console.error(error); });
    }
  },
  onUnload() { this.active = false; onUploadProgress(); },
  async onShow() { try { const user = await getApp().ensureLogin(); if (this.active) this.syncUser(user); } catch (error) { wx.showToast({ title: error.message, icon: "none" }); } },
  syncUser(user) { this.setData({ computeCount: user.compute_count ?? user.credits ?? 0, adReward: user.ad?.reward || 1, adRemaining: user.ad?.remaining_today ?? user.ad?.daily_limit ?? 20 }); },
  switchMode(event) { this.setData({ mode: event.currentTarget.dataset.mode, sourceMode: "upload", filePath: "", fileName: "", fileSize: "", videoLink: "" }); },
  switchSource(event) { this.setData({ sourceMode: event.currentTarget.dataset.source, filePath: "", fileName: "", fileSize: "" }); },
  selectDepth(event) { this.setData({ analysisDepth: event.currentTarget.dataset.depth }); },
  onVideoLinkInput(event) { this.setData({ videoLink: event.detail.value }); },
  async chooseMedia() {
    if (!(await ensurePrivacyAuthorized())) return;
    wx.chooseMedia({ count: 1, mediaType: [this.data.mode], sourceType: ["album", "camera"], maxDuration: 90, success: ({ tempFiles }) => { const file = tempFiles[0]; if (!file || !this.active) return; const size = file.size || 0; const max = this.data.mode === "image" ? 12 : 180; if (size > max * 1024 * 1024) return wx.showToast({ title: "文件超过 " + max + "MB", icon: "none" }); this.setData({ filePath: file.tempFilePath, fileName: file.tempFilePath.split("/").pop(), fileSize: (size / 1024 / 1024).toFixed(2) + " MB" }); } });
  },
  async watchRewardAd() {
    if (!this.rewardAd) return wx.showToast({ title: "激励广告尚未配置", icon: "none" });
    if (this.data.adRemaining <= 0) return wx.showToast({ title: "今日奖励次数已用完", icon: "none" });
    this.setData({ adLoading: true });
    try {
      const prepared = await api("/api/v1/rewards/ad/prepare", { method: "POST" });
      const ended = await new Promise(async (resolve, reject) => { const handler = (event) => { this.rewardAd.offClose(handler); resolve(event?.isEnded); }; this.rewardAd.onClose(handler); try { await this.rewardAd.show(); } catch (_) { try { await this.rewardAd.load(); await this.rewardAd.show(); } catch (error) { this.rewardAd.offClose(handler); reject(error); } } });
      if (!ended) return wx.showToast({ title: "完整观看后才能获得次数", icon: "none" });
      const result = await api("/api/v1/rewards/ad/complete", { method: "POST", data: { claim_token: prepared.claim_token } });
      if (this.active) this.setData({ computeCount: result.compute_count ?? result.credits, adRemaining: Math.max(0, this.data.adRemaining - 1) });
      wx.showToast({ title: "+" + result.rewarded + " 次算力" });
    } catch (error) { wx.showToast({ title: error.message || "领取失败", icon: "none" }); } finally { if (this.active) this.setData({ adLoading: false }); }
  },
  async submit() {
    const usingLink = this.data.mode === "video" && this.data.sourceMode === "link";
    if (this.data.computeCount < 1) return wx.showToast({ title: "算力次数不足", icon: "none" });
    if (usingLink && !this.data.videoLink.trim()) return wx.showToast({ title: "请粘贴视频分享链接", icon: "none" });
    if (!usingLink && !this.data.filePath) return wx.showToast({ title: "请选择素材", icon: "none" });
    const idempotencyKey = Date.now() + "-" + Math.random().toString(36).slice(2) + "-" + this.data.mode;
    this.setData({ submitting: true, progress: usingLink ? 5 : 0 });
    onUploadProgress((progress) => { if (this.active) this.setData({ progress }); });
    try {
      const job = usingLink ? await api("/api/v1/jobs/remote", { method: "POST", data: { url: this.data.videoLink.trim(), analysis_depth: this.data.analysisDepth, idempotency_key: idempotencyKey } }) : await uploadJob(this.data.filePath, this.data.mode, idempotencyKey, this.data.analysisDepth);
      await getApp().refreshMe(); wx.navigateTo({ url: "/pages/result/result?id=" + job.id });
    } catch (error) { await getApp().refreshMe().then((user) => { if (this.active) this.syncUser(user); }).catch(() => {}); wx.showModal({ title: error.stage === "upload" ? "上传失败" : "提交失败", content: error.message || "任务未提交，未消耗算力次数", showCancel: false }); } finally { onUploadProgress(); if (this.active) this.setData({ submitting: false }); }
  }
});
