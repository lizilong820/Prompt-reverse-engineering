const { api, uploadDepthJob, downloadAuthenticated } = require("../../utils/api");
const { AD_UNIT_ID } = require("../../config");
const { ensurePrivacyAuthorized } = require("../../utils/privacy");
Page({
  data: { sourceMode: "upload", filePath: "", fileName: "", videoLink: "", preset: "standard_depth", computeCount: 0, adReward: 1, adRemaining: 20, job: null, submitting: false, adLoading: false }, timer: null, active: true, rewardAd: null,
  onLoad() {
    this.active = true;
    if (AD_UNIT_ID && wx.createRewardedVideoAd) {
      this.rewardAd = wx.createRewardedVideoAd({ adUnitId: AD_UNIT_ID });
    }
  },
  async onShow() { this.active = true; try { const user = await getApp().ensureLogin(); this.setData({ computeCount: user.compute_count ?? user.credits ?? 0, adReward: user.ad?.reward || 1, adRemaining: user.ad?.remaining_today ?? user.ad?.daily_limit ?? 20 }); } catch (error) { wx.showToast({ title: error.message, icon: "none" }); } },
  onUnload() {
    this.active = false;
    if (this.timer) clearTimeout(this.timer);
  },
  chooseSource(event) { this.setData({ sourceMode: event.currentTarget.dataset.source, filePath: "", fileName: "", videoLink: "", job: null }); },
  choosePreset(event) { this.setData({ preset: event.currentTarget.dataset.preset }); },
  onLinkInput(event) { this.setData({ videoLink: event.detail.value }); },
  async watchRewardAd() {
    if (!this.rewardAd) return wx.showToast({ title: "激励广告尚未配置", icon: "none" });
    if (this.data.adRemaining <= 0) return wx.showToast({ title: "今日奖励次数已用完", icon: "none" });
    this.setData({ adLoading: true });
    try {
      const prepared = await api("/api/v1/rewards/ad/prepare", { method: "POST" });
      const ended = await new Promise(async (resolve, reject) => {
        const handler = (event) => { this.rewardAd.offClose(handler); resolve(event?.isEnded); };
        this.rewardAd.onClose(handler);
        try { await this.rewardAd.show(); } catch (_) {
          try { await this.rewardAd.load(); await this.rewardAd.show(); } catch (error) { this.rewardAd.offClose(handler); reject(error); }
        }
      });
      if (!ended) return wx.showToast({ title: "完整观看后才能获得次数", icon: "none" });
      const result = await api("/api/v1/rewards/ad/complete", { method: "POST", data: { claim_token: prepared.claim_token } });
      if (this.active) this.setData({ computeCount: result.compute_count ?? result.credits, adRemaining: Math.max(0, this.data.adRemaining - 1) });
      wx.showToast({ title: "+" + result.rewarded + " 次算力" });
    } catch (error) {
      wx.showToast({ title: error.message || "领取失败", icon: "none" });
    } finally {
      if (this.active) this.setData({ adLoading: false });
    }
  },
  async chooseVideo() { if (!(await ensurePrivacyAuthorized())) return; wx.chooseMedia({ count: 1, mediaType: ["video"], sourceType: ["album", "camera"], maxDuration: 300, success: ({ tempFiles }) => { const file = tempFiles[0]; if (!file) return; if (file.size > 500 * 1024 * 1024) return wx.showToast({ title: "视频不能超过 500MB", icon: "none" }); this.setData({ filePath: file.tempFilePath, fileName: file.tempFilePath.split("/").pop() }); } }); },
  async submit() {
    if (this.data.computeCount < 1) return wx.showToast({ title: "算力次数不足", icon: "none" });
    if (this.data.sourceMode === "upload" && !this.data.filePath) return wx.showToast({ title: "请选择视频", icon: "none" });
    if (this.data.sourceMode === "link" && !this.data.videoLink.trim()) return wx.showToast({ title: "请粘贴视频链接", icon: "none" });
    const key = Date.now() + "-" + Math.random().toString(36).slice(2) + "-depth"; this.setData({ submitting: true });
    try { const job = this.data.sourceMode === "upload" ? await uploadDepthJob(this.data.filePath, this.data.preset, key) : await api("/api/v1/depth/jobs/remote", { method: "POST", data: { url: this.data.videoLink.trim(), preset: this.data.preset, idempotency_key: key } }); this.setData({ job, computeCount: this.data.computeCount - 1 }); this.poll(job.id); }
    catch (error) { wx.showModal({ title: "提交失败", content: error.message || "任务未提交，未消耗算力次数", showCancel: false }); } finally { this.setData({ submitting: false }); }
  },
  async poll(id) { try { const job = await api("/api/v1/depth/jobs/" + id); if (!this.active) return; this.setData({ job }); if (["completed", "failed"].includes(job.status)) return; this.timer = setTimeout(() => this.poll(id), 1500); } catch (error) { if (this.active) wx.showToast({ title: error.message, icon: "none" }); } },
  async download() { try { const path = await downloadAuthenticated("/api/v1/depth/jobs/" + this.data.job.id + "/download"); await new Promise((resolve, reject) => wx.saveVideoToPhotosAlbum({ filePath: path, success: resolve, fail: reject })); wx.showToast({ title: "已保存到相册" }); } catch (error) { wx.showToast({ title: error.errMsg || error.message || "保存失败", icon: "none" }); } }
});
