const express = require('express');
const config = require('./config'); // BUG: this file does not exist

const app = express();
const port = process.env.PORT || 3000;

app.get('/', (req, res) => {
  res.json({ status: 'ok', message: `Hello from ${config.appName}` });
});

app.get('/health', (req, res) => {
  res.json({ status: 'healthy', uptime: process.uptime() });
});

app.listen(port, () => {
  console.log(`App listening on port ${port}`);
});
