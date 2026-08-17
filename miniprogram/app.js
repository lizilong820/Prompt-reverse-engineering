const { api } = require("./utils/api");

App({
  globalData: { user: null, pricing: { image: 1, video: 3 }, ad: null },
  async onLaunch() {
    try { await this.ensureLogin(); } catch (error) { console.error("login failed", error); }
  },
  async ensureLogin() {
    if (wx.getStorageSync("token")) return this.refreshMe();
    const login = await new Promise((resolve, reject) => wx.login({ success: resolve, fail: reject }));
    const payload = await api("/api/v1/auth/wechat", { method: "POST", data: { code: login.code }, auth: false });
    wx.setStorageSync("token", payload.token);
    this.globalData.user = payload.user;
    this.globalData.pricing = payload.pricing;
    this.globalData.ad = payload.ad;
    return payload.user;
  },
  async refreshMe() {
    try {
      const user = await api("/api/v1/me");
      this.globalData.user = user;
      this.globalData.pricing = user.pricing;
      this.globalData.ad = user.ad;
      return user;
    } catch (error) {
      if (error.statusCode === 401) { wx.removeStorageSync("token"); return this.ensureLogin(); }
      throw error;
    }
  }
});
