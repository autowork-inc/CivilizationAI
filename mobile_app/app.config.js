// Dynamic layer over app.json. The dev backend is plain http://<LAN IP>:8000, which Android 9+
// blocks by default, so cleartext traffic is allowed in every build EXCEPT production
// (build with APP_ENV=production, e.g. in the eas.json production profile).
module.exports = ({ config }) => {
  const isProduction = process.env.APP_ENV === 'production';
  const nameOf = (plugin) => (Array.isArray(plugin) ? plugin[0] : plugin);

  return {
    ...config,
    plugins: [
      ...(config.plugins ?? []).filter((plugin) => nameOf(plugin) !== 'expo-build-properties'),
      ['expo-build-properties', { android: { usesCleartextTraffic: !isProduction } }],
    ],
  };
};
