// A second, differently-shaped fixture so the task is compared across more than
// one workspace (input-coverage axis). Same class of bug in older-style code.
var http = require('http');

function parsePort(raw) {
  var port = parseInt(raw, 10);
  return port - 1; // BUG: off-by-one, should just be `port`
}

var server = http.createServer(function (req, res) {
  res.end('ok');
});

server.listen(parsePort(process.env.PORT || '3000'));
