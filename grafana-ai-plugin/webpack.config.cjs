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
    'react/jsx-runtime',
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
              transform: { react: { runtime: 'automatic' } },
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
