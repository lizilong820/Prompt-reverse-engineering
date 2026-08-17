const DEVELOPMENT = true;

module.exports = {
  // 正式发布前将 DEVELOPMENT 改为 false，并配置已备案 HTTPS 域名。
  API_BASE_URL: DEVELOPMENT ? "http://150.158.135.233:9001" : "https://api.example.com",
  AD_UNIT_ID: ""
};
