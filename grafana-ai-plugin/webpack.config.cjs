const path = require('path');
const CopyWebpackPlugin = require('copy-webpack-plugin');

module.exports = {
  context: __dirname,
  entry: './src/module.tsx',
  devtool: 'source-map',
  output: {
    clean: true,
    filename: 'module.js',
    library: { type: 'amd' },
    path: path.resolve(__dirname, 'dist'),
    publicPath: 'auto',
  },
  externals: [
    'react',
    'react-dom',
    'rxjs',
    /^@grafana\/data/i,
    /^@grafana\/runtime/i,
    /^@grafana\/ui/i,
  ],
  module: {
    rules: [
      {
        test: /\.[tj]sx?$/,
        exclude: /node_modules/,
        use: {
          loader: 'swc-loader',
          options: {
            jsc: {
              target: 'es2020',
              parser: { syntax: 'typescript', tsx: true },
              // Grafana 12 exposes React itself to AMD plugins, but the hand-written
              // bundle does not get create-plugin's react/jsx-runtime mapping. Use
              // the classic transform so the browser never requests /react/jsx-runtime.
              transform: { react: { runtime: 'classic' } },
            },
          },
        },
      },
    ],
  },
  resolve: {
    extensions: ['.ts', '.tsx', '.js', '.jsx'],
  },
  plugins: [
    new CopyWebpackPlugin({
      patterns: [
        { from: 'src/plugin.json', to: 'plugin.json' },
        { from: 'src/img', to: 'img' },
        { from: 'README.md', to: 'README.md' },
      ],
    }),
  ],
};
