function ensurePrivacyAuthorized() {
  if (typeof wx.requirePrivacyAuthorize !== "function") return Promise.resolve(true);
  return new Promise((resolve) => {
    wx.requirePrivacyAuthorize({
      success: () => resolve(true),
      fail: () => {
        wx.showModal({
          title: "需要隐私授权",
          content: "使用相册选择和复制提示词前，请先阅读并同意隐私保护指引。",
          confirmText: "查看指引",
          success: ({ confirm }) => {
            if (confirm && typeof wx.openPrivacyContract === "function") {
              wx.openPrivacyContract({ success: () => resolve(false), fail: () => resolve(false) });
            } else {
              resolve(false);
            }
          },
          fail: () => resolve(false),
        });
      },
    });
  });
}

module.exports = { ensurePrivacyAuthorized };
