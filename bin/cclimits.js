#!/usr/bin/env node
const { spawn } = require('child_process');
const path = require('path');

const script = path.join(__dirname, '..', 'lib', 'cclimits.py');
const child = spawn(process.platform === 'win32' ? 'python' : 'python3', [script, ...process.argv.slice(2)], {
  stdio: 'inherit',
  env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  windowsHide: true
});

child.on('error', (err) => {
  if (err.code === 'ENOENT') {
    console.error('Error: python3 not found. Please install Python 3.9+');
    process.exit(1);
  }
  throw err;
});

child.on('close', (code) => process.exit(code ?? 0));
